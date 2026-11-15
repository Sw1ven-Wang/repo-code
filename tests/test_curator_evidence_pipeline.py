"""Protocol fixtures are synthetic DEV observations, not model efficacy evidence."""
import copy,json
import pytest
from repo_code.util import digest,now,RepoError,uid
from repo_code.episodes import EpisodeBuilder
from repo_code.memory.repository import MemoryRepository
from repo_code.memory.consolidation import Consolidator
from repo_code.memory.models import MemoryProfile
from repo_code.memory.evidence_pipeline import EvidencePipeline,CORE_ROLES,validate_metadata,adapt_legacy_metadata,clipped
from repo_code.memory.causal_curation import CausalCuration
from repo_code.models import Assistant
from repo_code.providers import ScriptedProvider
from test_review_workflow import setup

def emit(app,run,tool,args,body):
    op=uid('op-')
    with app.store.tx() as c:
        c.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)',(op,op,run['id'],'SUCCEEDED','PURE',json.dumps({'arguments':args}),json.dumps(body),0,now()))
        seq=app.store.emit(c,run['id'],'tool.operation_finished',{'operation_id':op,'tool_name':tool,'result':body},'DEV')
        return c.execute('SELECT event_id FROM events WHERE sequence=?',(seq,)).fetchone()[0]

async def prepared(tmp_path,*,boundary=True,config=None):
    app,run=setup(tmp_path)
    before=emit(app,run,'process.run',{'argv':['python','-c','print("unrelated_service")']},{'exit_code':0,'stdout':'unrelated'})
    original=emit(app,run,'process.run',{'argv':['python','-c','import demo; print(demo.normalize(1))']},{'exit_code':0,'stdout':'old state differs\n'})
    diagnostic=emit(app,run,'process.run',{'argv':['python','-c','import demo; print(demo.normalize(2))']},{'exit_code':0,'stdout':'comparison: "equal"\nmetadata differs\n'})
    change=emit(app,run,'edit',{'path':'demo.py','old_text':'def normalize(x): return x','new_text':'def normalize(x):\n    return x + 1\n'},{'changed_paths':['demo.py']})
    if boundary:
        create=emit(app,run,'create',{'path':'test_demo.py','content':'from demo import normalize\ndef test_boundary():\n    assert normalize(0) == 1\n'},{'changed_paths':['test_demo.py'],'versions':{'test_demo.py':digest('test')}})
    good=emit(app,run,'process.run',{'argv':['python','-m','pytest','test_demo.py']},{'exit_code':0,'stdout':'2 passed\n'})
    for _ in range(3):emit(app,run,'process.run',{'argv':['python','-m','pytest','test_demo.py']},{'exit_code':1,'stderr':"No module named 'pytest'\n"})
    partial=emit(app,run,'process.run',{'argv':['python','-m','pytest','test_demo.py','-k','one']},{'exit_code':0,'stdout':'1 passed, 2 deselected\n'})
    app.runtime.end(run['id'],'PARTIAL','synthetic protocol fixture')
    old,=EpisodeBuilder(app.store).capture_run(run['id'],pipeline='legacy');eid=EpisodeBuilder(app.store).capture_causal(old)
    app.store.put('operator_config','curator_evidence_pipeline',{'enabled':True,**(config or {})})
    owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only',auxiliary_calls=3))
    return app,run,old,eid,owner,dict(unrelated=before,original=original,diagnostic=diagnostic,change=change,good=good,partial=partial,**({'create':create} if boundary else {}))

def observe(payload,*,category='OBSERVATION'):
    src=next(s for s in payload['evidence'] if 'stdout_excerpt' in s['selectors'])
    quote=src['selectors']['stdout_excerpt'].splitlines()[0]
    return {'category':category,'text':quote,'subject':'local fixture','evidence_refs':[{'evidence_id':src['evidence_id'],'source_digest':src['source_digest'],'selector':'stdout_excerpt','quote':quote,'relation':'supports'}]}

def procedure(payload):
    refs=[]
    for role,selector,relation in [('ORIGINAL_FAILURE','stdout_excerpt','supports'),('IMPLEMENTATION_CHANGE','new_hunk','implements'),('POSITIVE_VERIFICATION','stdout_excerpt','verifies')]:
        src=next(s for s in payload['evidence'] if role in s['roles'] and selector in s['selectors'])
        refs.append(dict(evidence_id=src['evidence_id'],selector=selector,source_digest=src['source_digest'],quote=src['selectors'][selector].splitlines()[0],relation=relation))
    return {'category':'PROCEDURE','text':'Inspect state changes across transformation boundaries','subject':'state propagation','evidence_refs':refs,
        'procedure':{'problem_pattern':'State changes at a transform boundary','preconditions':['A local state comparison is observable'],
            'triggers':[payload['metadata']['condition_keys'][0]],'exclusions':['run_cancelled'],
            'steps':['Compare state before and after the transformation','Repair the first state loss and recheck'],
            'expected_output':'A locally consistent transformation','necessary_checks':['Original discrepancy'],
            'counterexamples_or_unknowns':['No independent transfer evidence'], 'positive_probes':['Run original discrepancy'],
            'negative_probes':['Unrelated input remains unchanged'],'boundary_probes':['Zero and adjacent state values'],
            'supporting_claims':[],'counter_claims':[],'validation_status':'UNVERIFIED'}}

def provider(fn):
    def handle(system,messages,tools,index):
        assert not tools
        return Assistant(text=json.dumps(fn(json.loads(messages[0]['content']),index)),usage={'input_tokens':200,'output_tokens':100})
    return ScriptedProvider(handler=handle)

@pytest.mark.asyncio
async def test_causal_episode_segments_are_append_only_and_exclude_unrelated(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        b=EpisodeBuilder(app.store);legacy=b.get(old);v=b.get(eid)
        assert ids['original'] not in legacy['support_refs'] and ids['original'] in v['support_refs']
        assert ids['diagnostic'] in v['segments']['ROOT_CAUSE_DIAGNOSIS']
        assert ids['unrelated'] not in v['support_refs']
        assert len(v['support_refs'])==len(set(v['support_refs'])) and v['verification_incomplete']
        assert b.capture_causal(old)==eid and b.get(old)==legacy
        assert b.capture_run(run['id'])==[eid]
    finally:app.close()

@pytest.mark.asyncio
async def test_catalogue_covers_roles_deduplicates_noise_and_prioritizes_target_checks(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        pipe=EvidencePipeline(owner);metadata,records,ref=pipe.build(eid);selected,report=pipe.select(records,24000)
        assert set(CORE_ROLES)<=set(report['covered_roles'])
        assert records[ids['good']]['metadata']['quality']>records[ids['partial']]['metadata']['quality']
        assert records[ids['partial']]['metadata']['limitations'][-1]=='PARTIAL_CHECK_SELECTION'
        assert sum('ENVIRONMENT_BLOCKER' in x['roles'] for x in selected)==1
        assert sum(len(r['metadata']['duplicate_source_ids']) for r in records.values())==2
        assert 'pytest' in records[ids['good']]['metadata']['summary']
        assert 'created_content' in records[ids['create']]['metadata']['allowed_selectors']
        created=json.loads(records[ids['create']]['fields']['creation_summary'])
        assert created['assertion_count_in_source']==1 and created['execution_status']=='INVOCATION_OBSERVED'
        assert ids['good'] in created['subsequent_invocation_source_ids']
        key,expanded=pipe.expand(eid,records,{'evidence_id':ids['create'],'selector':'created_content','max_bytes':2000},'attempt')
        assert 'assert normalize(0)' in expanded['content']
        assert pipe.expand(eid,records,{'evidence_id':ids['create'],'selector':'created_content','max_bytes':2000},'attempt')[0]==key
        assert 'normalize' in pipe.expand(eid,records,{'evidence_id':ids['change'],'selector':'function_definition','symbol':'normalize','max_bytes':1000},'attempt')[1]['content']
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['path','selector','bytes','digest','symbol','revoked','changed_request'])
async def test_expansion_cannot_escape_saved_source_authority(tmp_path,mode):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        pipe=EvidencePipeline(owner);_,records,_=pipe.build(eid)
        req={'evidence_id':ids['change'],'selector':'function_definition','symbol':'normalize','max_bytes':1000}
        if mode=='path':req['path']='C:/private'
        if mode=='selector':req['selector']='shell'
        if mode=='bytes':req['max_bytes']=100000
        if mode=='digest':req['source_digest']=digest('wrong')
        if mode=='symbol':req['symbol']='unseen'
        if mode=='revoked':owner.repo.revoke(ids['change'])
        if mode=='changed_request':
            with app.store.tx() as c:
                c.execute("UPDATE operations SET request=? WHERE id=(SELECT json_extract(data,'$.operation_id') FROM events WHERE event_id=?)",(json.dumps({'arguments':{'path':'demo.py','new_text':'def normalize(x): return 99'}}),ids['change']))
        with pytest.raises(RepoError):pipe.expand(eid,records,req,'attempt')
    finally:app.close()

@pytest.mark.asyncio
async def test_missing_boundary_is_explicit_not_filled_by_environment(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path,boundary=False)
    try:
        meta,_,_=EvidencePipeline(owner).build(eid)
        assert 'BOUNDARY_COUNTEREXAMPLE' in meta.missing_roles
        p=provider(lambda v,i:{'status':'COMPLETE','claims':[observe(v)]})
        report=await owner.curate_episode(eid,p)
        assert report['status']=='INCOMPLETE' and p.calls==1 and not app.store.values('skill_candidate')
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['missing','type','empty','digest','long','version','unknown'])
async def test_metadata_schema_rejects_malformed_fields(tmp_path,bad):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        v=EvidencePipeline(owner).build(eid)[0].model_dump()
        if bad=='missing':v.pop('schema_version')
        if bad=='type':v['event_ids']='not a list'
        if bad=='empty':v['episode_id']=''
        if bad=='digest':v['trace_id']='sha256:wrong'
        if bad=='long':v['evidence_catalogue'][0]['summary']='a'*1300
        if bad=='version':v['schema_version']='v999'
        if bad=='unknown':v['unexpected']=1
        with pytest.raises(RepoError,match='INVALID_METADATA'):validate_metadata(v)
        legacy=adapt_legacy_metadata({'episode_id':eid,'catalogue':[ids['change']]})
        assert legacy.legacy_summary['read_only'] and not legacy.evidence_catalogue
    finally:app.close()

@pytest.mark.parametrize('limit',[1,40,64,100,1024])
def test_multibyte_excerpt_never_exceeds_byte_bound(limit):
    assert len(clipped('汉字\n"quoted"'*1000,limit).encode())<=limit

@pytest.mark.asyncio
@pytest.mark.parametrize('category',['OBSERVATION','HYPOTHESIS','LESSON','PROCEDURE'])
async def test_layered_claims_quote_bindings_and_local_candidates(tmp_path,category):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        p=provider(lambda v,i:{'status':'COMPLETE','claims':[procedure(v) if category=='PROCEDURE' else observe(v,category=category)]})
        report=await owner.curate_episode(eid,p)
        assert report['receipts'][0]['action']=='ADD',report
        lesson=app.store.values('lesson')[0]
        assert lesson['semantic_review']['status']=='PENDING_INDEPENDENT_REVIEW'
        if category=='PROCEDURE':
            candidate=app.store.values('skill_candidate')[0]
            assert candidate['applicability_limit']=='SINGLE_TASK' and candidate['activation_authority']=='NONE'
            assert len({b['quote'] for b in lesson['quote_bindings']})==3
            assert lesson['candidate_validation_plan']['status'].startswith('UNREGISTERED')
        else:assert not app.store.values('skill_candidate')
        assert (await owner.curate_episode(eid,p))==report and p.calls==1
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['quote','digest','relation','legacy_shared','global_verified'])
async def test_wrong_quotes_relations_and_validation_claims_rejected(tmp_path,bad):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        def answer(v,i):
            c=procedure(v)
            if bad=='quote':c['evidence_refs'][1]['quote']=c['evidence_refs'][0]['quote']
            if bad=='digest':c['evidence_refs'][0]['source_digest']=digest('wrong')
            if bad=='relation':c['evidence_refs'][0]['relation']='implements'
            if bad=='legacy_shared':c['quote']='one shared';c['source_ids']=[ids['change']]
            if bad=='global_verified':c['procedure']['validation_status']='VERIFIED'
            return {'status':'COMPLETE','claims':[c,observe(v)]}
        report=await owner.curate_episode(eid,provider(answer))
        assert [r['action'] for r in report['receipts']]==['REJECT','ADD']
        assert not app.store.values('skill_candidate')
    finally:app.close()

@pytest.mark.asyncio
async def test_two_expansions_exhaust_cleanly_and_duplicate_requests_do_not_dispatch(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        def answer(v,i):
            return {'status':'NEEDS_EVIDENCE','claims':[observe(v,category='HYPOTHESIS')],
                'evidence_requests':[{'evidence_id':ids['change'] if i==0 else ids['create'],
                    'selector':'function_definition' if i==0 else 'created_content',
                    **({'symbol':'normalize'} if i==0 else {}),'max_bytes':1000}]}
        p=provider(answer);r=await owner.curate_episode(eid,p)
        assert p.calls==3 and r['status']=='INCOMPLETE' and r['expansions']==2
        assert not app.store.values('skill_candidate') and app.store.values('lesson')
        assert await owner.curate_episode(eid,p)==r and p.calls==3
    finally:app.close()

@pytest.mark.asyncio
async def test_repeated_expansion_is_denied_without_third_call(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        p=provider(lambda v,i:{'status':'NEEDS_EVIDENCE','claims':[],
            'evidence_requests':[{'evidence_id':ids['create'],'selector':'created_content','max_bytes':1000}]})
        r=await owner.curate_episode(eid,p)
        assert p.calls==2 and r['status']=='INCOMPLETE' and r['stop_reason']=='EVIDENCE_ALREADY_EXPANDED'
    finally:app.close()

@pytest.mark.asyncio
async def test_cancel_and_revocation_during_model_call_prevent_commit(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        def answer(v,i):
            owner.repo.revoke(ids['change'])
            return {'status':'COMPLETE','claims':[procedure(v)]}
        with pytest.raises(RepoError):await owner.curate_episode(eid,provider(answer))
        assert not app.store.values('lesson')
    finally:app.close()

@pytest.mark.asyncio
async def test_response_recovery_does_not_repeat_model_cost(tmp_path,monkeypatch):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    p=provider(lambda v,i:{'status':'COMPLETE','claims':[observe(v)]});original=app.store.put
    try:
        def crash(kind,*args,**kw):
            result=original(kind,*args,**kw)
            if kind=='lesson':raise RuntimeError('simulated crash')
            return result
        monkeypatch.setattr(app.store,'put',crash)
        with pytest.raises(RuntimeError):await owner.curate_episode(eid,p)
        assert not app.store.values('lesson')
        monkeypatch.setattr(app.store,'put',original)
        assert (await owner.curate_episode(eid,p))['receipts'][0]['action']=='ADD'
        assert p.calls==1 and len(owner.repo.values('memory_budget'))==1
    finally:app.close()

@pytest.mark.asyncio
async def test_decoded_newlines_quotes_and_different_source_quotes_are_grounded(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        def answer(v,i):
            src=next(s for s in v['evidence'] if s['evidence_id']==ids['diagnostic'])
            quote=src['selectors']['stdout_excerpt']
            c={'category':'OBSERVATION','text':quote,'evidence_refs':[{'evidence_id':ids['diagnostic'],'selector':'stdout_excerpt','source_digest':src['source_digest'],'quote':quote,'relation':'supports'}]}
            return {'status':'COMPLETE','claims':[c,procedure(v)]}
        r=await owner.curate_episode(eid,provider(answer))
        assert [x['action'] for x in r['receipts']]==['ADD','ADD']
        assert '\nmetadata differs\n' in app.store.values('lesson')[0]['text']
    finally:app.close()

@pytest.mark.asyncio
async def test_host_fallback_supplies_known_but_undelivered_role(tmp_path,monkeypatch):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    calls=[];original=EvidencePipeline.select
    def limited(self,records,max_bytes):
        sources,report=original(self,records,max_bytes)
        sources=[s for s in sources if 'BOUNDARY_COUNTEREXAMPLE' not in s['roles']]
        return sources,report
    monkeypatch.setattr(EvidencePipeline,'select',limited)
    try:
        def answer(v,i):calls.append(v);return {'status':'COMPLETE','claims':[observe(v)]}
        p=provider(answer);r=await owner.curate_episode(eid,p)
        assert p.calls==2 and r['expansions']==1 and r['status']=='COMPLETE'
        assert 'EVIDENCE_COVERAGE_INCOMPLETE' in r['transitions']
        assert any('BOUNDARY_COUNTEREXAMPLE' in s['roles'] for s in calls[1]['evidence'])
    finally:app.close()

@pytest.mark.asyncio
async def test_unknown_usage_does_not_retry_or_commit(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        p=ScriptedProvider([Assistant(text='{"status":"COMPLETE","claims":[]}')])
        for _ in range(2):
            with pytest.raises(RepoError,match='CURATOR_USAGE_UNSETTLED'):await owner.curate_episode(eid,p)
        assert p.calls==1 and not app.store.values('lesson')
    finally:app.close()

@pytest.mark.asyncio
async def test_cancel_during_expansion_keeps_no_partial_claim(tmp_path,monkeypatch):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    original=EvidencePipeline.expand
    def expand(self,*args,**kwargs):
        result=original(self,*args,**kwargs)
        with app.store.tx() as c:c.execute("UPDATE runs SET state='CANCELLED' WHERE id=?",(run['id'],))
        return result
    monkeypatch.setattr(EvidencePipeline,'expand',expand)
    try:
        p=provider(lambda v,i:{'status':'NEEDS_EVIDENCE','claims':[observe(v)],'evidence_requests':[{'evidence_id':ids['create'],'selector':'created_content','max_bytes':500}]})
        with pytest.raises(RepoError,match='CURATOR_CANCELLED'):await owner.curate_episode(eid,p)
        assert p.calls==1 and not app.store.values('lesson') and len(app.store.values('evidence_expansion'))==1
    finally:app.close()

@pytest.mark.asyncio
async def test_hidden_cohort_exposure_blocks_new_episode_and_expansion(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        from repo_code.governance import Lineage
        Lineage(app.store).expose(ids['diagnostic'],'final','family')
        with pytest.raises(RepoError):EvidencePipeline(owner).build(eid)
    finally:app.close()

@pytest.mark.asyncio
async def test_large_create_and_incomplete_hunk_function_remain_bounded(tmp_path):
    app,run=setup(tmp_path)
    try:
        emit(app,run,'process.run',{'argv':['python','-c','print(demo.transform(1))']},{'exit_code':1,'stdout':'old result'})
        sid=emit(app,run,'edit',{'path':'demo.py','new_text':'    def transform(x):\n        return x+1\n    if ready:\n'}, {'changed_paths':['demo.py']})
        created=emit(app,run,'create',{'path':'test_demo.py','content':'# long\n'*2000+'\ndef test_boundary():\n assert demo.transform(0)==1\n'},{'changed_paths':['test_demo.py']})
        app.runtime.end(run['id'],'PARTIAL','fixture')
        old,=EpisodeBuilder(app.store).capture_run(run['id'],pipeline='legacy');eid=EpisodeBuilder(app.store).capture_causal(old)
        owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
        pipe=EvidencePipeline(owner);_,records,_=pipe.build(eid)
        assert 'transform' in records[sid]['definitions']
        _,result=pipe.expand(eid,records,{'evidence_id':created,'selector':'created_content','max_bytes':512},'attempt')
        assert result['truncated'] and len(result['content'].encode())<=512
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('review',['missing','forged','stale','approved'])
async def test_skill_s1_requires_independent_semantic_review(tmp_path,review):
    from types import SimpleNamespace
    from repo_code.capabilities import CapabilityRouter
    from repo_code.skill_validation import SkillValidator
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        report=await owner.curate_episode(eid,provider(lambda v,i:{'status':'COMPLETE','claims':[procedure(v)]}))
        lesson=app.store.get('lesson',report['receipts'][0]['id']);d=report['consolidation'][0]
        ref,contract=CapabilityRouter(owner).contract(d['entity_id'],step_rules=[{'tool':'read','arguments_match':{'path':'demo.py'}},{'tool':'edit','arguments_match':{'path':'demo.py'}}],probe_ids=['p'])
        entry={'experience_id':d['entity_id'],'revision':d['revision'],'revision_digest':d['revision_digest'],
            'scope':contract['scope'],'required_probe_ids':['p'],'step_rules':contract['step_rules']}
        candidate=app.releases.candidate('review-guard',{'skills_index':{'kind':'config','skills':[entry]}},'Only candidate',['DEV'])
        if review!='missing':
            payload={'lesson_id':lesson['id'],'lesson_digest':digest('stale') if review=='stale' else digest(lesson),'decision':'APPROVED'}
            app.store.put('claim_semantic_review',lesson['id'],{'payload':payload,'signature':'forged' if review=='forged' else app.store.sign(payload)})
        verdict=SkillValidator(app,SimpleNamespace(isolated_states=True)).s1(ref,candidate.id,{})
        assert ('SKILL_SEMANTIC_REVIEW_REQUIRED' in verdict['reasons'])==(review!='approved'),verdict
        assert not verdict['passed'] # No registered executable probes, even with semantic approval.
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['claim','request'])
async def test_malformed_items_do_not_lose_valid_claim_or_escape_state_machine(tmp_path,mode):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        p=provider(lambda v,i:{'status':'COMPLETE','claims':['invalid',observe(v)]} if mode=='claim' else {'status':'NEEDS_EVIDENCE','claims':[observe(v)],'evidence_requests':['invalid']})
        r=await owner.curate_episode(eid,p)
        if mode=='claim':assert [x['action'] for x in r['receipts']]==['REJECT','ADD']
        else:assert r['status']=='INCOMPLETE' and r['stop_reason']=='EVIDENCE_REQUEST_SCHEMA'
        assert p.calls==1
    finally:app.close()

@pytest.mark.asyncio
async def test_legacy_ablation_enforces_shared_quote_without_silent_protocol_switch(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path,config={'citation_mode':'legacy_shared','max_expansions':0})
    try:
        def answer(v,i):
            current=observe(v);ref=current['evidence_refs'][0]
            legacy={k:value for k,value in current.items() if k!='evidence_refs'}
            legacy.update(source_ids=[ref['evidence_id']],quote=ref['quote'])
            return {'status':'COMPLETE','claims':[legacy,current]}
        r=await owner.curate_episode(eid,provider(answer))
        assert [x['action'] for x in r['receipts']]==['ADD','REJECT']
    finally:app.close()
