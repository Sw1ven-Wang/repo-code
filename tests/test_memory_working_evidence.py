"""Real packing/lineage contracts, no external model calls or task answers."""
import json
from types import SimpleNamespace
import pytest
from repo_code.context import groups
from repo_code.models import ToolCall
from repo_code.util import RepoError,digest
from repo_code.providers import ScriptedProvider,model_configuration
from repo_code.memory.models import MemoryProfile
from repo_code.service import RepoCode
from test_memory_v2_contracts import extraction


class NativeScripted(ScriptedProvider):
    model='native-packing-fixture'
    max_tokens=4096
    def build_request(self,system,messages,tools,**options):
        from repo_code.chatgpt_provider import ChatGPTPlanProvider
        return ChatGPTPlanProvider(self.model,auth=SimpleNamespace(),max_output_tokens=4096).build_request(system,messages,tools,**options)


def app_fixture(tmp_path,profile=None,provider=None):
    source=tmp_path/'source';source.mkdir()
    app=RepoCode(tmp_path/'state',provider or ScriptedProvider(handler=extraction),'local',
                 memory_profile=profile or MemoryProfile(mode='lexical_only',learning_enabled=False,max_tokens=16000,reserve_tokens=4096,safety_tokens=500))
    run=app.new_task('Implement the current public API',source,authorize_write=True)
    return app,run


async def tool(app,run,name,args,uid):
    call=ToolCall(id=uid,name=name,arguments=args)
    request=app.broker.register_call(run['id'],uid,call)
    app.store.append_message(run['id'],{'role':'assistant','content':'','tool_calls':[call.model_dump()]})
    result=await app.broker.submit(request);app.broker.append_result(request,result)
    assert result['status']=='SUCCEEDED'
    return result['result']


async def cover(app,run):
    records=app.memory.history.records(run['id']);_,state=app.memory.reducer.reduce(run['id'])
    return await app.memory.compactor.compact(run['id'],records[-1]['seq'],state)


def outputs(messages):return {m['tool_call_id']:json.loads(m['content']) for m in messages if m['role']=='tool'}
def fits(manifest):
    b=manifest['budget_breakdown']
    assert b['estimated_input_total']+b['reserved_output']+b['safety_margin']<=b['max_effective_window']


@pytest.mark.asyncio
async def test_two_current_code_pages_survive_summary_head_and_next_request(tmp_path):
    app,run=app_fixture(tmp_path)
    try:
        code=''.join(f'line_{i:03}=public_contract_value_{i:03}\n' for i in range(160))
        await tool(app,run,'create',{'path':'module.py','content':code},'write')
        await tool(app,run,'read',{'path':'module.py','start':1,'lines':80},'first')
        await tool(app,run,'read',{'path':'module.py','start':81,'lines':80},'second')
        head,_=await cover(app,run);before=app.store.messages(run['id']);calls=app.provider.calls
        for _ in range(2):
            _,messages,manifest=await app.memory.prepare_context(run['id'],[])
            visible=outputs(messages);groups(messages);fits(manifest)
            assert 'line_079=public_contract_value_079' in visible['first']['content']
            assert 'line_080=public_contract_value_080' in visible['second']['content']
            assert manifest['summary_node_ids']==[head]
            assert len({c['id'] for m in messages for c in m.get('tool_calls',[])})==3
        assert app.provider.calls==calls and app.store.messages(run['id'])==before
    finally:app.close()


@pytest.mark.asyncio
async def test_last_successful_patch_backfilled_including_exact_arguments(tmp_path):
    app,run=app_fixture(tmp_path)
    try:
        await tool(app,run,'patch',{'operations':[{'kind':'create','path':'module.py','content':'def public_api():\n    return "code-evidence"\n'*80}]},'patch')
        await cover(app,run)
        _,messages,manifest=await app.memory.prepare_context(run['id'],[])
        patch=next(c for m in messages for c in m.get('tool_calls',[]) if c['id']=='patch')
        original=next(c for m in app.store.messages(run['id']) for c in m.get('tool_calls',[]) if c['id']=='patch')
        assert patch==original and 'patch' in outputs(messages)
        assert any(x['covered_by_summary'] and x['status']=='FULL' for x in app.store.get('projection',run['id'])['working_evidence'])
        groups(messages);fits(manifest)
    finally:app.close()


@pytest.mark.asyncio
async def test_stale_file_version_is_not_rehydrated(tmp_path):
    app,run=app_fixture(tmp_path)
    try:
        await tool(app,run,'create',{'path':'module.py','content':'value=1\n'},'create')
        await tool(app,run,'read',{'path':'module.py'},'old-read')
        await cover(app,run)
        await tool(app,run,'edit',{'path':'module.py','old_text':'value=1','new_text':'value=2'},'change')
        await tool(app,run,'read',{'path':'module.py'},'new-read')
        await cover(app,run)
        _,messages,manifest=await app.memory.prepare_context(run['id'],[])
        result=outputs(messages)
        assert 'new-read' in result and 'old-read' not in result and 'create' not in result
        assert 'value=2' in result['new-read']['content']
        groups(messages);fits(manifest)
    finally:app.close()


@pytest.mark.asyncio
async def test_backfilled_source_revocation_invalidates_envelope(tmp_path):
    app,run=app_fixture(tmp_path)
    try:
        await tool(app,run,'create',{'path':'module.py','content':'value=1\n'},'create')
        await tool(app,run,'read',{'path':'module.py'},'read')
        await cover(app,run)
        _,messages,manifest=await app.memory.prepare_context(run['id'],[])
        assert 'read' in outputs(messages)
        ref=app.memory.history.records(run['id'])[-1]['ref']['source_id']
        app.memory.repo.revoke(ref)
        with pytest.raises(RepoError,match='MEMORY_SOURCE_REVOKED'):app.memory.repo.check([manifest['protocol_envelope_ref']])
    finally:app.close()


@pytest.mark.asyncio
async def test_large_parallel_group_backfill_is_bounded_without_splitting(tmp_path):
    app,run=app_fixture(tmp_path)
    try:
        # Offline pressure fixture; actual Broker records remain immutable.
        for i in range(2):
            await tool(app,run,'create',{'path':f'{i}.py','content':('line of current source '+str(i)+'\n')*1200},f'write-{i}')
        calls=[ToolCall(id=str(i),name='read',arguments={'path':f'{i}.py','lines':500}) for i in range(2)]
        requests=[app.broker.register_call(run['id'],'parallel',c) for c in calls]
        app.store.append_message(run['id'],{'role':'assistant','content':'','tool_calls':[c.model_dump() for c in calls]})
        for req in requests:app.broker.append_result(req,await app.broker.submit(req))
        await cover(app,run);before=app.store.messages(run['id'])
        _,messages,manifest=await app.memory.prepare_context(run['id'],[],policy={'max_tokens':10000})
        visible=outputs(messages)
        assert {'0','1'}<=visible.keys()
        assert any('bounded_preview' in visible[k] for k in ['0','1'])
        assert all('artifact' in visible[k] for k in ['0','1'])
        assert app.store.messages(run['id'])==before
        groups(messages);fits(manifest)
    finally:app.close()


@pytest.mark.asyncio
async def test_budget_pressure_keeps_required_user_material_and_fail_closed(tmp_path):
    app,run=app_fixture(tmp_path)
    try:
        await tool(app,run,'create',{'path':'a','content':'data'},'create')
        await cover(app,run)
        with pytest.raises(RepoError,match='CONTEXT_BUDGET_EXCEEDED|exceed budget'):
            await app.memory.prepare_context(run['id'],[],approved_guidance='Mandatory instruction '*4000,policy={'max_tokens':8000})
    finally:app.close()


@pytest.mark.asyncio
async def test_natural_fixture_pressure_compacts_once_then_refills_native_patch(tmp_path):
    provider=NativeScripted(handler=extraction)
    profile=MemoryProfile(mode='lexical_only',learning_enabled=False,max_tokens=16000,reserve_tokens=4096,safety_tokens=500,
                          protocol_rebase_verified=True,protocol_rebase_profile_digests=[digest(model_configuration(provider))],
                          protocol_rebase_tool_digests=[digest([])])
    app,run=app_fixture(tmp_path,profile,provider)
    try:
        # This pressure is only an offline fixture; no evaluator history injection.
        for i in range(6):app.store.append_message(run['id'],{'role':'assistant','content':'Historical inspection '+str(i)+' evidence '*1000})
        args={'operations':[{'kind':'create','path':'module.py','content':'def public_api():\n    return "current-code"\n'*70}]}
        await tool(app,run,'patch',args,'patch')
        with app.store.tx() as c:
            row=c.execute("SELECT id,data FROM messages WHERE run_id=? AND data LIKE '%tool_calls%' ORDER BY id DESC LIMIT 1",(run['id'],)).fetchone()
            message=json.loads(row['data'])
            message['native_blocks']=[{'type':'function_call','id':'fc_patch','call_id':'patch','name':'patch',
                                       'namespace':'repo_code','arguments':json.dumps(args)}]
            c.execute('UPDATE messages SET data=? WHERE id=?',(json.dumps(message),row['id']))
        before=app.store.messages(run['id'])
        _,messages,manifest=await app.memory.prepare_context(run['id'],[],policy={'max_tokens':8000})
        assert manifest['summary_node_ids'] and provider.calls==1
        assert 'patch' in outputs(messages)
        assert any(m.get('native_blocks')==message['native_blocks'] for m in messages)
        assert app.store.messages(run['id'])==before
        groups(messages);fits(manifest)
    finally:app.close()


@pytest.mark.asyncio
async def test_covered_opaque_group_requires_verified_rebase_for_backfill(tmp_path):
    app,run=app_fixture(tmp_path)
    try:
        await tool(app,run,'create',{'path':'module.py','content':'value=1\n'},'write')
        with app.store.tx() as c:
            row=c.execute("SELECT id,data FROM messages WHERE run_id=? AND data LIKE '%tool_calls%' ORDER BY id DESC LIMIT 1",(run['id'],)).fetchone()
            message=json.loads(row['data']);message['native_blocks']=[{'type':'reasoning','encrypted_content':'OPAQUE_OFFLINE_FIXTURE'}]
            c.execute('UPDATE messages SET data=? WHERE id=?',(json.dumps(message),row['id']))
        await cover(app,run)
        _,messages,manifest=await app.memory.prepare_context(run['id'],[])
        assert 'write' not in outputs(messages)
        assert any(x['status']=='OPAQUE_REBASE_UNVERIFIED' for x in app.store.get('projection',run['id'])['working_evidence'])
        fits(manifest)
    finally:app.close()
