"""Host stage imports, finite signed allocations, and raw-history isolation."""
import copy
import pytest
from repo_code.benchmark import TrustedStageLoader,admit_budget
from repo_code.models import Budget,ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.util import RepoError,Faults,digest


def setup(tmp_path,**budget):
    source=tmp_path/'source';source.mkdir();(source/'first.py').write_bytes(b'initial\n')
    app=RepoCode(tmp_path/'host',ScriptedProvider(),sandbox='local')
    run=app.new_task('first request',source,conversation=True,authorize_write=True,authorize_process=True,
                     budget=Budget(tool_calls=1000,wall_seconds=1800))
    admit_budget(app.runtime,run['id'],experiment_digest=digest('frozen-spec'),
        session_model_calls=budget.get('model',2000),session_tool_calls=budget.get('tools',5000),
        stage_model_calls=budget.get('stage_model',700),stage_tool_calls=budget.get('stage_tools',700),
        wall_seconds=1800,stage_seconds=600)
    loader=TrustedStageLoader(app.runtime)
    ws=app.runtime.workspace(app.runtime.run(run['id']))
    loader.admit(run['id'],'first',{'first.py':b'initial\n'},['first.py'],prompt_digest=digest('first request'),expected_before=ws.snapshot(),initial=True)
    return app,run,loader


async def invoke(app,run,call):
    req=app.broker.register_call(run['id'],'test-'+call.id,call)
    return await app.broker.submit(req)


def test_finite_allocation_survives_restart_and_default_task_cap_remains(tmp_path):
    app,run,_=setup(tmp_path)
    app.runtime.consume(run['id'],'model_calls',600)
    app.runtime.consume(run['id'],'tool_calls',600)
    assert app.runtime.budget_limits(app.runtime.run(run['id']))['model_calls']==2000
    app.close()
    app=RepoCode(tmp_path/'host',ScriptedProvider(),sandbox='local')
    assert app.runtime.run(run['id'])['data']['budget_used']=={'model_calls':600,'tool_calls':600,'child_calls':0}
    assert app.runtime.budget_limits(app.runtime.run(run['id']))['tool_calls']==5000
    app.close()
    plain=tmp_path/'plain';plain.mkdir();app=RepoCode(tmp_path/'plain-host',ScriptedProvider(),sandbox='local')
    run=app.new_task('ordinary',plain,budget=Budget(tool_calls=5000))
    app.runtime.consume(run['id'],'tool_calls',1000)
    with pytest.raises(RepoError,match='TASK_TOTAL_BUDGET'):app.runtime.consume(run['id'],'tool_calls',1)
    app.close()


@pytest.mark.parametrize('change',['tamper','revoke'])
def test_allocation_requires_host_signature_and_current_authority(tmp_path,change):
    app,run,_=setup(tmp_path)
    with app.store.tx() as c:
        current=app.runtime.run(run['id'],c);grant=current['data']['benchmark_allocation']
        if change=='tamper':
            grant['body']['session_model_calls']=99999
            app.runtime.update(c,current,data=current['data'])
        else:app.store.put('revocation',digest(grant['body']),{'reason':'operator revoked evaluation'},c)
    with pytest.raises(RepoError,match='BENCHMARK_ALLOCATION_'):app.runtime.consume(run['id'],'model_calls')
    app.close()


async def test_stage_scope_and_budget_reject_before_tool_effect(tmp_path):
    app,run,_=setup(tmp_path,stage_tools=1)
    receipt=await invoke(app,run,ToolCall(id='first-read',name='read',arguments={'path':'first.py'}))
    assert receipt['status']=='SUCCEEDED'
    with pytest.raises(RepoError) as stopped:
        await invoke(app,run,ToolCall(id='second-read',name='read',arguments={'path':'first.py'}))
    assert stopped.value.code=='BUDGET_EXHAUSTED'
    assert not app.runtime.unresolved(run['id'])
    app.runtime.wait_for_user(run['id'])
    ws=app.runtime.workspace(app.runtime.run(run['id']))
    TrustedStageLoader(app.runtime).admit(run['id'],'second',{'second.py':b'new'},['second.py'],
        prompt_digest=digest('second request'),expected_before=ws.snapshot())
    app.submit_turn(run['id'],'second','second request')
    refused=await invoke(app,run,ToolCall(id='bad-write',name='create',arguments={'path':'future.py','content':'never'}))
    assert refused['status']=='FAILED' and refused['result']['code']=='BENCHMARK_STAGE_SCOPE_DENIED'
    assert not ws.path('future.py').exists()
    assert app.runtime.run(run['id'])['data']['budget_used']['tool_calls']==2
    app.close()


def test_stage_versions_idempotency_and_future_input_absent(tmp_path):
    app,run,loader=setup(tmp_path)
    app.runtime.wait_for_user(run['id']);prior=app.runtime.run(run['id']);ws=app.runtime.workspace(prior)
    args=dict(prompt_digest=digest('stage2'),expected_before=ws.snapshot())
    first=loader.admit(run['id'],'stage2',{'second.py':b'current only'},['second.py'],**args)
    assert loader.admit(run['id'],'stage2',{'second.py':b'current only'},['second.py'],**args)==first
    after=app.runtime.run(run['id'])
    assert after['writer_epoch']==prior['writer_epoch']+1 and after['control_epoch']==prior['control_epoch']+1
    assert set(ws.manifest()['files'])=={'first.py','second.py'}
    with pytest.raises(RepoError,match='IDEMPOTENCY_CONFLICT'):
        loader.admit(run['id'],'stage2',{'second.py':b'different'},['second.py'],prompt_digest=digest('changed'),expected_before=ws.snapshot())
    app.close()


def test_stage_budget_origin_is_signed_and_wall_deadline_is_enforced(tmp_path):
    app,run,_=setup(tmp_path)
    assert 0<app.runtime.remaining_seconds(run['id'])<=600
    with app.store.tx() as c:
        stage=app.store.get('benchmark_active_stage',run['id'],c)
        stage['budget_start']['model_calls']=-999
        app.store.put('benchmark_active_stage',run['id'],stage,c)
    with pytest.raises(RepoError,match='BENCHMARK_STAGE_SIGNATURE_INVALID'):app.runtime.consume(run['id'],'model_calls')
    app.close()


async def test_benchmark_refuses_local_process_without_isolated_import(tmp_path):
    import sys
    app,run,_=setup(tmp_path)
    app.runtime.wait_for_user(run['id']);ws=app.runtime.workspace(app.runtime.run(run['id']))
    TrustedStageLoader(app.runtime).admit(run['id'],'web',{'src/index.html':b'protected','src/App.tsx':b'initial'},['src/*'],
        protected_paths=['src/index.html'],prompt_digest=digest('web'),expected_before=ws.snapshot())
    app.submit_turn(run['id'],'web','web')
    with pytest.raises(RepoError,match='BENCHMARK_ISOLATED_PROCESS_REQUIRED'):
        await invoke(app,run,ToolCall(id='deny-process-write',name='process.run',arguments={
            'argv':[sys.executable,'-c',"open('src/index.html','w').write('bad')"],'expect_workspace_change':True}))
    assert ws.path('src/index.html').read_bytes()==b'protected'
    app.close()


class Crash(BaseException):pass


@pytest.mark.parametrize('point',['stage_prepared','stage_file_0','stage_file_1','stage_before_commit','stage_after_commit'])
def test_stage_crashes_reconcile_exactly_once(tmp_path,point):
    app,run,_=setup(tmp_path);app.runtime.wait_for_user(run['id'])
    before=app.runtime.run(run['id']);ws=app.runtime.workspace(before)
    loader=TrustedStageLoader(app.runtime,Faults(lambda name:(_ for _ in ()).throw(Crash()) if name==point else None))
    with pytest.raises(Crash):loader.admit(run['id'],'next',{'two.py':b'two','three.py':b'three'},['two.py','three.py'],prompt_digest=digest('next'),expected_before=ws.snapshot())
    if point!='stage_after_commit':
        with pytest.raises(RepoError,match='BENCHMARK_STAGE_RECOVERY_REQUIRED'):app.submit_turn(run['id'],'next','next')
    app.close();app=RepoCode(tmp_path/'host',ScriptedProvider(),sandbox='local')
    recovered=TrustedStageLoader(app.runtime).recover(run['id'])
    assert recovered is None or recovered['status']=='COMMITTED'
    assert app.runtime.workspace(app.runtime.run(run['id'])).read('three.py')['content']=='three'
    assert app.runtime.run(run['id'])['writer_epoch']==before['writer_epoch']+1
    assert TrustedStageLoader(app.runtime).recover(run['id']) is None
    app.close()


@pytest.mark.parametrize('fence',['writer','control','external-file'])
def test_prepared_stage_never_replays_through_changed_authority(tmp_path,fence):
    app,run,_=setup(tmp_path);app.runtime.wait_for_user(run['id']);ws=app.runtime.workspace(app.runtime.run(run['id']))
    loader=TrustedStageLoader(app.runtime,Faults(lambda name:(_ for _ in ()).throw(Crash()) if name=='stage_prepared' else None))
    with pytest.raises(Crash):loader.admit(run['id'],'next',{'two.py':b'two'},['two.py'],prompt_digest=digest('next'),expected_before=ws.snapshot())
    if fence=='external-file':ws.path('two.py').write_bytes(b'external change')
    else:
        with app.store.tx() as c:app.runtime.update(c,app.runtime.run(run['id'],c),**{fence:True})
    with pytest.raises(RepoError) as stopped:TrustedStageLoader(app.runtime).recover(run['id'])
    assert stopped.value.code in {'BENCHMARK_STAGE_STALE_FENCE','RECOVERY_REQUIRED'}
    if fence=='external-file':assert ws.path('two.py').read_bytes()==b'external change'
    app.close()
