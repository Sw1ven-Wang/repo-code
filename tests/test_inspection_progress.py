"""Read/history loops stop without changing A/D tools or requiring execution."""
import json
import pytest
from repo_code.context import groups
from repo_code.models import Assistant,Budget,ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.memory.models import MemoryProfile
from repo_code.progress import coalesce_process_observations


def setup(tmp_path,memory=False,provider=None):
    source=tmp_path/'source';source.mkdir();(source/'a.py').write_text('value=0\n'*100)
    profile=MemoryProfile(mode='lexical_only',learning_enabled=False) if memory else None
    app=RepoCode(tmp_path/'state',provider or ScriptedProvider([{'text':'Ready'}]),'local',memory_profile=profile)
    run=app.new_task('Implement current behavior',source,conversation=True,authorize_write=True,budget=Budget(model_calls=40,tool_calls=100))
    return app,run


async def tool(app,rid,index,name,args):
    call=ToolCall(id=str(index),name=name,arguments=args)
    req=app.broker.register_call(rid,'turn-'+str(index),call)
    app.store.append_message(rid,{'role':'assistant','tool_calls':[call.model_dump()]})
    receipt=await app.broker.submit(req);app.broker.append_result(req,receipt)
    return req,receipt


@pytest.mark.asyncio
@pytest.mark.parametrize('memory',[False,True])
async def test_alternating_read_and_archive_loops_have_bounded_executions(tmp_path,memory):
    app,run=setup(tmp_path,memory);rid=run['id']
    try:
        _,first=await tool(app,rid,'r0','read',{'path':'a.py','lines':10})
        artifact=first['result']['result_artifact']
        for i in range(5):
            if i:await tool(app,rid,'r'+str(i),'read',{'path':'a.py','lines':10})
            await tool(app,rid,'a'+str(i),'artifact.read',{'artifact':artifact,'length':100})
        before=app.store.messages(rid)
        view=coalesce_process_observations(before)
        groups(view)
        assert sum('duplicate_inspection_observation' in m.get('content','') for m in view)==8
        req,denied=await tool(app,rid,'denied-read','read',{'path':'a.py','lines':10})
        assert denied['status']=='CANCELLED' and denied['result']['code']=='INSPECTION_NO_PROGRESS'
        assert denied['result']['identical_observations']==5 and not denied['result']['executed']
        assert await app.broker.submit(req)==denied
        _,denied=await tool(app,rid,'denied-archive','artifact.read',{'artifact':artifact,'length':100})
        assert denied['status']=='CANCELLED'
        assert app.runtime.run(rid)['data']['budget_used']['tool_calls']==10
        assert not app.runtime.unresolved(rid)
        # A genuinely new slice remains available, even without a write.
        assert (await tool(app,rid,'new-slice','read',{'path':'a.py','start':11,'lines':10}))[1]['status']=='SUCCEEDED'
        # Editing changes the observation state and permits re-reading the file.
        assert (await tool(app,rid,'edit','edit',{'path':'a.py','old_text':'value=0\n'*100,'new_text':'value=1\n'*100}))[1]['status']=='SUCCEEDED'
        assert (await tool(app,rid,'after-edit','read',{'path':'a.py','lines':10}))[1]['status']=='SUCCEEDED'
    finally:app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('memory',[False,True])
async def test_agent_stops_read_only_repetition_and_keeps_protocol_complete(tmp_path,memory):
    replies=[Assistant(tool_calls=[ToolCall(id=str(i),name='read',arguments={'path':'a.py'})]) for i in range(10)]
    app,run=setup(tmp_path,memory,ScriptedProvider(replies));rid=run['id']
    try:
        result=await app.execute(rid)
        assert (result['state'],result['reason'])==('PARTIAL','NO_PROGRESS')
        assert result['budget_used']['tool_calls']==5 and result['budget_used']['model_calls']==7
        groups(app.store.messages(rid));assert not app.runtime.unresolved(rid)
        assert result['delivery'] is None
    finally:app.close()


@pytest.mark.asyncio
async def test_history_expansion_loop_is_guarded_but_new_page_and_new_turn_are_allowed(tmp_path):
    app,run=setup(tmp_path,True);rid=run['id']
    try:
        reference=app.memory.history.records(rid)[0]['ref']['source_id']
        args={'reference':reference,'length':10}
        for i in range(5):assert (await tool(app,rid,i,'history.expand',args))[1]['status']=='SUCCEEDED'
        assert (await tool(app,rid,'blocked','history.expand',args))[1]['status']=='CANCELLED'
        assert (await tool(app,rid,'new-page','history.expand',{**args,'start':10}))[1]['status']=='SUCCEEDED'
        await app.execute(rid);app.submit_turn(rid,'second','New current request')
        assert (await tool(app,rid,'new-turn','history.expand',args))[1]['status']=='SUCCEEDED'
    finally:app.close()


@pytest.mark.asyncio
async def test_bounded_large_read_retains_full_evidence_identity(tmp_path):
    app,run=setup(tmp_path);rid=run['id']
    try:
        app.runtime.workspace(run).path('a.py').write_text(('long source line '*200+'\n')*40)
        for i in range(5):
            receipt=(await tool(app,rid,i,'read',{'path':'a.py'}))[1]
            assert receipt['result']['truncated'] and receipt['result']['inspection_progress']
        denied=(await tool(app,rid,'blocked','read',{'path':'a.py'}))[1]
        assert denied['result']['code']=='INSPECTION_NO_PROGRESS'
        assert app.runtime.run(rid)['data']['budget_used']['tool_calls']==5
    finally:app.close()


@pytest.mark.asyncio
async def test_agent_can_edit_after_withheld_read_and_then_retest(tmp_path):
    replies=[Assistant(tool_calls=[ToolCall(id=str(i),name='read',arguments={'path':'a.py'})]) for i in range(6)]
    replies += [Assistant(tool_calls=[ToolCall(id='write',name='create',arguments={'path':'fixed.py','content':'value=1\n'})]),
                Assistant(tool_calls=[ToolCall(id='recheck',name='read',arguments={'path':'a.py'})]),Assistant(text='Ready for external checks')]
    app,run=setup(tmp_path,provider=ScriptedProvider(replies));rid=run['id']
    try:
        result=await app.execute(rid)
        assert result['state']=='WAITING_USER'
        assert app.runtime.workspace(app.runtime.run(rid)).path('fixed.py').read_text()=='value=1\n'
        assert result['budget_used']['tool_calls']==7
        groups(app.store.messages(rid))
    finally:app.close()
