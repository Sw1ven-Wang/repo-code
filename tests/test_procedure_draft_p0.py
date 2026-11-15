"""P0-only contracts/storage checks; no task difficulty or role-selection changes."""
import copy,json
import pytest
from pydantic import ValidationError
from repo_code.capabilities import CapabilityRouter,DraftRuntimeCapabilityContract
from repo_code.memory.causal_curation import ProcedureDraft
from repo_code.memory.models import ClaimRecord
from repo_code.memory.evidence_pipeline import EvidencePipeline
from repo_code.memory.experience import ExperienceConsolidation
from repo_code.util import RepoError,digest
from test_curator_evidence_pipeline import prepared,provider,procedure,observe,emit
from test_elp_experience import approved_fixture
from test_review_workflow import setup
from repo_code.episodes import EpisodeBuilder
from repo_code.memory.consolidation import Consolidator
from repo_code.memory.repository import MemoryRepository
from repo_code.memory.models import MemoryProfile

def draft(payload):
    p=procedure(payload)
    return {'category':'PROCEDURE_DRAFT','text':'Inspect state at expression transformation boundaries','subject':'state propagation',
        'evidence_refs':p['evidence_refs'],'procedure_draft':{'problem_pattern':'Internal state differs despite equal-looking values',
            'applicability':{'scope':payload['draft_context']['scope'],'task_family':payload['draft_context']['task_family'],
                'trigger_hypotheses':['Values compare equal but internal metadata differs'],
                'exclusion_hypotheses':[],'unknown_boundaries':['Only one local task has evidence'],'status':'UNVERIFIED'},
            'steps':['Inspect internal metadata before and after the transformation','Repair the earliest state loss and recheck boundaries'],
            'positive_probe_proposals':[],'negative_probe_proposals':[],'boundary_probe_proposals':[],
            'status':'LOCAL_PROCEDURE_DRAFT'}}

@pytest.mark.asyncio
async def test_natural_draft_empty_exclusions_is_persisted_but_never_runtime_eligible(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        report=await owner.curate_episode(eid,provider(lambda p,i:{'status':'COMPLETE','claims':[draft(p)]}))
        assert report['receipts'][0]['action']=='ADD',report
        obj=app.store.values('procedure_draft')[0]
        assert obj['status']=='LOCAL_PROCEDURE_DRAFT' and not obj['runtime_eligible']
        assert obj['draft']['applicability']['exclusion_hypotheses']==[] and not app.store.values('skill_candidate')
        ref,view=CapabilityRouter(owner).draft_contract(obj['id'])
        assert not view['runtime_eligible'] and view['trigger_predicates']==[] and view['registered_probe_ids']==[]
        with pytest.raises(ValidationError):DraftRuntimeCapabilityContract(**{**view,'runtime_eligible':True})
        d=report['consolidation'][0]
        with pytest.raises(RepoError,match='CAPABILITY_NOT_PROCEDURE'):CapabilityRouter(owner).contract(d['entity_id'])
        # Even a trusted test baseline entry cannot turn draft content into a routable Skill.
        new,facts=approved_fixture(app,owner,run,d)
        hits,trace=owner.retrieve_experiences('state',purpose='runtime',run_id=new['id'],facts=facts)
        trace=owner.repo.get('retrieval_traces',trace)
        assert hits==[] and any(x['reason']=='NOT_SKILL' for x in trace['filters'])
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['scope','family','unknowns','verified'])
async def test_draft_cannot_widen_scope_or_claim_verified_applicability(tmp_path,bad):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        def answer(p,i):
            d=draft(p)
            if bad=='scope':d['procedure_draft']['applicability']['scope']='global'
            if bad=='family':d['procedure_draft']['applicability']['task_family']='every-task'
            if bad=='unknowns':d['procedure_draft']['applicability']['unknown_boundaries']=[]
            if bad=='verified':d['procedure_draft']['applicability']['status']='VERIFIED'
            return {'status':'COMPLETE','claims':[d,observe(p)]}
        r=await owner.curate_episode(eid,provider(answer))
        assert [x['action'] for x in r['receipts']]==['REJECT','ADD'] and not app.store.values('procedure_draft')
    finally:app.close()

@pytest.mark.asyncio
async def test_first_round_lesson_survives_complete_with_only_new_draft(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        def answer(p,i):
            if i==0:return {'status':'NEEDS_EVIDENCE','claims':[observe(p,category='LESSON')],
                'evidence_requests':[{'evidence_id':ids['change'],'selector':'function_definition','symbol':'normalize','max_bytes':1000}]}
            return {'status':'COMPLETE','claims':[draft(p)]}
        p=provider(answer);r=await owner.curate_episode(eid,p)
        assert p.calls==2 and {l['category'] for l in app.store.values('lesson')}=={'LESSON','PROCEDURE_DRAFT'}
        assert len(r['receipts'])==2 and len(app.store.values('procedure_draft'))==1
        assert await owner.curate_episode(eid,p)==r and p.calls==2
    finally:app.close()

@pytest.mark.asyncio
async def test_relations_use_separate_fields_and_counterexample_gets_review_and_revocation(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        def answer(p,i):
            c=procedure(p);c.pop('procedure');c.update(category='HYPOTHESIS',text='State loss may explain this local discrepancy')
            first=next(x for x in p['evidence'] if x['evidence_id']==ids['diagnostic'])
            c['evidence_refs'] += [dict(evidence_id=ids['diagnostic'],selector='stdout_excerpt',source_digest=first['source_digest'],quote='metadata differs',relation='contradicts')]
            other=next(x for x in p['evidence'] if x['evidence_id']==ids['partial'])
            c['evidence_refs'] += [dict(evidence_id=ids['partial'],selector='stdout_excerpt',source_digest=other['source_digest'],quote='2 deselected',relation='context')]
            return {'status':'COMPLETE','claims':[c]}
        r=await owner.curate_episode(eid,provider(answer));lesson=app.store.values('lesson')[0]
        claim=owner.repo.get('memory_versions',lesson['claim_ref'])
        assert [len(claim[k]) for k in ['support_refs','implementation_evidence','verification_evidence','contradiction_refs','context_refs']]==[1,1,1,1,1]
        assert claim['contradiction_refs'][0]['source_id']==ids['diagnostic'] and ids['diagnostic'] not in {e['source_id'] for e in claim['support_refs']}
        assert claim['validity']=='CONTRADICTED' and app.store.values('curation_review')
        record=ExperienceConsolidation(owner).get(r['consolidation'][0]['entity_id']);assert record['counter_refs']
        owner.repo.revoke(ids['diagnostic'])
        with pytest.raises(RepoError):ExperienceConsolidation(owner).get(record['id'])
    finally:app.close()

@pytest.mark.asyncio
async def test_evidence_revision_and_added_counter_not_swallowed_by_noop(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        def answer(p,i):
            first=observe(p,category='HYPOTHESIS');first['text']='A bounded shared claim'
            second=copy.deepcopy(first)
            src=next(s for s in p['evidence'] if s['evidence_id']==ids['diagnostic'])
            second['evidence_refs'] += [dict(evidence_id=ids['diagnostic'],selector='stdout_excerpt',source_digest=src['source_digest'],quote='comparison: "equal"',relation='supports')]
            third=copy.deepcopy(second);third['evidence_refs'][-1]['quote']='metadata differs'
            fourth=copy.deepcopy(third);fourth['evidence_refs'][-1]['relation']='contradicts'
            return {'status':'COMPLETE','claims':[first,second,third,fourth,fourth]}
        report=await owner.curate_episode(eid,provider(answer))
        assert [x['action'] for x in report['receipts']]==['ADD','ADD_SUPPORT','EVIDENCE_REVISION','CONFLICT'],report
        lessons=app.store.values('lesson');assert len(lessons)==4
        last=max(lessons,key=lambda x:x['evidence_revision']);claim=owner.repo.get('memory_versions',last['claim_ref'])
        assert last['evidence_revision']==4 and len({x['semantic_id'] for x in lessons})==1
        assert claim['contradiction_refs'] and len(claim['support_refs'])==2
        assert len(app.store.values('experience_conflict'))>=1
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('limit',[1,10])
async def test_live_batch_conflict_and_capacity_view(tmp_path,limit):
    app,run,old,eid,owner,ids=await prepared(tmp_path)
    try:
        owner.profile=owner.profile.model_copy(update={'max_items':limit})
        def answer(p,i):
            first=observe(p,category='HYPOTHESIS');second=copy.deepcopy(first);second['text']='A different explanation';return {'status':'COMPLETE','claims':[first,second]}
        r=await owner.curate_episode(eid,provider(answer))
        assert [x['action'] for x in r['receipts']]==(['ADD','REJECT'] if limit==1 else ['ADD','CONFLICT'])
        assert len(owner.repo.values('memory_versions'))<=limit
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('snippet,status',[('def only_signature(x):','INCOMPLETE_SOURCE'),('def broken(x):\n return (','INCOMPLETE_SOURCE'),('def complete(x):\n return x\nif pending:','COMPLETE_FUNCTION')])
async def test_function_expansion_never_synthesizes_pass(tmp_path,snippet,status):
    app,run=setup(tmp_path)
    try:
        emit(app,run,'edit',{'path':'example.py','new_text':snippet},{'changed_paths':['example.py']})
        app.runtime.end(run['id'],'PARTIAL','fixture');eid,=EpisodeBuilder(app.store).capture_run(run['id'],pipeline='causal_v2')
        owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
        _,records,_=EvidencePipeline(owner).build(eid);record=next(iter(records.values()))
        assert all('pass' not in d for d in record['definitions'].values())
        if status=='INCOMPLETE_SOURCE':assert not record['definitions'] and json.loads(record['fields']['definition_status'])['status']==status
        else:assert record['definitions']['complete']=='def complete(x):\n return x'
    finally:app.close()

def test_legacy_claim_requires_support_but_v2_counter_only_has_no_positive_support():
    e=dict(source_kind='tool_result',source_id='e',content_digest=digest('e'),run_id='r',scope_id='s',origin_class='HOST_OBSERVATION',visibility_domain='DEV',observed_at=1.0)
    base=dict(claim_id='claim',kind='HYPOTHESIS',text='Local hypothesis',scope_id='s')
    with pytest.raises(ValidationError):ClaimRecord(**base,support_refs=[])
    assert ClaimRecord(**base,evidence_schema_version=2,contradiction_refs=[e]).support_refs==[]
    with pytest.raises(ValidationError):ClaimRecord(**base,evidence_schema_version=2,contradiction_refs=[e],independent_support=['task'])
