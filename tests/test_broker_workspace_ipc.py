import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
import pytest
from repo_code.models import ToolCall,OperationRequest
from repo_code.util import RepoError,Faults,digest
from repo_code.workspace import Workspace,Mutator,relative_path
from repo_code.ipc import BrokerServer,BrokerClient,Tokens
from repo_code.sandbox import Sandbox
from conftest import new_run,tool

@pytest.mark.parametrize('path',['../outside','/tmp/x','C:/foo','a\\b','a/./b','NUL.txt','aux','x/COM1','x:stream','x/../../z','a\x00b','a.','dir/ LPT1.'])
def test_path_boundaries(path):
    with pytest.raises(RepoError):relative_path(path)

@pytest.mark.asyncio
async def test_read_edit_create_move_delete_and_provenance(app,source,contract):
    run=new_run(app,source,contract)
    _,r=await tool(app,run,'read',{'path':'main.py'});version=r['result']['version']
    _,r=await tool(app,run,'edit',{'path':'main.py','old_text':'return 42','new_text':'return 43'})
    assert r['status']=='SUCCEEDED'
    assert app.runtime.workspace(run).read('main.py')['version']!=version
    _,r=await tool(app,run,'create',{'path':'lib/a.txt','content':'alpha'})
    assert r['status']=='SUCCEEDED'
    req,r=await tool(app,run,'move',{'source':'lib/a.txt','destination':'lib/b.txt'})
    assert r['status']=='WAITING_APPROVAL';app.broker.approve(req.key)
    assert (await app.broker.submit(req))['status']=='SUCCEEDED'
    req,r=await tool(app,run,'delete',{'path':'lib/b.txt'})
    assert r['status']=='WAITING_APPROVAL';app.broker.approve(req.key)
    assert (await app.broker.submit(req))['status']=='SUCCEEDED'
    assert not app.runtime.workspace(run).path('lib/b.txt').exists()
    assert (source/'main.py').read_text().endswith('return 42\n')

@pytest.mark.asyncio
async def test_stale_version_preserves_external_change(app,source,contract):
    run=new_run(app,source,contract);await tool(app,run,'read',{'path':'main.py'})
    app.runtime.workspace(run).path('main.py').write_text('def answer(): return 7\n')
    _,r=await tool(app,run,'edit',{'path':'main.py','old_text':'return 42','new_text':'return 43'})
    assert r['status']=='FAILED' and r['result']['code']=='STALE_VERSION'
    assert 'return 7' in app.runtime.workspace(run).path('main.py').read_text()

@pytest.mark.asyncio
async def test_idempotent_submit_does_not_repeat_write_or_budget(app,source,contract):
    run=new_run(app,source,contract);req,r=await tool(app,run,'create',{'path':'a.txt','content':'one'},call_id='fixed')
    old=app.runtime.run(run['id'])['data']['budget_used']['tool_calls']
    assert (await app.broker.submit(req))==r
    assert app.runtime.run(run['id'])['data']['budget_used']['tool_calls']==old
    bad=req.model_copy(update={'arguments':{'path':'a.txt','content':'two'}})
    with pytest.raises(RepoError,match='IDEMPOTENCY_CONFLICT'):await app.broker.submit(bad)

@pytest.mark.asyncio
async def test_concurrent_same_submit_owns_one_effect(app,source,contract):
    run=new_run(app,source,contract);req=app.broker.register_call(run['id'],'turn',ToolCall(id='same',name='create',arguments={'path':'a.txt','content':'one'}))
    results=await asyncio.gather(app.broker.submit(req),app.broker.submit(req))
    assert {r['operation_id'] for r in results}=={results[0]['operation_id']}
    assert app.broker.get(req.key)['status']=='SUCCEEDED'
    assert app.runtime.run(run['id'])['data']['budget_used']['tool_calls']==1

@pytest.mark.asyncio
async def test_ipc_lost_reply_queries_original_operation(app,source,contract):
    run=new_run(app,source,contract);req=app.broker.register_call(run['id'],'t',ToolCall(id='stable',name='create',arguments={'path':'ipc.txt','content':'done'}))
    with BrokerServer(app.broker,drop_after_result=True) as server:
        cli=BrokerClient(server.address,server.tokens.issue(run['id']),run['id'])
        with pytest.raises(RepoError,match='Query the original'):await asyncio.to_thread(cli.submit,req)
        original=await asyncio.to_thread(cli.get,req.key)
        duplicate=await asyncio.to_thread(cli.submit,req)
        assert original==duplicate and original['status']=='SUCCEEDED'
    assert app.runtime.run(run['id'])['data']['budget_used']['tool_calls']==1

@pytest.mark.asyncio
async def test_ipc_token_cannot_cross_run_or_mint_call(app,source,contract):
    run=new_run(app,source,contract);req=app.broker.register_call(run['id'],'t',ToolCall(id='x',name='create',arguments={'path':'x','content':'x'}))
    with BrokerServer(app.broker) as server:
        client=BrokerClient(server.address,server.tokens.issue('another-run'),run['id'])
        with pytest.raises(RepoError,match='UNAUTHORIZED'):await asyncio.to_thread(client.submit,req)
        cli=BrokerClient(server.address,server.tokens.issue(run['id']),run['id'])
        bogus=req.model_copy(update={'tool_call_uid':'never_registered'})
        with pytest.raises(RepoError,match='UNREGISTERED_HOST_CALL'):await asyncio.to_thread(cli.submit,bogus)

@pytest.mark.asyncio
async def test_crash_after_effect_before_result_reconcile(app,source,contract):
    class Crash(BaseException):pass
    run=new_run(app,source,contract)
    def fault(point):
        if point=='effect_before_result_commit':raise Crash()
    app.broker.faults=Faults(fault)
    req=app.broker.register_call(run['id'],'t',ToolCall(id='x',name='create',arguments={'path':'x','content':'durable'}))
    with pytest.raises(Crash):await app.broker.submit(req)
    assert app.broker.get(req.key)['status']=='UNCERTAIN'
    app.broker.faults=Faults();receipt=app.broker.reconcile(req.key)
    assert receipt['status']=='SUCCEEDED' and receipt['result']['reconciled']=='APPLIED'
    assert (await app.broker.submit(req))==receipt

@pytest.mark.asyncio
async def test_uncertain_process_is_not_replayed(app,source,contract):
    class Crash(BaseException):pass
    run=new_run(app,source,contract)
    def fault(p):
        if p=='effect_before_result_commit':raise Crash()
    app.broker.faults=Faults(fault)
    req=app.broker.register_call(run['id'],'t',ToolCall(id='proc',name='process.run',arguments={'argv':['python','-c','print(1)']}))
    with pytest.raises(Crash):await app.broker.submit(req)
    with pytest.raises(RepoError,match='RESOURCE_LIFETIME_UNKNOWN'):app.broker.reconcile(req.key)
    assert (await app.broker.submit(req))['status']=='UNCERTAIN'

@pytest.mark.asyncio
async def test_multi_file_failure_compensates(app,source,contract):
    run=new_run(app,source,contract);n=[0]
    def fault(p):
        if p=='after_file_effect':
            n[0]+=1
            if n[0]==2:raise RuntimeError('injected')
    app.broker.faults=Faults(fault)
    _,r=await tool(app,run,'patch',{'operations':[{'kind':'create','path':'a','content':'a'},{'kind':'create','path':'b','content':'b'}]})
    assert not app.runtime.workspace(run).path('a').exists() and not app.runtime.workspace(run).path('b').exists()
    assert r['status']!='SUCCEEDED'

@pytest.mark.asyncio
async def test_patch_plan_failure_zero_disk_changes(app,source,contract):
    run=new_run(app,source,contract)
    _,r=await tool(app,run,'patch',{'operations':[{'kind':'create','path':'a','content':'a'},{'kind':'create','path':'main.py','content':'bad'}]})
    assert r['status']=='FAILED' and not app.runtime.workspace(run).path('a').exists()

@pytest.mark.asyncio
async def test_external_change_during_recovery_preserved(app,source,contract):
    class Crash(BaseException):pass
    run=new_run(app,source,contract)
    def fault(p):
        if p=='after_file_effect':raise Crash()
    app.broker.faults=Faults(fault)
    req=app.broker.register_call(run['id'],'t',ToolCall(id='x',name='create',arguments={'path':'a','content':'original'}))
    with pytest.raises(Crash):await app.broker.submit(req)
    app.runtime.workspace(run).path('a').write_text('human change')
    with pytest.raises(RepoError):app.broker.reconcile(req.key)
    assert app.runtime.workspace(run).path('a').read_text()=='human change'

@pytest.mark.asyncio
async def test_approval_invalidated_by_steering(app,source,contract):
    run=new_run(app,source,contract);await tool(app,run,'read',{'path':'main.py'})
    req,r=await tool(app,run,'delete',{'path':'main.py'});assert r['status']=='WAITING_APPROVAL'
    app.runtime.admit(run['id'],'no-delete','STEER','do not delete')
    with pytest.raises(RepoError):app.broker.approve(req.key)
    assert app.broker.get(req.key)['status']=='CANCELLED'

@pytest.mark.asyncio
async def test_large_artifact_access_is_run_scoped(app,source,contract):
    run=new_run(app,source,contract);(app.runtime.workspace(run).root/'large.txt').write_text('x'*30000)
    _,r=await tool(app,run,'read',{'path':'large.txt'})
    assert r['result']['truncated']
    _,got=await tool(app,run,'artifact.read',{'artifact':r['result']['artifact'],'offset':0,'length':10})
    assert got['status']=='SUCCEEDED'
    secret=app.store.blob('private')
    _,denied=await tool(app,run,'artifact.read',{'artifact':secret})
    assert denied['status']=='FAILED' and denied['result']['code']=='ARTIFACT_SCOPE_DENIED'

@pytest.mark.asyncio
async def test_sandbox_process_changes_are_imported_via_journal(app,source,contract):
    run=new_run(app,source,contract)
    _,r=await tool(app,run,'process.run',{'argv':['python','-c',"open('generated.txt','w').write('ok')"]})
    assert r['status']=='SUCCEEDED',r
    assert app.runtime.workspace(run).path('generated.txt').read_text()=='ok'
    assert not (source/'generated.txt').exists()
    assert app.store.get('journal',r['operation_id'])

@pytest.mark.asyncio
async def test_sandbox_cannot_read_host_secret_or_network(app,source,contract):
    run=new_run(app,source,contract);secret=str(app.store.root/'host.key')
    code=f"import socket,os,json\na=[]\ntry:open({secret!r});a.append('file')\nexcept OSError:pass\ntry:socket.socket();a.append('network')\nexcept OSError:pass\nprint(json.dumps([os.getuid(),a]))"
    _,r=await tool(app,run,'process.run',{'argv':['python','-c',code]})
    assert r['status']=='SUCCEEDED',r
    who,violations=json.loads(r['result']['stdout']);assert who!=0 and violations==[]

@pytest.mark.asyncio
async def test_cancel_process_does_not_wait_for_ordinary_queue(app,source,contract):
    run=new_run(app,source,contract)
    req,r=await tool(app,run,'process.start',{'argv':['python','-c','import time;time.sleep(30)'],'timeout':40})
    assert r['result']['running']
    app.runtime.admit(run['id'],'cancel','CANCEL')
    assert app.sandbox.resources_stopped(run['id'])
    assert app.runtime.run(run['id'])['state']=='CANCELLED'

@pytest.mark.asyncio
async def test_timeout_and_restart_unknown_lifetime(app,source,contract):
    run=new_run(app,source,contract)
    _,r=await tool(app,run,'process.run',{'argv':['python','-c','import time;time.sleep(2)'],'timeout':.1})
    assert r['result']['timeout'] and app.sandbox.resources_stopped(run['id'])
    lease=app.sandbox.start(run['id'],app.runtime.workspace(run).root,['python','-c','import time;time.sleep(2)'],timeout=3)
    fresh=Sandbox(app.store,'native');fresh.kit=app.sandbox.kit
    with pytest.raises(RepoError,match='restart requires reconciliation'):fresh.status(lease)
    app.sandbox.status(lease,True)
