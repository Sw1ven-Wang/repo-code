import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from repo_code.models import Contract,Requirement,Check,Subject,Authority,Evidence,PreparedDecision
from repo_code.acceptance import validate_contract,freeze_contract,delivery_verified
from repo_code.util import RepoError,digest,Faults
from conftest import new_run,make_contract

def mutate_contract(contract,**change):return Contract.model_validate({**contract.model_dump(),**change})

@pytest.mark.parametrize('change,code',[
    ({'required_checks':[]},'EMPTY'),
    ({'coverage':{'R1':['unknown']}},'UNKNOWN'),
    ({'coverage':{}},'COVERAGE'),
    ({'required_checks':['behavior','behavior']},'DUPLICATE'),
    ({'global_required_checks':['unknown']},'GLOBAL'),
])
def test_contract_rejects_invalid_mapping(contract,change,code):
    with pytest.raises(RepoError):validate_contract(mutate_contract(contract,**change))

def test_unmapped_required_check_rejected(contract):
    c=mutate_contract(contract,checks=[*contract.checks,Check(id='static',kind='static_python')],required_checks=['behavior','static'])
    with pytest.raises(RepoError):validate_contract(c)

def test_explicit_global_check_allowed(contract):
    c=mutate_contract(contract,checks=[*contract.checks,Check(id='static',kind='static_python')],required_checks=['behavior','static'],global_required_checks=['static'])
    validate_contract(c,True)

@pytest.mark.asyncio
async def test_all_required_checks_not_only_coverage(app,source):
    contract=make_contract(extra=[Check(id='static',kind='static_python')])
    run=new_run(app,source,contract);(app.runtime.workspace(run).root/'bad.py').write_text('if :\n')
    intent=app.runtime.prepare(run['id']);decision=await app.grader.evaluate(intent,contract)
    assert not decision.verified
    assert app.runtime.commit(intent,decision)['status']=='NOT_VERIFIED'

@pytest.mark.asyncio
async def test_success_pins_every_input(app,source,contract):
    run=new_run(app,source,contract);intent=app.runtime.prepare(run['id']);decision=await app.grader.evaluate(intent,contract)
    assert decision.verified
    result=app.runtime.commit(intent,decision)
    assert result['status']=='COMMITTED'
    d=result['delivery'];assert d['source_digest']==intent.source_digest and d['contract_digest']==contract.digest
    assert app.runtime.commit(intent,decision)['status']=='ALREADY_COMMITTED'
    with app.store.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==1

@pytest.mark.asyncio
async def test_code_change_after_test_invalidates(app,source,contract):
    run=new_run(app,source,contract);intent=app.runtime.prepare(run['id']);decision=await app.grader.evaluate(intent,contract)
    (app.runtime.workspace(run).root/'main.py').write_text('def answer(): return 0')
    assert app.runtime.commit(intent,decision)['status']=='REVERIFY_REQUIRED'

@pytest.mark.asyncio
@pytest.mark.parametrize('action',['STEER','CANCEL','PAUSE'])
async def test_control_after_last_check_before_commit(app,source,contract,action):
    run=new_run(app,source,contract);intent=app.runtime.prepare(run['id']);decision=await app.grader.evaluate(intent,contract)
    app.runtime.admit(run['id'],'control-1',action,'Add authentication')
    assert app.runtime.commit(intent,decision)['status']=='STALE_CONTROL'
    with app.store.connect() as c:assert c.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0]==0

@pytest.mark.asyncio
async def test_late_control_preserves_committed_delivery(app,source,contract):
    run=new_run(app,source,contract);i=app.runtime.prepare(run['id']);d=await app.grader.evaluate(i,contract)
    app.runtime.commit(i,d)
    event=app.runtime.admit(run['id'],'later','STEER','new goal')
    assert event['status']=='AFTER_TERMINAL' and app.runtime.run(run['id'])['state']=='SUCCEEDED'

@pytest.mark.asyncio
async def test_delivery_and_outbox_rollback_together(app,source,contract):
    run=new_run(app,source,contract);i=app.runtime.prepare(run['id']);d=await app.grader.evaluate(i,contract)
    def fail(point):
        if point=='delivery_before_outbox':raise RuntimeError('fault')
    with pytest.raises(RuntimeError):app.runtime.commit(i,d,Faults(fail))
    with app.store.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
    assert app.runtime.run(run['id'])['state']=='VERIFYING'
    assert app.runtime.commit(i,d)['status']=='COMMITTED'

@pytest.mark.asyncio
async def test_two_connections_race_cancel_and_delivery(app,source,contract):
    from repo_code.runtime import Runtime
    from repo_code.storage import Store
    run=new_run(app,source,contract);i=app.runtime.prepare(run['id']);d=await app.grader.evaluate(i,contract)
    other=Runtime(Store(app.store.root),app.sandbox);barrier=threading.Barrier(2)
    def commit():barrier.wait();return app.runtime.commit(i,d)
    def cancel():barrier.wait();return other.admit(run['id'],'racing-cancel','CANCEL')
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(commit);b=pool.submit(cancel);result=a.result();event=b.result()
    if result['status']=='COMMITTED':assert event['status']=='AFTER_TERMINAL'
    else:assert result['status']=='STALE_CONTROL' and event['status']=='PENDING_CANCEL'

@pytest.mark.asyncio
async def test_evidence_tampering_rejected(app,source,contract):
    run=new_run(app,source,contract,content='def answer(): return 0');i=app.runtime.prepare(run['id']);d=await app.grader.evaluate(i,contract)
    row=app.store.get('evidence',d.evidence_ids[0]);row['status']='PASSED';app.store.put('evidence',row['id'],row)
    fake=d.model_copy(update={'verified':True});fake=fake.model_copy(update={'signature':app.store.sign(fake.model_dump(mode='json',exclude={'signature'}))})
    assert app.runtime.commit(i,fake)['status']=='NOT_VERIFIED'

@pytest.mark.asyncio
async def test_source_checks_do_not_need_artifact_hash(app,source,contract):
    run=new_run(app,source,contract);i=app.runtime.prepare(run['id']);d=await app.grader.evaluate(i,contract)
    ev=app.store.get('evidence',d.evidence_ids[0]);assert ev['tested_artifact'] is None and ev['status']=='PASSED'

@pytest.mark.asyncio
async def test_inprocess_diagnostic_not_behavior_proof(app,source):
    c=Contract(requirements=[Requirement(id='R',description='answer')],checks=[Check(id='py',kind='diagnostic',definition={'argv':['python','main.py']})],required_checks=['py'],coverage={'R':['py']})
    with pytest.raises(RepoError):freeze_contract(c,'operator')

@pytest.mark.asyncio
async def test_sameprocess_monkeypatch_does_not_fool_external_grader(app,source,contract):
    evil='import unittest\nunittest.TestCase.assertEqual=lambda *a,**k: None\ndef answer(): return 0\n'
    run=new_run(app,source,contract,content=evil);i=app.runtime.prepare(run['id']);d=await app.grader.evaluate(i,contract)
    assert not d.verified

@pytest.mark.asyncio
async def test_successor_inherits_bytes_not_contract_evidence(app,source,contract):
    run=new_run(app,source,contract);app.runtime.admit(run['id'],'change','STEER','return 43')
    app.runtime.finish_control('change','contract_change')
    second=app.runtime.successor(run['id'],'return 43',make_contract(43))
    assert second['data']['manifest']['task_spec_revision']==2
    assert second['data']['manifest']['contract_digest']!=run['data']['manifest']['contract_digest']
    assert app.runtime.workspace(second).read('main.py')['content']=='def answer():\n    return 42\n'
    assert app.runtime.run(run['id'])['state']=='SUPERSEDED'

@pytest.mark.asyncio
async def test_control_idempotency_and_changed_payload(app,source,contract):
    run=new_run(app,source,contract);first=app.runtime.admit(run['id'],'same','STEER','constraint')
    second=app.runtime.admit(run['id'],'same','STEER','constraint')
    assert first['sequence']==second['sequence']
    with pytest.raises(RepoError,match='IDEMPOTENCY_CONFLICT'):app.runtime.admit(run['id'],'same','STEER','different')
