"""R4 scoped retrieval, deterministic decisions and atomic immutable revisions."""
import copy,json
import pytest
from repo_code.episodes import EpisodeBuilder
from repo_code.memory.curation import EpisodeCuration
from repo_code.memory.consolidation import Consolidator
from repo_code.memory.repository import MemoryRepository
from repo_code.memory.models import MemoryProfile
from repo_code.memory.retrieval import DynamicRetriever
from repo_code.memory.experience import ExperienceConsolidation
from repo_code.models import Release
from repo_code.util import RepoError,digest
from test_review_workflow import setup,tool
from test_elp_curator import provider,claim,PROCEDURE

async def initial(tmp_path):
    app,run=setup(tmp_path)
    owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
    return app,run,owner

async def learn(app,owner,run=None,*,goal='Independent task A',change=None,claim_updates=None,consolidate=True,family='editing-boundary'):
    if run is None:run=app.new_task(goal,app.store.root.parent/'source',authorize_write=True,authorize_process=True)
    with app.store.tx() as c:
        current=app.runtime.run(run['id'],c);current['data']['manifest']['task_family']=family
        app.runtime.update(c,current,data=current['data'])
    await tool(app,run,'create',{'path':'fixed.py','content':'value=1'},'write')
    with app.store.tx() as c:
        app.store.emit(c,run['id'],'tool.operation_finished',{'tool_name':'process.run','result':{
            'exit_code':1,'stderr':'AssertionError RAW_TRACE_NEVER_VECTORIZE'}},'DEV')
    app.runtime.end(run['id'],'PARTIAL','boundary failed')
    eid,=EpisodeBuilder(app.store).capture_run(run['id'])
    def output(payload,i):
        value=claim(payload);value['text']='RETENTION_RULE preserve counterexample through repair'
        if change:change(value)
        return {'status':'COMPLETE','claims':[value]}
    result=await EpisodeCuration(owner).run(eid,provider(output));lid=result['receipts'][0]['id']
    if claim_updates:
        lesson=app.store.get('lesson',lid);value=owner.repo.get('memory_versions',lesson['claim_ref']);value.update(claim_updates)
        owner.repo.put('memory_versions',lesson['claim_ref'],value,immutable=False)
    decision=owner.consolidate_lesson(lid) if consolidate else None
    return run,lid,decision

class Encoder:
    identity='E0-instrumented';seconds=0
    def __init__(self):self.embeds=[];self.reranks=[];self.on_rank=None
    def embed(self,texts,query=False):self.embeds.append((texts,query));return [[1.,0.] for _ in texts]
    def rerank(self,query,texts):
        self.reranks.append(texts)
        if self.on_rank:self.on_rank()
        return [1. for _ in texts]

def hybrid(owner):
    encoder=Encoder();owner.profile=owner.profile.model_copy(update={'mode':'hybrid_semantic_strict'})
    owner.retriever=DynamicRetriever(owner.repo,owner.profile,encoder)
    return encoder

def approved_fixture(app,owner,source_run,decision,*,goal='Review the same task',family='editing-boundary',**overrides):
    """Trusted operator-baseline fixture only; never activates a candidate Release."""
    record=ExperienceConsolidation(owner).get(decision['entity_id'])
    entry={'experience_id':record['id'],'revision':record['revision'],'revision_digest':record['revision_digest'],
        'scope':record['conditions']['scope'],'task_family':family,'status':'ACTIVE','phases':['DEBUG'],**overrides}
    base=app.releases.get(app.releases.active()['digest']);components={**base.components,
        'skills_index':app.store.json_blob({'kind':'config','skills':[entry]})}
    release=Release(components=components,base_digest=None,worker_build_digest=base.worker_build_digest,
        dependencies_digest=base.dependencies_digest,gate_policy_digest=base.gate_policy_digest,
        build_attestation_digest=app.store.json_blob({'type':'operator_baseline_fixture','components':components}))
    app.store.put('release',release.digest,release,immutable=True)
    run=app.runtime.create(goal,app.store.root.parent/'source',release.digest,
        task_id=source_run['task_id'],task_revision=2,expected_active=source_run['id'])
    with app.store.tx() as c:
        current=app.runtime.run(run['id'],c);current['data']['manifest']['task_family']=family
        app.runtime.update(c,current,data=current['data'])
    facts={**{v:True for v in PROCEDURE['triggers']},**{v:False for v in PROCEDURE['exclusions']}}
    return run,facts

@pytest.mark.asyncio
async def test_verified_mechanism_recalls_one_approved_revision_but_never_overrides_exclusions(tmp_path):
    app,run,owner=await initial(tmp_path)
    family='mechanism:once';key='task_family:'+family
    try:
        def update(v):
            v['procedure'].update(triggers=[key],exclusions=['run_cancelled'])
        source,_,decision=await learn(app,owner,run,change=update,family=family)
        current,_=approved_fixture(app,owner,source,decision,family=family)
        for cancelled in (False,True):
            hits,tid=owner.retrieve_experiences('zyxwvu',purpose='runtime',run_id=current['id'],
                facts={key:True,'run_cancelled':cancelled},match_only=True)
            trace=owner.repo.get('retrieval_traces',tid)
            assert bool(hits)==(not cancelled)
            assert trace.get('host_mechanism_recall')==(None if cancelled else key)
        assert not app.store.values('application_receipt')
    finally:app.close()

@pytest.mark.asyncio
async def test_exact_duplicate_adds_support_without_copying_canonical_skill(tmp_path):
    app,run,owner=await initial(tmp_path)
    try:
        a,lid,first=await learn(app,owner,run)
        b,lid2,second=await learn(app,owner,goal='Independent task B')
        assert first['action']=='NEW' and second['action']=='ADD_SUPPORT'
        assert first['entity_id']==second['entity_id'] and second['revision']==2
        engine=ExperienceConsolidation(owner);old=engine.get(first['entity_id'],1);new=engine.get(first['entity_id'])
        assert len(old['support'])==1 and len(new['support'])==2
        assert len(app.store.values('experience_head'))==1
        assert new['independent_family_count']==1 and new['independent_validation_count']==0
        assert owner.consolidate_lesson(lid2)==second
        assert len(app.store.values('experience_revision'))==2
    finally:app.close()

@pytest.mark.asyncio
async def test_resampled_same_public_task_is_noop_despite_new_task_id(tmp_path):
    app,run,owner=await initial(tmp_path)
    try:
        app.runtime.end(run['id'],'PARTIAL','unused')
        _,_,a=await learn(app,owner,goal='same public issue')
        _,_,b=await learn(app,owner,goal='same public issue')
        assert b['action']=='NOOP' and a['entity_id']==b['entity_id'] and b['revision']==1
    finally:app.close()

@pytest.mark.asyncio
async def test_different_conditions_split_and_conflicting_method_requires_review(tmp_path):
    app,run,owner=await initial(tmp_path)
    try:
        _,_,a=await learn(app,owner,run)
        def condition(v):v['procedure']['triggers']=['Only when the producer invariant is visible']
        _,_,b=await learn(app,owner,goal='B',change=condition)
        def opposite(v):v['procedure']['steps']=['Preserve the current implementation and inspect the consumer first']
        _,_,d=await learn(app,owner,goal='C',change=opposite)
        assert b['action']=='SPLIT' and d['action']=='CONFLICT_REVIEW'
        assert len(app.store.values('experience_head'))==3
        assert app.store.values('experience_conflict')
        assert all(r['automatic_merge'] is False for r in owner.repo.values('memory_relations'))
    finally:app.close()

@pytest.mark.asyncio
async def test_explicit_host_revision_is_cas_bound_and_preserves_previous(tmp_path):
    app,run,owner=await initial(tmp_path)
    try:
        _,_,a=await learn(app,owner,run)
        _,lid,_=await learn(app,owner,goal='B',change=lambda v:v.update(text='RETENTION_RULE revised recovery procedure'),consolidate=False)
        with pytest.raises(RepoError,match='EXPERIENCE_HEAD_CAS'):owner.consolidate_lesson(lid,target_id=a['entity_id'],expected_revision=0)
        b=owner.consolidate_lesson(lid,target_id=a['entity_id'],expected_revision=1)
        assert b['action']=='REVISE' and b['revision']==2
        history=ExperienceConsolidation(owner)
        assert history.get(a['entity_id'],1)['content']['text']!=history.get(a['entity_id'],2)['content']['text']
        assert app.releases.active()['revision']==0
    finally:app.close()

@pytest.mark.asyncio
async def test_consolidation_crash_rolls_back_head_version_and_receipt(tmp_path,monkeypatch):
    app,run,owner=await initial(tmp_path)
    try:
        _,_,a=await learn(app,owner,run)
        _,lid,_=await learn(app,owner,goal='B',consolidate=False)
        original=app.store.put
        def crash(kind,*args,**kwargs):
            value=original(kind,*args,**kwargs)
            if kind=='experience_head':raise RuntimeError('Injected crash before receipt')
            return value
        monkeypatch.setattr(app.store,'put',crash)
        with pytest.raises(RuntimeError):owner.consolidate_lesson(lid)
        assert ExperienceConsolidation(owner).get(a['entity_id'])['revision']==1
        monkeypatch.setattr(app.store,'put',original)
        result=owner.consolidate_lesson(lid)
        assert result['revision']==2 and len(app.store.values('experience_revision'))==2
    finally:app.close()

@pytest.mark.asyncio
async def test_graded_retrieval_exact_skips_dense_semantic_uses_it_and_metrics(tmp_path):
    app,run,owner=await initial(tmp_path)
    try:
        _,_,a=await learn(app,owner,run)
        encoder=hybrid(owner)
        hits,tid=owner.retrieve_experiences('RETENTION_RULE',purpose='consolidation',scope=run['data']['manifest']['scope'])
        trace=owner.repo.get('retrieval_traces',tid)
        assert hits and not encoder.embeds and trace['dense_calls']==0 and trace['reranker_calls']==1
        hits,tid=owner.retrieve_experiences('producer invariant diagnosis',purpose='consolidation',scope=run['data']['manifest']['scope'],semantic=True)
        trace=owner.repo.get('retrieval_traces',tid)
        assert hits and trace['dense_calls']==2 and 0<trace['reranker_items']<=20
        assert trace['selected_tokens']>0 and trace['injected_tokens']==0 and trace['index_build_seconds']>=0
        assert all('RAW_TRACE_NEVER_VECTORIZE' not in text for texts,_ in encoder.embeds for text in texts)
        assert ExperienceConsolidation(owner).get(a['entity_id'])['revision']==1  # Rank does not consolidate.
    finally:app.close()

@pytest.mark.asyncio
async def test_only_published_bound_skills_with_true_conditions_return_runtime(tmp_path):
    app,run,owner=await initial(tmp_path)
    try:
        source,_,a=await learn(app,owner,run)
        current,facts=approved_fixture(app,owner,source,a)
        hits,tid=owner.retrieve_experiences('RETENTION_RULE',purpose='runtime',run_id=current['id'],facts=facts)
        assert len(hits)==1 and hits[0]['status']=='ACTIVE' and hits[0]['applicability']=='TRUE'
        assert owner.repo.get('retrieval_traces',tid)['purpose']=='runtime'
        assert app.releases.active()['revision']==0  # No candidate publication was performed.
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['unknown-trigger','unknown-exclusion','expired','revision','scope','phase','family','unsupported-task','unpublished'])
async def test_runtime_filters_before_encoders(tmp_path,fault):
    app,run,owner=await initial(tmp_path)
    try:
        source,_,a=await learn(app,owner,run)
        changes={'valid_until':0} if fault=='expired' else {'revision':99} if fault=='revision' else {'scope':'other'} if fault=='scope' else {}
        current,facts=approved_fixture(app,owner,source,a,**changes)
        if fault=='unknown-trigger':facts.pop(PROCEDURE['triggers'][0])
        if fault=='unknown-exclusion':facts.pop(PROCEDURE['exclusions'][0])
        if fault in {'family','unsupported-task','unpublished'}:
            candidate=app.releases.candidate('fixture',{'skills_index':app.releases.components(current['data']['manifest']['release_digest'])['skills_index']},'must not enter runtime',['fixture']) if fault=='unpublished' else None
            with app.store.tx() as c:
                r=app.runtime.run(current['id'],c)
                if fault=='family':r['data']['manifest']['task_family']='other'
                if fault=='unpublished':
                    r['data']['manifest']['release_digest']=candidate.candidate_release_digest
                if fault=='unsupported-task':c.execute('UPDATE runs SET task_id=? WHERE id=?',('different-task',current['id']))
                app.runtime.update(c,r,data=r['data'])
        encoder=hybrid(owner)
        hits,tid=owner.retrieve_experiences('RETENTION_RULE',purpose='runtime',run_id=current['id'],facts=facts,phase='PLAN' if fault=='phase' else 'DEBUG')
        assert hits==[] and not encoder.embeds and not encoder.reranks
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('restriction',[{'preconditions':{'python':'3.13'}},{'file_dependencies':{'fixed.py':'old-version'}}])
async def test_dependency_unknown_or_stale_never_becomes_true(tmp_path,restriction):
    app,run,owner=await initial(tmp_path)
    try:
        source,_,a=await learn(app,owner,run,claim_updates=restriction)
        current,facts=approved_fixture(app,owner,source,a)
        hits,_=owner.retrieve_experiences('RETENTION_RULE',purpose='runtime',run_id=current['id'],facts=facts,file_versions={'fixed.py':'new-version'})
        assert not hits
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['revoke','hidden','tamper','during-rank'])
async def test_invalid_source_never_reaches_or_returns_from_ranker(tmp_path,fault):
    app,run,owner=await initial(tmp_path)
    try:
        source,lid,a=await learn(app,owner,run);encoder=hybrid(owner)
        current,facts=approved_fixture(app,owner,source,a)
        if fault=='revoke':owner.repo.revoke(lid)
        if fault=='hidden':
            from repo_code.governance import Lineage
            Lineage(app.store).expose(lid,'final','protected-family')
        if fault=='tamper':
            with app.store.tx() as c:
                lesson=app.store.get('lesson',lid,c)
                event=lesson['source_ids'][0]
                c.execute('UPDATE events SET data=? WHERE event_id=?',('{"tampered":true}',event))
        if fault=='during-rank':encoder.on_rank=lambda:owner.repo.revoke(lid)
        hits,_=owner.retrieve_experiences('RETENTION_RULE',purpose='runtime',run_id=current['id'],facts=facts)
        assert not hits
        if fault!='during-rank':assert not encoder.reranks
    finally:app.close()

@pytest.mark.asyncio
async def test_empty_unrelated_and_zero_budget_do_not_force_hits(tmp_path):
    app,run,owner=await initial(tmp_path)
    try:
        await learn(app,owner,run)
        for query,opts in [('',{}),('tropical volcanic mineralogy',{}),('RETENTION_RULE',{'token_budget':0}),('RETENTION_RULE',{'limit':0})]:
            hits,_=owner.retrieve_experiences(query,purpose='consolidation',scope=run['data']['manifest']['scope'],**opts)
            assert not hits
    finally:app.close()

@pytest.mark.asyncio
async def test_concurrent_support_commit_is_idempotent_and_survives_reopen(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    app,run,owner=await initial(tmp_path)
    try:
        _,_,a=await learn(app,owner,run)
        _,lid,_=await learn(app,owner,goal='Concurrent independent task',consolidate=False)
        with ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(lambda _:owner.consolidate_lesson(lid),range(2)))
        assert rows[0]==rows[1] and len(app.store.values('experience_head'))==1
        reopened=Consolidator(MemoryRepository(app.store),app.runtime,owner.profile)
        assert ExperienceConsolidation(reopened).get(a['entity_id'])['revision']==2
    finally:app.close()

@pytest.mark.asyncio
async def test_new_counterevidence_is_reviewed_and_existing_active_claim_is_preserved(tmp_path):
    app,run,owner=await initial(tmp_path)
    try:
        _,lid,a=await learn(app,owner,run)
        first=ExperienceConsolidation(owner).get(a['entity_id']);backing=owner.repo.get('memory_versions',first['claim_ref'])
        owner.repo.put('memory_control','claim:'+backing['claim_id'],{'version':1,'lifecycle':'ACTIVE'},immutable=False)
        _,_,b=await learn(app,owner,goal='Additional independent source')
        assert b['action']=='ADD_SUPPORT'
        assert owner.repo.get('memory_control','claim:'+backing['claim_id'])=={'version':1,'lifecycle':'ACTIVE'}
        _,other,_=await learn(app,owner,goal='Counterexample source',consolidate=False)
        source=app.store.get('lesson',other);value=owner.repo.get('memory_versions',source['claim_ref'])
        value['contradiction_refs']=value['support_refs']
        owner.repo.put('memory_versions',source['claim_ref'],value,immutable=False)
        conflict=owner.consolidate_lesson(other)
        assert conflict['action']=='CONFLICT_REVIEW'
        assert ExperienceConsolidation(owner).get(conflict['entity_id'])['counter_refs']==[other]
    finally:app.close()

@pytest.mark.asyncio
async def test_growth_rejection_has_no_partial_revision(tmp_path):
    app,run,owner=await initial(tmp_path)
    try:
        _,_,a=await learn(app,owner,run)
        _,lid,_=await learn(app,owner,goal='Independent task B',consolidate=False)
        owner.profile=owner.profile.model_copy(update={'max_items':2})
        with pytest.raises(RepoError,match='MEMORY_GROWTH_BUDGET'):owner.consolidate_lesson(lid)
        assert ExperienceConsolidation(owner).get(a['entity_id'])['revision']==1
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('change',['context','release','conflict'])
async def test_runtime_rechecks_context_and_release_after_ranking(tmp_path,change):
    app,run,owner=await initial(tmp_path)
    try:
        source,_,a=await learn(app,owner,run);current,facts=approved_fixture(app,owner,source,a)
        encoder=hybrid(owner)
        def changed():
            if change=='release':owner.repo.revoke(current['data']['manifest']['release_digest'])
            elif change=='conflict':app.store.put('experience_conflict','late-conflict',{'entity_id':a['entity_id'],'status':'NEEDS_REVIEW'})
            else:
                with app.store.tx() as c:app.runtime.update(c,app.runtime.run(current['id'],c),writer=True)
        encoder.on_rank=changed
        hits,tid=owner.retrieve_experiences('RETENTION_RULE',purpose='runtime',run_id=current['id'],facts=facts)
        trace=owner.repo.get('retrieval_traces',tid)
        assert not hits and trace['selected_tokens']==0 and trace['injected_tokens']==0
    finally:app.close()


def test_reranker_top20_bound_and_no_raw_trace_or_forced_merge(tmp_path):
    app,run=setup(tmp_path)
    try:
        repo=MemoryRepository(app.store);profile=MemoryProfile(mode='hybrid_semantic_strict',evidence_tokens=8000)
        encoder=Encoder();retriever=DynamicRetriever(repo,profile,encoder)
        rows=[{'id':str(i),'text':'RETENTION_RULE keep focused evidence '+str(i)} for i in range(35)]
        hits,trace=retriever.rank_experiences(rows,'RETENTION_RULE',limit=20,token_budget=8000)
        assert hits and len(encoder.reranks[0])==20 and trace['reranker_items']==20
        assert trace['automatic_merge'] is False and trace['dense_calls']==0
    finally:app.close()

@pytest.mark.asyncio
async def test_strict_semantic_missing_encoder_fails_with_honest_audit(tmp_path):
    app,run,owner=await initial(tmp_path)
    try:
        await learn(app,owner,run)
        owner.profile=owner.profile.model_copy(update={'mode':'hybrid_semantic_strict'})
        with pytest.raises(RepoError,match='SEMANTIC_MODELS_UNAVAILABLE'):
            owner.retrieve_experiences('producer invariant diagnosis',purpose='consolidation',scope=run['data']['manifest']['scope'],semantic=True)
        audit=owner.repo.values('retrieval_traces')[-1]
        assert audit['cost_status']=='PARTIAL_FAILURE' and audit['reranker_calls'] is None
    finally:app.close()

@pytest.mark.asyncio
async def test_runtime_pins_approved_revision_while_support_candidate_advances(tmp_path):
    app,run,owner=await initial(tmp_path)
    try:
        source,_,a=await learn(app,owner,run)
        current,facts=approved_fixture(app,owner,source,a)
        # Finish the fixture Run so the existing Curator queue can process B.
        app.runtime.end(current['id'],'PARTIAL','offline retrieval fixture')
        _,_,b=await learn(app,owner,goal='New independent support')
        assert b['revision']==2
        hits,_=owner.retrieve_experiences('RETENTION_RULE',purpose='runtime',run_id=current['id'],facts=facts)
        assert len(hits)==1 and hits[0]['revision']==1
        assert hits[0]['revision_digest']==a['revision_digest']
    finally:app.close()

@pytest.mark.parametrize('options',[{'limit':-1},{'limit':True},{'token_budget':-1},{'token_budget':False}])
def test_ranker_rejects_invalid_policy_limits(tmp_path,options):
    app,run=setup(tmp_path)
    try:
        retriever=DynamicRetriever(MemoryRepository(app.store),MemoryProfile(mode='lexical_only'))
        with pytest.raises(RepoError,match='INVALID_MEMORY_POLICY'):retriever.rank_experiences([],'query',**options)
    finally:app.close()

@pytest.mark.asyncio
async def test_memory_service_exposes_retrieval_without_offering_candidates(tmp_path):
    app,run=setup(tmp_path,memory=True)
    try:
        hits,tid=await app.memory.search_capabilities(run['id'],'RETENTION_RULE',facts={})
        assert not hits and app.provider.calls==0
        assert app.memory.repo.get('retrieval_traces',tid)['purpose']=='runtime'
        app.memory.disable('fixture shutdown')
        with pytest.raises(RepoError,match='MEMORY_PROFILE_DISABLED'):
            await app.memory.search_capabilities(run['id'],'RETENTION_RULE')
    finally:app.close()

@pytest.mark.parametrize('text',['x'*7000,chr(0)*4000+'x'*3000])
def test_large_dense_recall_respects_actual_worker_request_envelope(tmp_path,text):
    app,run=setup(tmp_path)
    class BoundedEncoder(Encoder):
        def embed(self,texts,query=False):
            wire=json.dumps({'operation':'embed','texts':[('query: ' if query else 'passage: ')+v for v in texts],'query':''},ensure_ascii=False)
            assert len(texts)<=64 and len(wire)<256000
            return super().embed(texts,query)
        def rerank(self,query,texts):
            wire=json.dumps({'operation':'rerank','texts':texts,'query':query},ensure_ascii=False)
            assert len(texts)<=64 and len(wire)<256000
            return super().rerank(query,texts)
    try:
        repo=MemoryRepository(app.store);profile=MemoryProfile(mode='hybrid_semantic_strict')
        encoder=BoundedEncoder();retriever=DynamicRetriever(repo,profile,encoder)
        rows=[{'id':str(i),'text':text} for i in range(70)]
        _,trace=retriever.rank_experiences(rows,'unmatched semantic query',semantic=True)
        assert len(encoder.embeds)>2 and trace['dense_calls']==len(encoder.embeds)
        assert trace['reranker_calls']==len(encoder.reranks) and trace['reranker_items']<=20
    finally:app.close()
