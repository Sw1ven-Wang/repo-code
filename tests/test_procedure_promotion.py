"""Native coordinator tests. Scripted fixtures prove integration, never benefit."""
import copy
import json
from types import SimpleNamespace
import pytest
from repo_code.procedure_promotion import ProcedurePromotion
from repo_code.providers import ScriptedProvider
from repo_code.models import Assistant, Contract, Requirement, Check
from repo_code.acceptance import freeze_contract
from repo_code.util import RepoError, ProviderNotDispatched, digest
from test_curator_evidence_pipeline import emit, provider
from test_procedure_draft_p0 import draft
from test_review_workflow import setup
from repo_code.episodes import EpisodeBuilder
from repo_code.memory.consolidation import Consolidator
from repo_code.memory.repository import MemoryRepository
from repo_code.memory.models import MemoryProfile


async def fixture(tmp_path):
    app, run = setup(tmp_path)
    with app.store.tx() as c:
        current=app.runtime.run(run['id'],c)
        current['data']['manifest']['task_family']='precision-sensitive'
        app.runtime.update(c,current,data=current['data'])
    emit(app,run,'process.run',{'argv':['python','-c','import demo; print(demo.normalize(1))']},{'exit_code':0,'stdout':'old state differs\n'})
    emit(app,run,'process.run',{'argv':['python','-c','import demo; print(demo.normalize(2))']},{'exit_code':0,'stdout':'comparison: "equal"\nmetadata differs\n'})
    emit(app,run,'edit',{'path':'demo.py','old_text':'def normalize(x): return x','new_text':'def normalize(x):\n    return x + 1\n'},{'changed_paths':['demo.py']})
    emit(app,run,'create',{'path':'test_demo.py','content':'from demo import normalize\ndef test_boundary():\n    assert normalize(0) == 1\n'},{'changed_paths':['test_demo.py'],'versions':{'test_demo.py':digest('test')}})
    emit(app,run,'process.run',{'argv':['python','-m','pytest','test_demo.py']},{'exit_code':0,'stdout':'2 passed\n'})
    emit(app,run,'process.run',{'argv':['python','-m','pytest','test_demo.py','-k','one']},{'exit_code':0,'stdout':'1 passed, 2 deselected\n'})
    app.runtime.end(run['id'],'PARTIAL','Synthetic protocol integration fixture')
    old,=EpisodeBuilder(app.store).capture_run(run['id'],pipeline='legacy')
    eid=EpisodeBuilder(app.store).capture_causal(old)
    app.store.put('operator_config','curator_evidence_pipeline',{'enabled':True})
    owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
    report=await owner.curate_episode(eid, provider(lambda p, i: {'status': 'COMPLETE', 'claims': [draft(p)]}))
    assert app.store.values('procedure_draft'),report
    d = app.store.values('procedure_draft')[0]
    contract = freeze_contract(Contract(requirements=[Requirement(id='r', description='Return the input')],
        checks=[Check(id=n, kind='function', definition={'module':'main','function':'answer',
                 'cases':[{'id':n,'args':[v],'expected':v}]}) for n,v in [('p',1),('n',-1),('b',0)]],
        required_checks=['p','n','b'], coverage={'r':['p','n','b']}), 'test-curator')
    task = {'id':'support', 'goal':'Preserve state through a transform.',
            'files':{'main.py':'def answer(x): return x\n'}, 'contract':contract.model_dump(mode='json'),
            'budget':{'model_calls':8,'tool_calls':20,'wall_seconds':120},
            'capability_context':{'scope':d['scope'],'task_family':d['task_family']}}
    probes = [{'probe_id':n,'kind':k,'task_id':task['id'],'check_id':n,
               'expected_behavior':'Correct transformation without changing numerical equality',
               'source_refs':[d['id']],'environment_profile':app.sandbox.environment_digest}
              for n,k in [('p','POSITIVE'),('n','NEGATIVE'),('b','BOUNDARY')]]
    plan = {'title':'Inspect and repair metadata preservation without changing numerical equality',
            'procedure':{'triggers':['task_family:'+d['task_family']], 'exclusions':['run_cancelled'],
                'steps':['Inspect transformation metadata'], 'expected_output':'Metadata preserved',
                'necessary_checks':['Original failure and adjacent counterexamples'],
                'counterexamples_or_unknowns':['Independent transfer remains unverified']},
            'step_rules':[{'tool':'read','arguments_match':{'path':'main.py'}}]}
    quotes = app.store.get('lesson', d['lesson_ref'])['quote_bindings']
    review = {'decision':'APPROVED','rationale':'Evidence supports only a local experiment, not publication',
              'quote_ids':[q['quote_id'] for q in quotes], 'limitations':['No unseen efficacy proof']}
    return app, d, task, probes, plan, review


def model(value):
    return ScriptedProvider([Assistant(text=json.dumps(value), usage={'input_tokens':100,'output_tokens':50})])


class Runner:
    isolated_states = True
    def __init__(self, failed=False): self.calls=0; self.failed=failed
    async def run(self, task, release, cohort, **kwargs):
        self.calls += 1
        return {'task_id':task['id'],'state_root':str(self.calls),'manifest':{},'state':'SUCCEEDED',
            'passed':not self.failed,'provider_kind':'scripted_test_double',
            'executed_check_ids':['p','n','b'],'passed_check_ids':['p','n','b'],
            'capability_applied':True,'capability_offered':task.get('skill_role')!='NEGATIVE',
            'cost':{'model_calls':1,'tool_calls':1},'usage':{'input_tokens':100,'output_tokens':10,'usage_available':True}}


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['APPROVED', 'REJECTED'])
async def test_binding_protocol_visible_to_both_review_stages_without_tool_authority(tmp_path, decision):
    from repo_code.tool_models import definitions
    app,d,t,probes,plan,review = await fixture(tmp_path)
    seen = {}
    plan['binding_slots'] = {'implementation': {'kind': 'path', 'purpose': 'Locate the current reconstruction boundary'}}
    plan['step_rules'] = [{'tool': 'read', 'arguments_match': {'path': {'slot': 'implementation'}}}]
    def inspect(context, stage):
        seen[stage] = context['live_tool_definitions']
        actual = {x['name']: x for x in seen[stage]}
        assert actual['skill.bind'] == definitions(['skill.bind'])[0]
        assert {'read', 'edit', 'process.run', 'workspace.diff'} <= actual.keys()
    def planner(payload, index):
        inspect(payload, 'planner')
        return plan
    def reviewer(payload, index):
        inspect(payload['source'], 'reviewer')
        return {**review, 'decision': decision}
    try:
        active = app.releases.active()
        runner = Runner()
        # provider() also asserts no executable tools are granted to either model.
        result = await app.procedure_promotion(runner).run(
            d['id'], provider(planner), provider(reviewer), support_tasks={t['id']: t}, probes=probes)
        assert set(seen) == {'planner', 'reviewer'}, result
        assert seen['planner'] == seen['reviewer']
        expected = 'INDEPENDENT_TRANSFER_SUITE_REQUIRED' if decision == 'APPROVED' else 'SEMANTIC_REJECTED'
        assert result['reason'] == expected, result
        assert runner.calls == (2 if decision == 'APPROVED' else 0)
        assert app.releases.active() == active and not result['published']
        assert not app.store.values('release_approval')
    finally:
        app.close()


@pytest.mark.asyncio
async def test_native_candidate_reuses_validator_and_does_not_publish_without_transfer(tmp_path):
    app,d,t,probes,plan,review = await fixture(tmp_path)
    try:
        before=copy.deepcopy(d);active=app.releases.active(); runner=Runner()
        flow=app.procedure_promotion(runner)
        result=await flow.run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=probes)
        assert result['status']=='BLOCKED' and result['reason']=='INDEPENDENT_TRANSFER_SUITE_REQUIRED',result
        assert runner.calls==2 and app.store.get('skill_s2',digest([result['contract_ref'],result['candidate']]))['passed']
        assert app.store.get('procedure_draft',d['id'])==before and app.releases.active()==active
        lesson=app.store.get('lesson',app.store.get('promotion_materialization',result['job'])['lesson_id'])
        assert lesson['category']=='PROCEDURE' and lesson['source_draft_ref']==d['id']
        assert app.store.get('claim_semantic_review',lesson['id'])['payload']['lesson_digest']==digest(lesson)
        with pytest.raises(RepoError,match='SKILL_QUALIFICATION_REQUIRED|ISOLATION_REQUIRED'):app.releases.activate(result['candidate'])
        # Completed paid stages remain cached across a new coordinator instance.
        again=await app.procedure_promotion(runner).run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=probes)
        assert again==result and runner.calls==2
    finally: app.close()


@pytest.mark.asyncio
async def test_context_revision_keeps_old_rejection_and_requires_fresh_review(tmp_path):
    app,d,t,probes,plan,review = await fixture(tmp_path)
    try:
        runner = Runner()
        old_flow = app.procedure_promotion(runner)
        old_flow.REVISION = 'procedure-promotion-v1.2-portable-binding'
        rejected = await old_flow.run(d['id'], model(plan), model({**review, 'decision': 'REJECTED'}),
                                      support_tasks={t['id']: t}, probes=probes)
        planner, reviewer = model(plan), model(review)
        result = await app.procedure_promotion(runner).run(
            d['id'], planner, reviewer, support_tasks={t['id']: t}, probes=probes)
        assert result['job'] != rejected['job']
        assert planner.calls == reviewer.calls == 1
        assert result['reason'] == 'INDEPENDENT_TRANSFER_SUITE_REQUIRED'
        assert app.store.get('procedure_promotion', rejected['job']) == rejected
        assert not result['published'] and not app.store.values('release_approval')
    finally:
        app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['condition','step','citation','coverage','review','duplicate_probe','probe_task','probe_source','missing_probe'])
async def test_rejections_preserve_draft_and_active_release(tmp_path,bad):
    app,d,t,probes,plan,review = await fixture(tmp_path)
    try:
        active=app.releases.active(); runner=Runner()
        if bad=='condition':plan['procedure']['triggers']=['invented_fact']
        if bad=='step':plan['procedure']['steps'].append('Unbound step')
        if bad=='citation':review['quote_ids']=['invented']
        if bad=='coverage':review['quote_ids']=review['quote_ids'][:1]
        if bad=='review':review['decision']='INSUFFICIENT_EVIDENCE'
        if bad=='duplicate_probe':probes.append(probes[0])
        if bad=='probe_task':probes[0]['task_id']='missing'
        if bad=='probe_source':probes[0]['source_refs']=['nonexistent']
        if bad=='missing_probe':probes=[]
        result=await app.procedure_promotion(runner).run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=probes)
        assert not result['published'] and result['status'] in {'BLOCKED','REJECTED'},result
        assert app.releases.active()==active and runner.calls==0
        assert not app.store.values('skill_qualification') and not app.store.values('release_approval')
    finally:app.close()


@pytest.mark.asyncio
async def test_failed_support_stops_before_transfer_or_release(tmp_path):
    app,d,t,p,plan,review=await fixture(tmp_path)
    try:
        runner=Runner(failed=True)
        r=await app.procedure_promotion(runner).run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=p,transfer_tasks=[{}])
        assert r['reason']=='S2_REJECTED' and runner.calls==2
        assert not app.store.values('skill_s3') and not app.store.values('gate')
    finally:app.close()


@pytest.mark.asyncio
async def test_unknown_dispatch_not_reissued_after_restart(tmp_path):
    app,d,t,p,plan,review=await fixture(tmp_path)
    try:
        flow=app.procedure_promotion(Runner()); planner=ScriptedProvider([TimeoutError('after dispatch')])
        r=await flow.run(d['id'],planner,model(review),support_tasks={t['id']:t},probes=p)
        assert r['status']=='BLOCKED' and planner.calls==1
        fresh=model(plan)
        r2=await app.procedure_promotion(Runner()).run(d['id'],fresh,model(review),support_tasks={t['id']:t},probes=p)
        assert r2['reason']=='PROMOTION_STAGE_RECONCILIATION_REQUIRED' and fresh.calls==0
        with pytest.raises(RepoError,match='PROMOTION_RETRY_DENIED'):flow.retry_undispatched(r['job'],'PLAN')
    finally:app.close()


@pytest.mark.asyncio
async def test_proven_undispatched_can_resume_without_replaying_completed_stage(tmp_path):
    app,d,t,p,plan,review=await fixture(tmp_path)
    try:
        flow=app.procedure_promotion(Runner())
        planner=ScriptedProvider([ProviderNotDispatched('HOST_CAPACITY')])
        r=await flow.run(d['id'],planner,model(review),support_tasks={t['id']:t},probes=p)
        flow.retry_undispatched(r['job'],'PLAN')
        r2=await flow.run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=p)
        assert r2['reason']=='INDEPENDENT_TRANSFER_SUITE_REQUIRED'
        assert len(app.store.values('procedure_promotion_recovery'))==1
    finally:app.close()


@pytest.mark.asyncio
async def test_source_revocation_denied_even_with_cached_plan(tmp_path):
    app,d,t,p,plan,review=await fixture(tmp_path)
    try:
        flow=app.procedure_promotion(Runner())
        flow.owner.repo.revoke(d['id'])
        with pytest.raises(RepoError):await flow.run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=p)
    finally:app.close()


@pytest.mark.asyncio
async def test_same_provider_instance_cannot_review_its_own_plan(tmp_path):
    app,d,t,p,plan,review=await fixture(tmp_path)
    try:
        m=model(plan)
        with pytest.raises(RepoError,match='PROMOTION_INDEPENDENT_REVIEW_REQUIRED'):
            await app.procedure_promotion(Runner()).run(d['id'],m,m,support_tasks={t['id']:t},probes=p)
        assert m.calls==0
    finally:app.close()


@pytest.mark.asyncio
async def test_full_independent_fixture_suite_remains_integration_only(tmp_path):
    app,d,t,p,plan,review=await fixture(tmp_path)
    try:
        unseen=[]
        for i,role in enumerate(['CORE','CORE','CORE','TRANSFER','NEGATIVE']):
            v=copy.deepcopy(t);v['id']='independent-'+str(i);v['skill_role']=role
            # Fixture implementations are distinct; no real-model benefit claim.
            v['files']['main.py']='def answer(x): return x + '+str(i+1)+'\n'
            if role=='NEGATIVE':v['capability_context']['task_family']='different'
            unseen.append(v)
        runner=Runner()
        r=await app.procedure_promotion(runner).run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=p,transfer_tasks=unseen)
        assert r['reason']=='S3_REJECTED_OR_INTEGRATION_ONLY' and runner.calls==12,r
        assert r['s3']['evidence_class']=='INTEGRATION_ONLY'
        assert not app.store.values('skill_qualification') and not app.store.values('gate')
    finally:app.close()


@pytest.mark.asyncio
async def test_changed_task_inputs_cannot_reuse_paid_stage_cache(tmp_path):
    app,d,t,p,plan,review=await fixture(tmp_path)
    try:
        flow=app.procedure_promotion(Runner())
        a=await flow.run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=p)
        t2=copy.deepcopy(t);t2['goal']+=' New frozen attempt.'
        fresh=model(plan)
        b=await flow.run(d['id'],fresh,model(review),support_tasks={t['id']:t2},probes=p)
        assert a['job']!=b['job'] and fresh.calls==1
    finally:app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('gate_result,approval,expected',[
    ('REJECT',False,'RELEASE_GATES_REJECTED'),
    ('PASS',False,'RELEASE_APPROVAL_REQUIRED'),
    ('PASS',True,'ISOLATION_REQUIRED'),
])
async def test_late_stage_orchestration_never_bypasses_real_activate(tmp_path,monkeypatch,gate_result,approval,expected):
    """Control-flow mocks only; no genuine model/qualification/gate assertions."""
    app,d,t,p,plan,review=await fixture(tmp_path)
    try:
        class Evaluator:
            calls=0
            async def qualify(self,cid,*cohorts):
                self.calls+=1
                if approval:app.releases.approve(cid,'offline-test-operator')
                return {'status':gate_result}
        evaluator=Evaluator();flow=app.procedure_promotion(Runner(),evaluator)
        original_model=flow._model
        async def dispatch(*args):
            v=await original_model(*args);v['integration_only']=False;return v
        monkeypatch.setattr(flow,'_model',dispatch)
        async def s2(*a,**kw):return {'passed':True,'evidence_class':'MODEL_TASK_EVALUATION'}
        async def s3(*a,**kw):return {'passed':True,'evidence_class':'MODEL_TASK_EVALUATION'}
        monkeypatch.setattr(flow.validator,'s2',s2);monkeypatch.setattr(flow.validator,'s3',s3)
        before=app.releases.active()
        r=await flow.run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=p,
                         transfer_tasks=[{'unit_control_flow_only':True}],cohorts=['dev','promotion','final'])
        assert r['reason']==expected and evaluator.calls==1,r
        assert not r['published'] and app.releases.active()==before
        assert not app.store.values('activated')
    finally:app.close()


@pytest.mark.asyncio
async def test_orphan_candidate_build_is_recovered_not_duplicated(tmp_path):
    app,d,t,p,plan,review=await fixture(tmp_path)
    try:
        flow=app.procedure_promotion(Runner())
        r=await flow.run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=p)
        with app.store.tx() as c:
            c.execute("DELETE FROM objects WHERE kind='promotion_candidate' AND id=?",(r['job'],))
        before=len(app.store.values('candidate'))
        resumed=await flow.run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=p)
        assert resumed['candidate']==r['candidate'] and len(app.store.values('candidate'))==before
    finally:app.close()


@pytest.mark.asyncio
async def test_infrastructure_failure_is_blocked_not_model_rejection(tmp_path):
    app,d,t,p,plan,review=await fixture(tmp_path)
    try:
        class BrokenRunner(Runner):
            async def run(self,*args,**kw):
                result=await super().run(*args,**kw)
                result['state']='INFRASTRUCTURE_ERROR';result['passed']=False
                return result
        r=await app.procedure_promotion(BrokenRunner()).run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=p)
        assert r['status']=='BLOCKED' and r['reason']=='S2_INFRASTRUCTURE_ERROR'
        assert not app.store.values('skill_s3') and not app.store.values('gate')
    finally:app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('binding,reason',[
    ({'tool':'patch','arguments_match':{'function_definition':'def f(): pass'}},'PROMOTION_STEP_ARGUMENT_UNKNOWN'),
    ({'tool':'create','arguments_match':{'created_content':'assert True'}},'PROMOTION_STEP_ARGUMENT_UNKNOWN'),
    ({'tool':'process.run','arguments_match':{'argv':'python -m pytest'}},'PROMOTION_STEP_ARGUMENT_TYPE'),
    ({'tool':'read','arguments_match':{'start':0}},'PROMOTION_STEP_ARGUMENT_TYPE'),
    ({'tool':'edit','arguments_match':{'expected_version':'old-version'}},'PROMOTION_VOLATILE_STEP_BINDING'),
    ({'tool':'patch','arguments_match':{'operations':[{'kind':'edit','path':'main.py','old_text':'x','new_text':'y'}]}},'PROMOTION_VOLATILE_STEP_BINDING'),
    ({'tool':'patch','arguments_match':{'operations':[{'kind':'create','path':'main.py'}]}},'PROMOTION_STEP_ARGUMENT_TYPE'),
])
async def test_live_trace_invalid_bindings_stop_before_review_or_trials(tmp_path,binding,reason):
    app,d,t,p,plan,review=await fixture(tmp_path)
    try:
        plan['step_rules']=[binding];runner=Runner();reviewer=model(review)
        r=await app.procedure_promotion(runner).run(d['id'],model(plan),reviewer,support_tasks={t['id']:t},probes=p)
        assert r['status']=='BLOCKED' and r['reason']==reason,r
        assert reviewer.calls==0 and runner.calls==0
        assert not app.store.values('promotion_materialization') and not app.store.values('candidate')
    finally:app.close()
