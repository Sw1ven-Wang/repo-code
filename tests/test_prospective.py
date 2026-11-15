"""Offline protocol tests; no scripted outcome proves model benefit."""
import pytest
from repo_code.util import RepoError,digest
from repo_code.models import Budget
from repo_code.governance import HoldoutVault
from repo_code.evaluation import SuiteEvaluator
from repo_code.providers import ScriptedProvider
from test_review_workflow import setup

def prepared(tmp_path):
    app,_=setup(tmp_path)
    c=app.releases.candidate('test',{'retry_policy':{'kind':'config','max_identical_failures':2}},'prospective fixture',[])
    vault=HoldoutVault(app.store)
    evaluator=SuiteEvaluator(app,vault,lambda _:ScriptedProvider())
    evaluator.runner.isolated_states=True;evaluator.runner.trial_root=tmp_path/'trials'
    queue=app.prospective_certification(evaluator)
    key=queue.freeze(c.id,budget=Budget(),counts=dict.fromkeys(queue.ROLES,1))
    return app,c,vault,evaluator,queue,key

def cohort(vault,role,*,identity=None,group=None,files=None,budget=None,family='precision'):
    identity=identity or role
    return vault.register(identity,role,[{'id':identity,'goal':'repair '+identity,
        'files':files or {'main.py':'value='+repr(identity)},'budget':budget or Budget().model_dump(mode='json')}],
        synthetic=True,applicability_family=family,contamination_groups=[group or identity])

def test_frozen_collection_persistence_duplicates_and_quotas(tmp_path):
    app,c,v,e,q,key=prepared(tmp_path)
    try:
        assert q.status(key)['missing']==dict.fromkeys(q.ROLES,1)
        d=cohort(v,'dev');q.nominate(key,d);assert q.nominate(key,d)['counts']['dev']==1
        with pytest.raises(RepoError,match='QUOTA'):q.nominate(key,cohort(v,'dev',identity='extra'))
        with pytest.raises(RepoError,match='SOURCE_OVERLAP'):q.nominate(key,cohort(v,'promotion',group='dev'))
        with pytest.raises(RepoError,match='DUPLICATE_TASK'):q.nominate(key,cohort(v,'promotion',files={'main.py':"value='dev'"}))
        with pytest.raises(RepoError,match='BUDGET_MISMATCH'):q.nominate(key,cohort(v,'promotion',budget={'model_calls':5}))
        q.nominate(key,cohort(v,'promotion'));q.nominate(key,cohort(v,'final'))
        assert q.status(key)['status']=='READY_FOR_GATES'
        from repo_code.prospective import ProspectiveCertification
        assert ProspectiveCertification(app,e).status(key)==q.status(key)
        assert not app.store.values('gate') and app.releases.active()['digest']==c.base_release_digest
        with pytest.raises(RepoError,match='ALREADY_FROZEN'):q.freeze(c.id,budget=Budget(),counts=dict.fromkeys(q.ROLES,2))
    finally:app.close()

@pytest.mark.asyncio
async def test_waiting_does_not_invoke_evaluator_and_observation_never_counts(tmp_path):
    app,c,v,e,q,key=prepared(tmp_path)
    try:
        async def forbidden(*a,**k):raise AssertionError('No paid evaluation while waiting')
        e.qualify=forbidden
        assert (await q.evaluate(key))['status']=='WAITING_EVIDENCE'
        source=tmp_path/'ordinary';source.mkdir();(source/'a.py').write_text('x=1')
        run=app.new_task('ordinary task',source)
        with pytest.raises(RepoError,match='NOT_TERMINAL'):q.observe(key,run['id'])
        app.runtime.end(run['id'],'PARTIAL','fixture')
        q.observe(key,run['id']);q.observe(key,run['id'])
        assert q.status(key)['counts']==dict.fromkeys(q.ROLES,0)
        assert len(app.store.values('certification_observation'))==1
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['none','exception','crash','changed','not_isolated'])
async def test_delegation_and_fail_closed_recovery(tmp_path,fault):
    app,c,v,e,q,key=prepared(tmp_path);calls=[]
    try:
        for role in q.ROLES:q.nominate(key,cohort(v,role))
        async def fake(cid,dev,promotion,final):
            calls.append(cid)
            assert [app.store.get('cohort',k)['role'] for k in (dev,promotion,final)]==list(q.ROLES)
            assert all(app.store.get('cohort',k)['applicability_family']=='precision' for k in (dev,promotion,final))
            if fault=='exception':raise RepoError('FIXTURE_FAILURE')
            return {'status':'REJECT','reason':'fixture evaluator verdict'}
        e.qualify=fake
        if fault=='crash':
            state=app.store.get('certification_state',key);state['status']='RUNNING';app.store.put('certification_state',key,state)
        if fault=='changed':e.runner.context_ceiling=32000
        if fault=='not_isolated':e.runner.isolated_states=False
        if fault in {'exception','changed','not_isolated'}:
            with pytest.raises(RepoError):await q.evaluate(key)
        else:
            result=await q.evaluate(key)
            assert result['status']==('INTERRUPTED' if fault=='crash' else 'REJECTED')
            await q.evaluate(key)
        assert len(calls)==(1 if fault in {'none','exception'} else 0)
        assert app.releases.active()['digest']==c.base_release_digest
    finally:app.close()

def test_tampered_freeze_and_nonfinite_budget(tmp_path):
    app,c,v,e,q,key=prepared(tmp_path)
    try:
        with pytest.raises(RepoError,match='FINITE_BUDGET'):q.freeze(c.id,budget=Budget(model_calls=None))
        row=app.store.get('certification_plan',key);row['policy']['counts']['dev']=0;app.store.put('certification_plan',key,row)
        with pytest.raises(RepoError,match='PLAN_INVALID'):q.nominate(key,cohort(v,'dev'))
    finally:app.close()

@pytest.mark.asyncio
async def test_actual_native_gates_preserve_integration_only_and_approval(app,tmp_path):
    from repo_code.demo import task_fixture,coding_fixture
    c=app.releases.candidate('fixture',{'retry_policy':{'kind':'config','max_identical_failures':2,
        'require_new_evidence':True}},'offline recovery fixture',[])
    vault=HoldoutVault(app.store);ev=SuiteEvaluator(app,vault,coding_fixture)
    ev.runner.isolated_states=True;ev.runner.trial_root=tmp_path/'isolated'
    queue=app.prospective_certification(ev)
    key=queue.freeze(c.id,budget=task_fixture('budget')['budget'],counts=dict.fromkeys(queue.ROLES,1))
    for role in queue.ROLES:
        task=task_fixture(role);task['goal']+=' Protocol fixture '+role
        co=vault.register(role,role,[task],synthetic=True,applicability_family='offline-fixture',contamination_groups=[role])
        queue.nominate(key,co)
    result=await queue.evaluate(key)
    assert result['status']=='COMPLETED',result
    assert all(app.store.get('gate',digest([c.id,g]))['payload']['passed'] for g in ('G0','G1','G2','G3','G4','G4b'))
    assert len(app.store.values('trial_dev'))==2 and len(app.store.values('trial_private'))==4
    assert len({r['state_root'] for kind in ('trial_dev','trial_private') for r in app.store.values(kind)})==6
    with pytest.raises(RepoError,match='INTEGRATION_ONLY_EVIDENCE'):app.releases.activate(c.id)
    assert app.releases.active()['digest']==c.base_release_digest
    assert not app.store.values('release_approval')
