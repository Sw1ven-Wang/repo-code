"""Actual adapter/Host loop; rejected output never grants tool effects."""
import json
from types import SimpleNamespace as NS
import pytest
from repo_code.models import ToolCall
from repo_code.providers import OpenAIProvider,openai_messages
from repo_code.service import RepoCode
from repo_code.memory.models import MemoryProfile
from repo_code.memory.working_state import semantic_message
from repo_code.util import RepoError
from test_providers_mcp import FakeStream,chunk


class Queue:
    def __init__(self,*streams):self.streams=list(streams);self.requests=[]
    async def create(self,**kw):self.requests.append(kw);return self.streams.pop(0)


def native(*streams):
    q=Queue(*streams);return OpenAIProvider('fixture',client=NS(chat=NS(completions=q)),max_tokens=4096),q


@pytest.mark.asyncio
async def test_bad_json_keeps_original_protocol_and_known_usage():
    raw='{"argv":["python","-c","print(1)")],"timeout":60}'
    p,_=native(FakeStream([chunk(calls=[NS(index=0,id='bad',function=NS(name='process_run',arguments=raw))],finish='tool_calls',usage=NS(prompt_tokens=100,completion_tokens=20))]))
    reply=await p.complete('s',[],[{'name':'process.run','description':'run','parameters':{'type':'object'}}])
    call=reply.tool_calls[0];assert call.argument_error and call.arguments=={} and call.name=='process.run'
    assert reply.usage['completion_tokens']==20
    message={'role':'assistant','content':'','tool_calls':[call.model_dump(exclude_none=True)]}
    assert openai_messages([message])[0]['tool_calls'][0]['function']['arguments']==raw
    assert 'raw_arguments' not in semantic_message(message)['tool_calls'][0]


@pytest.mark.asyncio
async def test_length_response_discards_partial_calls_and_preserves_usage():
    p,_=native(FakeStream([chunk('unfinished',calls=[NS(index=0,id='partial',function=NS(name='create',arguments='{"path":'))],finish='length',usage=NS(prompt_tokens=1000,completion_tokens=4096))]))
    reply=await p.complete('s',[],[])
    assert reply.stop_reason=='length' and not reply.tool_calls and not reply.text
    assert reply.usage['completion_tokens']==4096


@pytest.mark.asyncio
async def test_actual_loop_recovers_invalid_then_length_without_any_tool_effect(tmp_path):
    streams=[FakeStream([chunk(calls=[NS(index=0,id='bad',function=NS(name='create',arguments='{"path":"unexpected.py",'))],finish='tool_calls',usage=NS(prompt_tokens=100,completion_tokens=20))]),
             FakeStream([chunk(calls=[NS(index=0,id='partial',function=NS(name='create',arguments='{"path":"unexpected.py","content":"x"}'))],finish='length',usage=NS(prompt_tokens=110,completion_tokens=4096))]),
             FakeStream([chunk('Investigation complete.',finish='stop',usage=NS(prompt_tokens=120,completion_tokens=5))])]
    p,q=native(*streams);app=RepoCode(tmp_path/'host',p,'local',memory_profile=MemoryProfile(mode='lexical_only',learning_enabled=False))
    source=tmp_path/'source';source.mkdir();run=app.new_task('Inspect without a frozen delivery Contract',source)
    result=await app.execute(run['id'])
    assert result['state']=='PARTIAL' and result['reason'].startswith('CANDIDATE_READY_EXTERNAL_EVALUATION')
    assert result['budget_used']['model_calls']==3 and result['budget_used']['tool_calls']==0
    assert not (app.runtime.workspace(run).root/'unexpected.py').exists()
    assert result['usage']['output_tokens']==4121
    assert all(r['state']=='SETTLED' for r in app.memory.repo.values('memory_budget'))
    tool_results=[json.loads(m['content']) for m in app.store.messages(run['id']) if m['role']=='tool']
    assert tool_results[0]['code']=='INVALID_TOOL_JSON' and len(q.requests)==3
    assert all(r['max_tokens']==4096 for r in q.requests)
    assert len([e for e in app.store.events(run['id'],trusted=True) if e['type']=='model.response_rejected'])==1
    app.close()


@pytest.mark.asyncio
async def test_raw_argument_metadata_is_denied_at_broker_boundary(tmp_path):
    from repo_code.providers import ScriptedProvider
    app=RepoCode(tmp_path/'host',ScriptedProvider(),'local');source=tmp_path/'source';source.mkdir();run=app.new_task('Read only',source)
    with pytest.raises(RepoError) as error:app.broker.register_call(run['id'],'turn',ToolCall(id='x',name='glob',arguments={},argument_error='bad JSON',raw_arguments='{'))
    assert error.value.code=='INVALID_TOOL_JSON' and not app.store.values('host_call')


@pytest.mark.asyncio
async def test_transport_error_after_usage_does_not_erase_known_cost(tmp_path):
    class Broken(FakeStream):
        async def _items(self):
            yield chunk(usage=NS(prompt_tokens=17,completion_tokens=9))
            raise ConnectionResetError('injected disconnect')
    p,_=native(Broken([]));app=RepoCode(tmp_path/'host',p,'local',memory_profile=MemoryProfile(mode='lexical_only',learning_enabled=False))
    source=tmp_path/'source';source.mkdir();run=app.new_task('Inspect',source)
    result=await app.execute(run['id'])
    row=app.memory.repo.values('memory_budget')[0]
    assert row['state']=='SETTLED' and row['charged_tokens']==26
    assert result['usage']['input_tokens']==17 and result['usage']['output_tokens']==9
    assert result['state']=='BLOCKED' and result['reason']=='MODEL_ERROR'


@pytest.mark.asyncio
async def test_truncated_reply_after_cancel_adds_no_recovery_guidance(tmp_path):
    class Cancel(FakeStream):
        async def _items(self):
            app.runtime.admit(run['id'],'cancel','CANCEL')
            yield chunk('partial',finish='length',usage=NS(prompt_tokens=10,completion_tokens=5))
    p,_=native(Cancel([]));app=RepoCode(tmp_path/'host',p,'local',memory_profile=MemoryProfile(mode='lexical_only'))
    source=tmp_path/'source';source.mkdir();run=app.new_task('Inspect',source)
    await app.execute(run['id'])
    assert not any('Runtime response rejected' in m.get('content','') for m in app.store.messages(run['id']))


@pytest.mark.asyncio
async def test_json_auxiliary_mode_is_explicit_in_native_request_and_budget():
    from repo_code.memory.budget import ContextBudgetCoordinator
    p,q=native(FakeStream([chunk('{"changes":[]}',finish='stop',usage=NS(prompt_tokens=100,completion_tokens=5))]))
    profile=MemoryProfile(mode='lexical_only',max_tokens=16000,reserve_tokens=4096,safety_tokens=500)
    coordinator=ContextBudgetCoordinator.auxiliary(p,profile,json_mode=True)
    request=coordinator.serialized('Return JSON',[{'role':'user','content':'facts'}],[])
    assert request['response_format']=={'type':'json_object'}
    assert 'response_format' not in p.build_request('s',[],[])[0]
    await p.complete('Return JSON',[{'role':'user','content':'facts'}],[],**coordinator.request_options)
    assert q.requests[0]==request


@pytest.mark.asyncio
@pytest.mark.parametrize('invent_handle',[False,True])
async def test_quote_handles_resolve_in_host_and_invented_handles_are_denied(tmp_path,invent_handle):
    class References:
        async def create(self,**request):
            assert request['response_format']=={'type':'json_object'}
            payload=json.loads(request['messages'][1]['content'])
            handle=payload['new_closed_groups'][0]['quote_fields'][0]['quote_id']
            text=json.dumps({'changes':[{'claim_id':'decision','kind':'DECISION','quote_id':'invented' if invent_handle else handle,'epistemic_status':'USER_STATED'}],'retained_open_loop_ids':[]})
            return FakeStream([chunk(text,finish='stop',usage=NS(prompt_tokens=100,completion_tokens=30))])
    p=OpenAIProvider('fixture',client=NS(chat=NS(completions=References())),max_tokens=4096)
    app=RepoCode(tmp_path/'host',p,'local',memory_profile=MemoryProfile(mode='lexical_only',learning_enabled=False))
    source=tmp_path/'input';source.mkdir();run=app.new_task('Preserve public interfaces and integer cents.',source)
    records=app.memory.history.records(run['id']);_,state=app.memory.reducer.reduce(run['id'])
    if invent_handle:
        with pytest.raises(RepoError) as error:await app.memory.compactor.compact(run['id'],records[-1]['seq'],state)
        assert error.value.code=='SUMMARY_QUOTE_ID_DENIED'
        assert not app.memory.repo.get('memory_control','summary:'+run['id'])
    else:
        _,node=await app.memory.compactor.compact(run['id'],records[-1]['seq'],state)
        assert node['claims'][0]['text']==run['data']['goal']
        assert node['claims'][0]['support_refs'][0]['source_id']==records[0]['ref']['source_id']
    app.close()
