"""Persistent turn semantics under real context pressure; no inference service."""
import json
import pytest

from repo_code.context import estimate_tokens,groups
from repo_code.memory.models import MemoryProfile
from repo_code.models import Assistant,ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.util import RepoError,digest
from test_memory_v2_contracts import extraction


def make_app(tmp_path,memory):
    source=tmp_path/'source';source.mkdir()
    def reply(system,messages,tools,index):
        if not tools and 'new_closed_groups' in messages[0].get('content',''):
            return extraction(system,messages,tools,index).model_copy(update={'usage':{'input_tokens':10,'output_tokens':2}})
        return Assistant(text='Turn complete.',usage={'input_tokens':10,'output_tokens':2})
    profile=MemoryProfile(mode='lexical_only',learning_enabled=False,max_tokens=16000,
                          reserve_tokens=4096,safety_tokens=500,auxiliary_calls=None,total_token_budget=None) if memory else None
    app=RepoCode(tmp_path/'state',ScriptedProvider(handler=reply),'local',memory_profile=profile)
    run=app.new_task('Overall project goal',source,conversation=True,authorize_write=True)
    return app,run


@pytest.mark.asyncio
@pytest.mark.parametrize('memory',[False,True])
async def test_current_turn_drives_policies_and_remains_visible_after_pressure(tmp_path,monkeypatch,memory):
    app,run=make_app(tmp_path,memory);rid=run['id']
    try:
        await app.execute(rid)
        app.store.put('plan',rid,{'steps':['Stale first-turn plan']})
        request='Second request: implement the public symbol dispatch and preserve UTF-8 编码。'
        app.submit_turn(rid,'second',request)
        for i in range(30):
            if i%10==0:app.store.append_message(rid,{'role':'assistant','content':'Earlier implementation note '*700})
            app.store.append_message(rid,{'role':'assistant','content':'','tool_calls':[{'id':f'p{i}','name':'read','arguments':{'path':str(i)}}]})
            app.store.append_message(rid,{'role':'tool','tool_call_id':f'p{i}','content':str(i)+' pressure evidence '*350})
        before=app.store.messages(rid)
        if memory:
            _,state=app.memory.reducer.reduce(rid)
            assert state['current_subgoal']==request and state['current_subgoal_source']=='ACTIVE_USER_REQUEST'
            _,messages,manifest=await app.memory.prepare_context(rid,[],policy={'max_tokens':16000,'reserve_tokens':4096})
            assert manifest['summary_node_ids']
            assert request in json.loads(messages[0]['content'])['working_state']['current_request']
        else:
            system,messages=app.agent.context.build(rid,[],{'max_tokens':16000,'reserve_tokens':4096,'safety_tokens':500})
            assert any(m.get('role')=='user' and m.get('content')==request for m in messages)
            assert estimate_tokens([system,messages,[]])+4596<=16000
            assert app.store.get('projection',rid)['folded_groups']>0
        assert app.store.messages(rid)==before
        groups(messages)
        seen=[];invoke=app.agent.worker.invoke
        async def spy(component,payload,run_id):
            if payload['operation'] in {'memory_selection','next_action_guidance','active_skills'}:seen.append(payload['goal'])
            return await invoke(component,payload,run_id)
        monkeypatch.setattr(app.agent.worker,'invoke',spy)
        result=await app.execute(rid)
        assert result['state']=='WAITING_USER' and seen==[request]*3
        assert app.runtime.run(rid)['data']['goal']=='Overall project goal'
        assert app.store.read_json(run['data']['manifest']['task_spec_digest'])['goal']=='Overall project goal'
        app.submit_turn(rid,'third','Third request')
        app.submit_turn(rid,'second',request)  # A replay cannot roll active state backwards.
        assert app.runtime.current_goal(app.runtime.run(rid))=='Third request'
        if memory:
            key,state=app.memory.reducer.reduce(rid)
            intent=app.memory.reducer.intent(rid,key,state)
            assert state['current_subgoal']=='Third request' and 'Third request' in intent.semantic_query
    finally:app.close()


@pytest.mark.asyncio
async def test_only_current_turn_plan_can_become_memory_subgoal(tmp_path):
    app,run=make_app(tmp_path,True);rid=run['id']
    try:
        await app.execute(rid);app.submit_turn(rid,'two','Implement current API')
        call=ToolCall(id='plan',name='plan.update',arguments={'steps':['Test the current API']})
        req=app.broker.register_call(rid,'model-turn',call)
        assert (await app.broker.submit(req))['status']=='SUCCEEDED'
        key,state=app.memory.reducer.reduce(rid)
        assert state['current_subgoal']=='Test the current API'
        assert 'Implement current API' in app.memory.reducer.intent(rid,key,state).semantic_query
        await app.execute(rid);app.submit_turn(rid,'three','Implement another API')
        assert app.memory.reducer.reduce(rid)[1]['current_subgoal']=='Implement another API'
    finally:app.close()


def archive(app,rid,body):
    ref=app.store.json_blob(body)
    app.store.put('run_artifact',digest([rid,ref]),{'run_id':rid,'artifact':ref})
    return {**body,'result_artifact':ref}


@pytest.mark.asyncio
async def test_legacy_parallel_last_group_reclaims_optional_bodies_without_losing_active_request(tmp_path):
    app,run=make_app(tmp_path,False);rid=run['id']
    try:
        await app.execute(rid);app.submit_turn(rid,'two','Current request must survive folding')
        app.store.append_message(rid,{'role':'assistant','content':'','tool_calls':[
            {'id':'read','name':'read','arguments':{'path':'a'}},
            {'id':'test','name':'process.run','arguments':{'argv':['python','-m','unittest']}}]})
        for call,body in [('read',{'content':'source line 汉字\n'*7000,'path':'a','version':'sha256:file'}),
                          ('test',{'stdout':'test output '*6000,'stderr':'real failure details','exit_code':1,'is_error':True})]:
            app.store.append_message(rid,{'role':'tool','tool_call_id':call,'content':json.dumps(archive(app,rid,body),ensure_ascii=False)})
        before=app.store.messages(rid)
        system,messages=app.agent.context.build(rid,[],{'max_tokens':16000,'reserve_tokens':4096,'safety_tokens':500})
        assert estimate_tokens([system,messages,[]])+4596<=16000
        assert any(m.get('content')=='Current request must survive folding' for m in messages)
        assert len(groups(messages)[-1])==3
        results={m['tool_call_id']:json.loads(m['content']) for m in messages if m['role']=='tool'}
        assert results['read']['version']=='sha256:file'
        assert results['test']['is_error'] and results['test']['exit_code']==1
        assert 'real failure details' in results['test']['bounded_preview']
        for body in results.values():assert app.store.get('run_artifact',digest([rid,body['artifact']]))
        assert app.store.messages(rid)==before
    finally:app.close()


@pytest.mark.asyncio
async def test_oversized_active_request_cannot_be_silently_archived(tmp_path):
    app,run=make_app(tmp_path,False);rid=run['id']
    try:
        await app.execute(rid);app.submit_turn(rid,'two','Mandatory current request '*5000)
        with pytest.raises(RepoError,match='active request'):
            app.agent.context.build(rid,[],{'max_tokens':16000,'reserve_tokens':4096,'safety_tokens':500})
    finally:app.close()
