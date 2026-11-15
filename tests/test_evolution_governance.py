import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from repo_code.models import Candidate,Contract,Budget
from repo_code.util import RepoError,digest
from repo_code.governance import ComplexityGovernor,HoldoutVault,Lineage,DEFAULT_GATE
from repo_code.evolved import EvolvedWorker
from repo_code.observation import Observer,EvolutionQueue
from repo_code.evaluation import SuiteEvaluator
from repo_code.evolution import EvolutionEngine
from repo_code.demo import task_fixture,coding_fixture,optimizer_fixture
from conftest import new_run

def candidate(app,changes=None,level=1,parents=None):
    return app.releases.candidate('pattern',changes or {'retry_policy':{'kind':'config','max_identical_failures':2,'require_new_evidence':True}},'test targeted replacement',['test-evidence'],parents=parents,level=level)

def trusted_unit_gates(app,c):
    from repo_code.regression import G4_PROTOCOL,G4_CASES,suite_digest
    app.store.put('operator_config','integration',{'enabled':True,'reviewer':'UNIT-ONLY registry tests'})
    for gate in ['G0','G1','G2','G3','G4','G4b']:
        report={'passed':True,'fixture':'UNIT-ONLY synthetic signed report; not a benchmark',
                'evidence_class':'INTEGRATION_ONLY'}
        if gate=='G4':
            report.update(protocol=G4_PROTOCOL,suite_digest=suite_digest(),tested_release=c.candidate_release_digest,
                tested_components=app.releases.get(c.candidate_release_digest).components,
                environment_digest=app.sandbox.environment_digest,cases=[{'id':x,'passed':True} for x in G4_CASES],skipped=[])
        app.releases.record_gate(c,gate,report)
    app.releases.approve(c.id,'unit-operator')

@pytest.mark.parametrize('component',['permissions','publisher','grader','database','safety','complexity_governor'])
def test_trusted_components_not_evolvable(app,component):
    with pytest.raises(RepoError,match='EVOLUTION_SCOPE_DENIED'):candidate(app,{component:{'kind':'config'}})

def test_l1_cannot_modify_optimizer(app):
    with pytest.raises(RepoError,match='META_REQUIRED'):candidate(app,{'l1_optimizer':{'kind':'config'}})

def test_meta_scope_cannot_mutate_coding_directly(app):
    with pytest.raises(RepoError,match='META_SCOPE_DENIED'):candidate(app,{'context_policy':{'kind':'config'}},2)

def test_full_manifest_cas_rejects_unassessed_combination(app):
    base=app.releases.active();a=candidate(app,{'context_policy':{'kind':'config','max_tokens':22000,'reserve_tokens':4096}});b=candidate(app)
    trusted_unit_gates(app,a);trusted_unit_gates(app,b)
    app.releases.activate(a.id)
    with pytest.raises(RepoError,match='RELEASE_CAS_CONFLICT'):app.releases.activate(b.id)
    active=app.releases.get(app.releases.active()['digest'])
    assert active.components['retry_policy']==app.releases.get(base['digest']).components['retry_policy']

@pytest.mark.asyncio
async def test_old_run_pins_release_and_rollback_whole_manifest(app,source,contract):
    run=new_run(app,source,contract);base=app.releases.active()['digest'];c=candidate(app);trusted_unit_gates(app,c);app.releases.activate(c.id)
    assert app.runtime.run(run['id'])['data']['manifest']['release_digest']==base
    app.releases.rollback(base,c.candidate_release_digest,'operator')
    assert app.releases.active()['digest']==base

@pytest.mark.parametrize('missing',['G0','G1','G2','G3','G4','G4b'])
def test_all_publish_gates_required(app,missing):
    c=candidate(app)
    for gate in ['G0','G1','G2','G3','G4','G4b']:
        if gate!=missing:app.releases.record_gate(c,gate,{'passed':True})
    app.releases.approve(c.id,'operator')
    with pytest.raises(RepoError,match=missing):app.releases.activate(c.id)

def test_gate_signature_tampering_rejected(app):
    c=candidate(app);trusted_unit_gates(app,c)
    key=digest([c.id,'G4b']);row=app.store.get('gate',key);row['payload']['passed']=False;app.store.put('gate',key,row)
    with pytest.raises(RepoError):app.releases.activate(c.id)

def test_complexity_rejects_prompt_bloat_and_unjustified_add(app):
    with pytest.raises(RepoError,match='prompt_tokens'):candidate(app,{'planning_policy':{'kind':'config','instructions':'x'*100000}})
    with pytest.raises(RepoError,match='GROWTH_JUSTIFICATION_REQUIRED'):
        app.releases.candidate('p',{'retry_policy':{'kind':'config'}},'h',[],operation='ADD')

@pytest.mark.asyncio
async def test_generated_code_isolated_before_and_after_promotion(app):
    path=str(app.store.root/'host.key')
    source=f"import os\ndef handle(payload):\n    try:\n        open({path!r}).read()\n        return {{'leaked':True}}\n    except OSError:\n        return {{'leaked':False,'uid':os.getuid()}}\n"
    c=candidate(app,{'retry_policy':{'kind':'python','source':source}})
    worker=EvolvedWorker(app.store,app.sandbox);component=app.releases.components(c.candidate_release_digest)['retry_policy']
    assert not (await worker.invoke(component,{}))['leaked']
    trusted_unit_gates(app,c);app.releases.activate(c.id)
    result=await worker.invoke(app.releases.components(app.releases.active()['digest'])['retry_policy'],{})
    assert not result['leaked'] and result['uid']!=0

def test_holdout_minimal_feedback_taints_descendants(app):
    vault=HoldoutVault(app.store);cohort=vault.register('hidden','promotion',[{'id':'t'}],family='family')
    with pytest.raises(RepoError,match='HIDDEN_DATA_DENIED'):vault.open(cohort)
    c=candidate(app);vault.claim(c.id,cohort,'G3');vault.notice(c.id,cohort,False,'REJECT')
    child=candidate(app,parents=[c.id])
    replacement=vault.register('renamed','promotion',[{'id':'different'}],family='family')
    with pytest.raises(RepoError,match='HOLDOUT_CONTAMINATED'):vault.claim(child.id,replacement,'G3')

def test_lineage_late_exposure_propagates(app):
    line=Lineage(app.store);line.derive('child',['parent']);line.derive('grandchild',['child'])
    line.expose('parent','cohort','family')
    assert line.is_exposed('grandchild','new-cohort','family')
    with pytest.raises(RepoError):line.derive('parent',['grandchild'])

def test_observer_excludes_hidden_and_is_replay_idempotent(app):
    with app.store.tx() as conn:
        for visibility in ['TRUSTED_ONLY','DEV']:
            for i in range(3):app.store.emit(conn,visibility,'tool.operation_finished',{'tool_name':'process.run','action_digest':'a','result':{'is_error':True,'relevant_state':'state','stderr':'ImportError: fixture'}},visibility)
    obs=Observer(app.store);assert len(obs.consume())==1;count=len(app.store.values('problem'));assert obs.consume()==[]
    assert len(app.store.values('problem'))==count
    assert all(p['run_id']=='DEV' for p in app.store.values('problem'))

def test_evolution_queue_deduplicates_and_defers_to_coding(app,source,contract):
    q=EvolutionQueue(app.store);job=q.enqueue('p')
    with pytest.raises(RepoError,match='PATTERN_ALREADY_QUEUED'):q.enqueue('p')
    run=new_run(app,source,contract);assert q.claim() is None
    app.runtime.end(run['id'],'PARTIAL','operator ended');assert q.claim()['id']==job
    assert q.claim() is None;q.finish(job)

@pytest.mark.asyncio
async def test_real_l0_to_l1_gates_publish_and_new_run(app,source):
    fixture=task_fixture('source');contract=Contract.model_validate(fixture['contract'])
    app.agent.provider=app.broker.provider=coding_fixture()
    run=app.new_task(fixture['goal'],source,contract,Budget.model_validate(fixture['budget']),True,True)
    result=await app.execute(run['id']);assert result['state']=='PARTIAL'
    patterns=Observer(app.store).consume();assert patterns
    vault=HoldoutVault(app.store);evaluator=SuiteEvaluator(app,vault,coding_fixture)
    dev=vault.register('development','dev',[task_fixture('dev')],synthetic=True);promotion=vault.register('promotion-integration-fixture','promotion',[task_fixture('pro')],synthetic=True);final=vault.register('final-integration-fixture','final',[task_fixture('fin')],synthetic=True)
    engine=EvolutionEngine(app,optimizer_fixture(),evaluator)
    search=await engine.search(patterns[0],dev,max_candidates=1,max_rounds=1)
    assert search['selected'],search
    result=await evaluator.qualify(search['selected'],dev,promotion,final)
    assert result['status']=='PASS',result
    app.releases.approve(search['selected'],'test-operator')
    with pytest.raises(RepoError,match='INTEGRATION_ONLY_EVIDENCE'):app.releases.activate(search['selected'])
    app.store.put('operator_config','integration',{'enabled':True,'reviewer':'test-fixture'})
    app.releases.activate(search['selected'])
    app.agent.provider=app.broker.provider=coding_fixture()
    r=app.new_task(fixture['goal'],source,contract,Budget.model_validate(fixture['budget']),True,True)
    result=await app.execute(r['id']);assert result['state']=='SUCCEEDED',result
    assert all(t['provider_kind']=='scripted_test_double' for t in app.store.values('trial_private'))

@pytest.mark.asyncio
async def test_negative_candidate_rejected_before_hidden_spend(app):
    vault=HoldoutVault(app.store);ev=SuiteEvaluator(app,vault,coding_fixture)
    dev=vault.register('d','dev',[task_fixture('d')]);pro=vault.register('p','promotion',[task_fixture('p')]);fin=vault.register('f','final',[task_fixture('f')])
    bad=candidate(app,{'retry_policy':{'kind':'config','require_new_evidence':False}})
    report=await ev.qualify(bad.id,dev,pro,fin);assert report['status']=='REJECT'
    with app.store.connect() as c:assert c.execute('SELECT COUNT(*) FROM cohort_claims').fetchone()[0]==0
