"""Early policy, command accounting and all-purpose budget regressions; offline."""
import json
from types import SimpleNamespace
import pytest
from repo_code.models import Assistant, ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.util import RepoError, ProviderNotDispatched
from write_policy import PublicTestWritePolicy
from trace_metrics import closure_metrics
from audited_provider import AuditedProvider

NAME='test/fixtures/rules/std_rule_cases/L031.yml'
BEFORE='old_case:\n  fail_str: SELECT a.x FROM x AS a\n  fix_str: SELECT x.x FROM x\n'
ADDITION='\nnew_case:\n  pass_str: SELECT 1\n'


@pytest.mark.asyncio
async def test_existing_yaml_rejected_before_effects_but_append_and_new_file_allowed(tmp_path):
    source=tmp_path/'source';(source/NAME).parent.mkdir(parents=True);(source/NAME).write_text(BEFORE)
    (source/'module.py').write_text('value=0\n')
    app=RepoCode(tmp_path/'state',ScriptedProvider(),'local')
    app.broker.write_validator=PublicTestWritePolicy()
    run=app.new_task('Fix implementation, preserve tests',source,authorize_write=True)
    async def call(name,args,uid):
        req=app.broker.register_call(run['id'],uid,ToolCall(id=uid,name=name,arguments=args))
        return await app.broker.submit(req)
    try:
        await call('read',{'path':NAME},'read')
        await call('read',{'path':'module.py'},'module')
        bad=await call('patch',{'operations':[{'kind':'edit','path':'module.py','old_text':'value=0','new_text':'value=1'},
                     {'kind':'edit','path':NAME,'old_text':BEFORE,'new_text':'old_case:\n  pass_str: SELECT a.x FROM x AS a\n'}]},'bad')
        assert bad['status']=='FAILED' and bad['result']['code']=='FIXTURE_MUST_APPEND_ONLY'
        ws=app.runtime.workspace(app.runtime.run(run['id']))
        assert ws.path(NAME).read_text()==BEFORE and ws.path('module.py').read_text()=='value=0\n'
        assert 'append' in bad['result']['message'].lower()
        good=await call('edit',{'path':NAME,'old_text':BEFORE,'new_text':BEFORE+ADDITION},'append')
        assert good['status']=='SUCCEEDED' and ws.path(NAME).read_text()==BEFORE+ADDITION
        malformed=await call('edit',{'path':NAME,'old_text':BEFORE+ADDITION,'new_text':BEFORE+ADDITION+'\nbroken: ['},'syntax')
        assert malformed['status']=='FAILED' and malformed['result']['code']=='FIXTURE_INVALID_YAML'
        assert ws.path(NAME).read_text()==BEFORE+ADDITION and app.runtime.run(run['id'])['state']=='RUNNING'
        assert (await call('create',{'path':'test/test_independent.py','content':'def test_new(): assert True\n'},'new'))['status']=='SUCCEEDED'
        assert not app.runtime.unresolved(run['id'])
    finally:app.close()


def test_same_final_policy_can_preserve_officially_reset_public_test(tmp_path):
    from export_policy import VerifierResetPolicy
    from repo_code.storage import Store
    from repo_code.workspace import Workspace
    store=Store(tmp_path/'store');source=tmp_path/'source';source.mkdir()
    (source/'test_old.py').write_text('old')
    ws=Workspace(source,store);baseline=ws.snapshot()
    run={'data':{'manifest':{'workspace_baseline_digest':baseline}}}
    plans=[{'after':{'test_old.py':store.blob(b'new')}}]
    with pytest.raises(RepoError,match='protected'):PublicTestWritePolicy()(run,ws,plans)
    trusted=VerifierResetPolicy('a'*40,'b'*64,frozenset({'test_old.py'}))
    PublicTestWritePolicy(trusted)(run,ws,plans)


class FakeNative:
    model='offline-test';base_url='offline';max_tokens=100;supports_json_mode=True
    def __init__(self,error=False):self.calls=0;self.error=error
    def build_request(self,s,m,t,**options):return {'instructions':s,'input':m,'tools':t,**options},{}
    async def complete(self,s,m,t,on_delta=None,**options):
        self.calls+=1
        if self.error:raise ConnectionError('offline unknown usage fixture')
        return Assistant(text='done',usage={'input_tokens':10,'output_tokens':2})


@pytest.mark.asyncio
async def test_budget_feedback_counts_main_auxiliary_and_not_dispatched(tmp_path):
    inner=FakeNative();provider=AuditedProvider(inner,tmp_path/'record',total_tokens=10000,calls=2)
    await provider.complete('s',[],[])
    status=provider.budget_status()
    assert status['known_used']==12 and status['remaining']==9988
    await provider.complete('s',[],[],json_mode=True)
    assert [r['purpose'] for r in provider.records]==['main','auxiliary']
    with pytest.raises(ProviderNotDispatched):await provider.complete('s',[],[])
    assert inner.calls==2 and len(provider.records)==2
    assert provider.budget_status()['known_used']==24


@pytest.mark.asyncio
async def test_unknown_usage_retains_reserve_and_blocks_new_calls(tmp_path):
    inner=FakeNative(error=True);provider=AuditedProvider(inner,tmp_path/'record')
    with pytest.raises(ConnectionError):await provider.complete('s',[],[])
    status=provider.budget_status()
    assert status['known_used']==0 and status['charged_including_unknown_reserves']>0 and status['unknown_usage_calls']==1
    with pytest.raises(ProviderNotDispatched):await provider.complete('s',[],[])
    assert inner.calls==1 and not (tmp_path/'record/responses/0001.json').exists()


def test_structural_git_metrics_separate_requests_commands_and_api_calls():
    result=closure_metrics([(1,{'tool_calls':[
        {'name':'process.run','arguments':{'argv':['git','diff']}},
        {'name':'process.run','arguments':{'argv':['sh','-c','git status && git diff --stat']}},
        {'name':'process.run','arguments':{'argv':['echo','git diff']}}]})])
    assert result['git_diff_status_request_count']==2
    assert result['git_diff_status_command_count']==3
    assert result['git_diff_status_api_call_count']==1


def test_swe_capabilities_cannot_be_reenabled_by_generic_task_authorization():
    from adapter import TaskBroker
    broker=object.__new__(TaskBroker);broker.mcp={};broker.sandbox=SimpleNamespace(mode='swebench',supports_background=False)
    run={'data':{'authorization':{'delegate':True},'manifest':{'budget':{'child_calls':3}}}}
    assert not broker.available_tools(run)&TaskBroker.unavailable
    with pytest.raises(RepoError,match='synchronous'):
        broker.register_call('unused','unused',ToolCall(id='no-delegate',name='delegate',arguments={'goal':'explore'}))


@pytest.mark.asyncio
async def test_inner_proved_non_dispatch_does_not_leave_unknown_reserve(tmp_path):
    class InnerRefusal(FakeNative):
        async def complete(self,*args,**kwargs):
            raise ProviderNotDispatched('INNER_LIMIT','Proved local refusal')
    provider=AuditedProvider(InnerRefusal(),tmp_path/'record')
    with pytest.raises(ProviderNotDispatched):await provider.complete('s',[],[])
    status=provider.budget_status()
    assert status['charged_including_unknown_reserves']==0 and status['unknown_usage_calls']==0
    assert status['not_dispatched_attempts']==1 and status['dispatched_calls']==0
    assert provider.records[0]['status']=='NOT_DISPATCHED'


@pytest.mark.asyncio
async def test_explicit_unbounded_eval_budget_keeps_usage_accounting(tmp_path):
    provider=AuditedProvider(FakeNative(),tmp_path/'record',total_tokens=None,calls=None)
    await provider.complete('s',[],[]);await provider.complete('s',[],[],json_mode=True)
    status=provider.budget_status()
    assert status['limit'] is None and status['remaining'] is None and status['call_limit'] is None
    assert status['known_used']==24 and status['unknown_usage_calls']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('memory',[False,True])
async def test_native_sdk_diff_handoff_and_token_feedback_without_network(tmp_path,memory):
    openai=pytest.importorskip('openai')
    from openai import _base_client
    from adapter import TaskBroker
    from repo_code.chatgpt_provider import ChatGPTPlanProvider
    from repo_code.chatgpt_auth import RESOURCE
    from repo_code.memory.models import MemoryProfile
    from repo_code.models import Budget
    wire=getattr(_base_client,'httpx2',None) or _base_client.httpx
    seen=[]
    async def handle(request):
        body=json.loads(request.content);seen.append(body)
        assert 'workspace_diff' in {t['name'] for t in body['tools'][0]['tools']}
        status=json.JSONDecoder().raw_decode(body['instructions'].split('Host runtime status (not task acceptance): ',1)[1])[0]
        assert status['budget']['cumulative_tokens']['known_used']==112*(len(seen)-1)
        if len(seen)==1:
            output=[{'type':'function_call','id':'fc_diff','call_id':'diff','name':'workspace_diff',
                     'namespace':'repo_code','arguments':'{}','status':'completed'}]
        else:
            assert any(x.get('type')=='function_call_output' and x.get('call_id')=='diff' for x in body['input'])
            output=[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'Ready for external evaluation; local diff inspected.'}]}]
        event={'type':'response.completed','response':{'id':'resp_'+str(len(seen)),'status':'completed','output':output,
                'usage':{'input_tokens':100,'output_tokens':12}}}
        return wire.Response(200,headers={'content-type':'text/event-stream'},content='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n')
    async with wire.AsyncClient(transport=wire.MockTransport(handle)) as transport:
        async with openai.AsyncOpenAI(api_key='offline-unit-test',base_url=RESOURCE,http_client=transport,max_retries=0) as sdk:
            native=ChatGPTPlanProvider('offline-unit-model',auth=SimpleNamespace(),client=sdk,max_output_tokens=4096,reasoning_effort='medium')
            provider=AuditedProvider(native,tmp_path/'logs')
            profile=MemoryProfile(mode='lexical_only',learning_enabled=False,max_tokens=16000) if memory else None
            app=RepoCode(tmp_path/'state',provider,'local',memory_profile=profile)
            try:
                broker=TaskBroker(app.store,app.runtime,app.sandbox,provider)
                app.broker=app.agent.broker=broker
                if memory:broker.memory=app.memory
                source=tmp_path/'source';source.mkdir();(source/'code.py').write_text('value=0\n')
                run=app.new_task('Inspect the candidate diff then hand off',source,budget=Budget(model_calls=5,child_calls=0))
                result=await app.execute(run['id'])
                assert result['state']=='PARTIAL' and result['reason'].startswith('CANDIDATE_READY_EXTERNAL_EVALUATION')
                assert len(seen)==2 and provider.budget_status()['known_used']==224 and result['delivery'] is None
                assert not app.runtime.unresolved(run['id'])
            finally:app.close()



@pytest.mark.asyncio
async def test_process_fixture_rejection_preserves_diagnostics_without_host_effect(tmp_path):
    source = tmp_path/'source'
    (source/NAME).parent.mkdir(parents=True)
    (source/NAME).write_text(BEFORE)
    app = RepoCode(tmp_path/'state',ScriptedProvider(),'local')
    app.sandbox.mode = 'swebench'
    app.broker.write_validator = PublicTestWritePolicy()
    run = app.new_task('Retain public checks',source,authorize_process=True,authorize_write=True)
    workspace = app.runtime.workspace(run)
    baseline = workspace.snapshot()
    forbidden = app.store.blob('old_case:\n  pass_str: SELECT 1\n')
    files = app.store.read_json(baseline)['files']
    output = app.store.json_blob({'files':{**files,NAME:{**files[NAME],'blob':forbidden}}})

    async def process(*args,**kwargs):
        return {'stdout':'process diagnostics','stderr':'','exit_code':0,'cancelled':False,'output_snapshot':output}

    app.sandbox.run = process
    try:
        request = app.broker.register_call(run['id'],'turn',ToolCall(id='run',name='process.run',arguments={'argv':['pytest']}))
        receipt = await app.broker.submit(request)
        result = receipt['result']
        assert receipt['status'] == 'FAILED'
        assert result['code'] == 'FIXTURE_MUST_APPEND_ONLY' and result['import_rejected']
        assert result['stdout'] == 'process diagnostics' and result['process_exit_code'] == 0
        assert 'output_snapshot' not in result and 'exit_code' not in result
        assert workspace.snapshot() == baseline and app.runtime.run(run['id'])['writer_epoch'] == 0
        assert not app.runtime.unresolved(run['id'])
    finally:
        app.close()
