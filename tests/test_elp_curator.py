"""R3 deterministic extraction: grounding, candidate separation, bounded calls and recovery."""
import asyncio
import copy
import json
import pytest
from repo_code.models import Assistant,ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.memory.repository import MemoryRepository
from repo_code.memory.consolidation import Consolidator
from repo_code.memory.models import MemoryProfile
from repo_code.memory.curation import EpisodeCuration
from repo_code.episodes import EpisodeBuilder
from repo_code.evolution import EvolutionEngine
from repo_code.util import RepoError
from test_review_workflow import setup,tool

PROCEDURE={'triggers':['A focused behavior check failed after a source change'],
 'exclusions':['Dependency or Host failures, or ambiguous requirements'],
 'steps':['Inspect the failing behavior, change the implementation, rerun that check'],
 'expected_output':'A corrected implementation with source-bound check evidence',
 'necessary_checks':['Rerun the observed counterexample on the final source'],
 'counterexamples_or_unknowns':['A local pass does not prove official acceptance']}

async def fixture(tmp_path,code=None):
    app,run=setup(tmp_path)
    await tool(app,run,'create',{'path':'fixed.py','content':'value=1'},'write')
    with app.store.tx() as c:
        app.store.emit(c,run['id'],'tool.operation_finished',{'tool_name':'process.run','result':{
            'exit_code':1,'stderr':'AssertionError: boundary mismatch',**({'code':code} if code else {})}},'DEV')
    app.runtime.end(run['id'],'PARTIAL','focused check failed')
    eid,=EpisodeBuilder(app.store).capture_run(run['id'])
    owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
    return app,run,eid,owner

def claim(payload,category='PROCEDURE',subject='counterexample-recovery'):
    source=payload['evidence'][0]
    quote='AssertionError' if 'AssertionError' in source['text'] else source['text'][:80]
    return {'category':category,'text':quote if category=='OBSERVATION' else 'Retain and recheck the observed counterexample after repair',
        'subject':subject,'source_ids':[source['source_id']],'quote':quote,
        **({'procedure':copy.deepcopy(PROCEDURE)} if category=='PROCEDURE' else {})}

def provider(make):
    def handler(system,messages,tools,index):
        assert tools==[]
        payload=json.loads(messages[0]['content'])
        answer=make(payload,index)
        return Assistant(text=json.dumps(answer),usage={'input_tokens':123,'output_tokens':40})
    return ScriptedProvider(handler=handler)

@pytest.mark.asyncio
async def test_curator_separates_lesson_and_skill_with_host_grounded_quotes(tmp_path):
    app,run,eid,owner=await fixture(tmp_path)
    try:
        before=app.releases.active()
        def output(p,i):return {'status':'COMPLETE','claims':[claim(p,'OBSERVATION','observed'),claim(p),claim(p,'HYPOTHESIS','cause')]}
        p=provider(output);report=await owner.curate_episode(eid,p)
        assert [r['action'] for r in report['receipts']]==['ADD']*3 and p.calls==1
        lessons=app.store.values('lesson');skills=app.store.values('skill_candidate')
        assert len(lessons)==3 and len(skills)==1
        assert all(l['quote_ids'] and l['status']=='CANDIDATE' for l in lessons)
        assert skills[0]['applicability_limit']=='SINGLE_TASK' and skills[0]['task_ids']==[run['task_id']]
        assert all(r['lifecycle']=='CANDIDATE' for r in owner.repo.values('memory_versions'))
        assert not app.store.values('knowledge') and app.releases.active()==before
        assert owner.repo.values('memory_budget')[0]['charged_tokens']==163
        assert (await owner.curate_episode(eid,p))==report and p.calls==1
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['source','quote','observation','procedure','category','fields'])
async def test_bad_claim_is_rejected_without_losing_good_claim(tmp_path,bad):
    app,run,eid,owner=await fixture(tmp_path)
    try:
        def output(p,i):
            value=claim(p)
            if bad=='source':value['source_ids']=['unseen-source']
            if bad=='quote':value['quote']='not in any source'
            if bad=='observation':value.update(category='OBSERVATION',text='All hidden tests passed');value.pop('procedure')
            if bad=='procedure':value['procedure'].pop('exclusions')
            if bad=='category':value['category']='FACT'
            if bad=='fields':value['quote_id']='model-supplied-authority'
            return {'status':'COMPLETE','claims':[value,claim(p,'OBSERVATION')]}
        report=await owner.curate_episode(eid,provider(output))
        assert [r['action'] for r in report['receipts']]==['REJECT','ADD']
        assert len(app.store.values('lesson'))==1 and not app.store.values('skill_candidate')
    finally:app.close()

@pytest.mark.asyncio
async def test_needs_evidence_has_one_bounded_expansion_and_no_full_trace(tmp_path):
    app,run,eid,owner=await fixture(tmp_path)
    seen=[]
    try:
        def output(p,i):
            seen.append(p)
            if i==0:return {'status':'NEEDS_EVIDENCE','source_ids':[p['metadata']['catalogue'][-1]]}
            return {'status':'COMPLETE','claims':[claim(p,'HYPOTHESIS')]}
        p=provider(output);report=await owner.curate_episode(eid,p)
        assert p.calls==2 and report['calls']==2 and seen[1]['expansion']
        assert all('events' not in payload and len(payload['evidence'])<=4 for payload in seen)
        assert len(owner.repo.values('memory_budget'))==2
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['unknown','third'])
async def test_invalid_expansion_cannot_escape_two_call_budget(tmp_path,mode):
    app,run,eid,owner=await fixture(tmp_path)
    try:
        p=provider(lambda payload,i:{'status':'NEEDS_EVIDENCE','source_ids':['hidden'] if mode=='unknown' else [payload['metadata']['catalogue'][0]]})
        with pytest.raises(RepoError):await owner.curate_episode(eid,p)
        assert p.calls==(1 if mode=='unknown' else 2)
        if mode=='third':
            with pytest.raises(RepoError,match='CURATOR_CALL_LIMIT'):await owner.curate_episode(eid,p)
            assert p.calls==2
        assert not app.store.values('lesson')
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('category',['HOST_DEFECT','ENVIRONMENT','CONTRACT_AMBIGUITY'])
async def test_non_behavior_issues_do_not_become_skills(tmp_path,category):
    app,run,eid,owner=await fixture(tmp_path,code='HOST_ERROR')
    try:
        p=provider(lambda payload,i:{'status':'COMPLETE','claims':[claim(payload),claim(payload,category)]})
        report=await owner.curate_episode(eid,p)
        assert [r['action'] for r in report['receipts']]==['REJECT','ADD']
        assert not app.store.values('skill_candidate') and len(app.store.values('curation_issue'))==1
    finally:app.close()

@pytest.mark.asyncio
async def test_replay_after_commit_crash_uses_persisted_response(tmp_path,monkeypatch):
    app,run,eid,owner=await fixture(tmp_path)
    p=provider(lambda payload,i:{'status':'COMPLETE','claims':[claim(payload)]})
    original=app.store.put
    def crash(kind,*args,**kwargs):
        result=original(kind,*args,**kwargs)
        if kind=='lesson':raise RuntimeError('injected publication crash')
        return result
    try:
        monkeypatch.setattr(app.store,'put',crash)
        with pytest.raises(RuntimeError):await owner.curate_episode(eid,p)
        assert not owner.repo.values('memory_versions') and not app.store.values('lesson')
        monkeypatch.setattr(app.store,'put',original)
        report=await owner.curate_episode(eid,p)
        assert p.calls==1 and report['receipts'][0]['action']=='ADD'
    finally:app.close()

@pytest.mark.asyncio
async def test_unknown_usage_stops_retries_and_does_not_publish(tmp_path):
    app,run,eid,owner=await fixture(tmp_path)
    p=ScriptedProvider([Assistant(text='{"status":"COMPLETE","claims":[]}')])
    try:
        for _ in range(2):
            with pytest.raises(RepoError,match='CURATOR_USAGE_UNSETTLED'):await owner.curate_episode(eid,p)
        assert p.calls==1 and owner.repo.values('memory_budget')[0]['state']=='USAGE_UNKNOWN'
        assert not app.store.values('lesson')
    finally:app.close()

@pytest.mark.asyncio
async def test_concurrent_curators_cannot_duplicate_dispatch(tmp_path):
    app,run,eid,owner=await fixture(tmp_path)
    entered=asyncio.Event();release=asyncio.Event()
    async def handle(system,messages,tools,index):
        entered.set();await release.wait()
        return Assistant(text=json.dumps({'status':'COMPLETE','claims':[claim(json.loads(messages[0]['content']))]}),usage={'input_tokens':100,'output_tokens':30})
    p=ScriptedProvider(handler=handle)
    try:
        first=asyncio.create_task(owner.curate_episode(eid,p));await entered.wait()
        with pytest.raises(RepoError):await owner.curate_episode(eid,p)
        release.set();await first
        assert p.calls==1 and len(app.store.values('lesson'))==1
    finally:app.close()

@pytest.mark.asyncio
async def test_revoke_during_extraction_rejects_commit_and_candidate_reads(tmp_path):
    app,run,eid,owner=await fixture(tmp_path)
    try:
        def output(payload,i):
            owner.repo.revoke(eid)
            return {'status':'COMPLETE','claims':[claim(payload)]}
        with pytest.raises(RepoError):await owner.curate_episode(eid,provider(output))
        assert not app.store.values('lesson')
    finally:app.close()

@pytest.mark.asyncio
async def test_candidate_read_rechecks_revocation_and_budget_zero_blocks_dispatch(tmp_path):
    app,run,eid,owner=await fixture(tmp_path)
    try:
        owner.profile=owner.profile.model_copy(update={'auxiliary_calls':0})
        p=provider(lambda payload,i:{'status':'COMPLETE','claims':[claim(payload)]})
        with pytest.raises(RepoError,match='MEMORY_AUXILIARY_BUDGET'):await owner.curate_episode(eid,p)
        assert p.calls==0
        owner.profile=owner.profile.model_copy(update={'auxiliary_calls':8})
        result=await owner.curate_episode(eid,p);item=result['receipts'][0]['id']
        assert owner.curated_item('skill_candidate',item)['status']=='CANDIDATE'
        owner.repo.revoke(eid)
        with pytest.raises(RepoError):owner.curated_item('skill_candidate',item)
    finally:app.close()

@pytest.mark.asyncio
async def test_legacy_entry_uses_unified_contract_and_low_run_makes_no_call(tmp_path):
    app,run=setup(tmp_path)
    try:
        app.runtime.end(run['id'],'PARTIAL','nothing changed')
        p=provider(lambda payload,i:{'status':'COMPLETE','claims':[]})
        report=await EvolutionEngine(app,p).extract_skill([run['id']])
        assert report['contract']=='episode-curation-r3' and report['reports']==[] and p.calls==0
    finally:app.close()

@pytest.mark.asyncio
async def test_batch_duplicates_conflicts_noop_and_persisted_report(tmp_path):
    app,run,eid,owner=await fixture(tmp_path)
    try:
        def output(payload,i):
            first=claim(payload,'HYPOTHESIS','same-subject')
            other={**first,'text':'A competing causal explanation remains unproven'}
            return {'status':'COMPLETE','claims':[first,first,other,{'category':'NO_LEARNING_VALUE'}]}
        p=provider(output);report=await owner.curate_episode(eid,p)
        assert [r['action'] for r in report['receipts']]==['ADD','NOOP','CONFLICT','NOOP']
        assert len(owner.repo.values('memory_versions'))==2
        reopened=Consolidator(MemoryRepository(app.store),app.runtime,owner.profile)
        assert await reopened.curate_episode(eid,p)==report and p.calls==1
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['tool','length','hidden','growth'])
async def test_admission_response_and_growth_boundaries(tmp_path,mode):
    app,run,eid,owner=await fixture(tmp_path)
    try:
        if mode=='hidden':
            from repo_code.governance import Lineage
            Lineage(app.store).expose(eid,'final','family')
        if mode=='growth':owner.profile=owner.profile.model_copy(update={'max_items':1})
        def output(payload,i):return {'status':'COMPLETE','claims':[claim(payload,'OBSERVATION','a'),claim(payload,'HYPOTHESIS','b')]}
        p=provider(output)
        if mode in {'tool','length'}:
            p=ScriptedProvider([Assistant(text='{}',tool_calls=[ToolCall(id='bad',name='read',arguments={'path':'secret'})] if mode=='tool' else [],
                stop_reason='length' if mode=='length' else 'stop',usage={'input_tokens':5,'output_tokens':5})])
        if mode=='growth':
            report=await owner.curate_episode(eid,p)
            assert [r['action'] for r in report['receipts']]==['ADD','REJECT']
        else:
            with pytest.raises(RepoError):await owner.curate_episode(eid,p)
            assert not app.store.values('lesson') and p.calls==(0 if mode=='hidden' else 1)
    finally:app.close()

@pytest.mark.asyncio
async def test_user_decision_requires_real_control_source(tmp_path):
    app,run,eid,owner=await fixture(tmp_path)
    try:
        app.runtime.admit(run['id'],'correction-input','STEER','Use the public boundary behavior')
        eid,=EpisodeBuilder(app.store).capture_run(run['id'])
        def output(payload,i):
            source=next(s for s in payload['evidence'] if s['type']=='control.admitted')
            return {'status':'COMPLETE','claims':[{'category':'DECISION','text':'Use the public boundary behavior',
                'subject':'requested-boundary','source_ids':[source['source_id']],'quote':'Use the public boundary behavior'}]}
        result=await owner.curate_episode(eid,provider(output))
        assert result['receipts'][0]['action']=='ADD'
        row=owner.repo.values('memory_versions')[0]
        assert row['epistemic_status']=='USER_STATED' and row['support_refs'][0]['origin_class']=='USER_STATED'
    finally:app.close()

@pytest.mark.asyncio
async def test_profile_change_cannot_reset_episode_call_or_usage_limit(tmp_path):
    app,run,eid,owner=await fixture(tmp_path)
    p=ScriptedProvider([Assistant(text='{"status":"COMPLETE","claims":[]}')])
    try:
        with pytest.raises(RepoError,match='CURATOR_USAGE_UNSETTLED'):await owner.curate_episode(eid,p)
        owner.profile=owner.profile.model_copy(update={'auxiliary_calls':9})
        with pytest.raises(RepoError,match='CURATOR_PROFILE_CHANGED'):await owner.curate_episode(eid,p)
        assert p.calls==1
    finally:app.close()

@pytest.mark.asyncio
async def test_raw_multiline_quote_is_bound_without_json_escape_false_rejection(tmp_path):
 app,run=setup(tmp_path)
 with app.store.tx() as c:
  app.store.emit(c,run['id'],'tool.operation_finished',{'tool_name':'process.run','result':{'exit_code':1,'stderr':'AssertionError:\nexpected value differs\n'}},'DEV')
 app.runtime.end(run['id'],'PARTIAL','failed check')
 eid,=EpisodeBuilder(app.store).capture_run(run['id'])
 owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
 try:
  def output(payload,i):
   source=next(s for s in payload['evidence'] if any('AssertionError' in v for v in s['quote_views']))
   view=next(v for v in source['quote_views'] if 'AssertionError' in v and not v.startswith('{'))
   return {'status':'COMPLETE','claims':[{'category':'OBSERVATION','text':view,'quote':view,'source_ids':[source['source_id']]}]}
  result=await owner.curate_episode(eid,provider(output))
  assert result['receipts'][0]['action']=='ADD'
 finally:app.close()
