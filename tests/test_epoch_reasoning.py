"""Explicit relay string-reasoning compatibility; never a model score."""
import copy
import json
import pytest
from repo_code.context import ContextEngine,groups,estimate_tokens
from repo_code.models import ToolCall
from repo_code.projection_epoch import EpochProjection
from repo_code.providers import OpenAIProvider
from repo_code.util import RepoError
from epoch_helpers import new_app,raw_hash,options


async def native_tool(app,rid,n):
    calls=[ToolCall(id=f'c{n}-{j}',name='read',arguments={'path':'file.py','lines':500}) for j in range(2)]
    assistant={'role':'assistant','content':'Inspect public input.',
        'reasoning_content':'' if n%2 else 'Native relay reasoning '+str(n),
        'tool_calls':[c.model_dump(exclude_none=True) for c in calls]}
    app.store.append_message(rid,assistant)
    for call in calls:
        request=app.broker.register_call(rid,'native-'+str(n),call)
        receipt=await app.broker.submit(request);assert receipt['status']=='SUCCEEDED'
        app.broker.append_result(request,receipt)


async def test_native_epoch_150_turns_exact_assistants_pairing_and_restart(tmp_path):
    app,run=new_app(tmp_path,source_lines=150,live_profile={'preserve_reasoning':True});rid=run['id']
    provider=OpenAIProvider.__new__(OpenAIProvider)
    provider.model='offline-wire';provider.max_tokens=4096;provider.temperature=None
    provider.reasoning_effort='low';provider.thinking=None;provider.preserve_reasoning=True
    serialize=lambda s,m,t:provider.build_request(s,m,t)[0]
    engine=ContextEngine(app.store,app.runtime,request_serializer=serialize,output_reserve=4096,provider=provider)
    from repo_code.tool_models import definitions
    schema=definitions();curve=[]
    try:
        for stage in range(1,151):
            if stage>1:app.submit_turn(rid,str(stage),'Task '+str(stage)+': preserve API.')
            await native_tool(app,rid,stage)
            before=raw_hash(app,rid);raw=app.store.messages(rid)
            system,view=engine.build(rid,schema,{'max_tokens':262144,'reserve_tokens':4096})
            assert raw_hash(app,rid)==before
            assert [m for m in view if m['role']=='assistant']==[m for m in raw if m['role']=='assistant']
            assert [m.get('tool_call_id') for m in view if m['role']=='tool']==[m['tool_call_id'] for m in raw if m['role']=='tool']
            groups(view);wire=serialize(system,view,schema)
            assert estimate_tokens(wire)+4096<=262144
            trace=app.store.get('projection',rid)['live_compact'];curve.append(trace['epoch_generation'])
            if trace['projection_epoch_id'] and (trace['reason']=='EPOCH_REBASED' or stage in (46,75,100,125,150)):
                epoch=EpochProjection(app.store,app.runtime)
                replay,record=epoch.replay(rid,epoch.live.snapshot(rid));epoch.assert_current(rid,trace)
                assert [m for g in replay for m in g if m['role']=='assistant']==[m for m in raw if m['role']=='assistant']
            app.store.append_message(rid,{'role':'assistant','content':'Proposed implementation complete.',
                'reasoning_content':'' if stage%2 else 'Retained final reasoning.'})
            app.runtime.wait_for_user(rid)
        assert curve[-1]>=3
        assert app.provider.calls==0
    finally:app.close()


@pytest.mark.parametrize('native',[{'native_blocks':[{'type':'reasoning','encrypted_content':'opaque'}]},
    {'reasoning_content':{'text':'unknown'}},{'unknown_signed_field':'opaque'}])
async def test_native_epoch_still_rejects_unverified_protocol(tmp_path,native):
    app,run=new_app(tmp_path,live_profile={'preserve_reasoning':True});rid=run['id']
    app.store.append_message(rid,{'role':'assistant','content':'data '*15000,**native})
    before=raw_hash(app,rid)
    with pytest.raises(RepoError,match='PROTOCOL_REBASE_UNVERIFIED'):
        EpochProjection(app.store,app.runtime).prepare(rid,**options())
    assert before==raw_hash(app,rid) and not app.store.get('active_projection_epoch',rid)
    app.close()


async def test_native_card_corruption_cannot_drop_assistant(tmp_path):
    app,run=new_app(tmp_path,live_profile={'preserve_reasoning':True});rid=run['id']
    for n in range(8):
        if n:app.submit_turn(rid,str(n),'Task '+str(n))
        await native_tool(app,rid,n);app.runtime.wait_for_user(rid)
    app.submit_turn(rid,'active','Current task')
    epoch=EpochProjection(app.store,app.runtime);proposal=epoch.prepare(rid,**options(70000))
    original=copy.deepcopy(app.store.messages(rid));epoch.commit(proposal)
    view,record=epoch.replay(rid,epoch.live.snapshot(rid))
    assert [m for g in view for m in g if m['role']=='assistant']==[m for m in original if m['role']=='assistant']
    key=next(iter(proposal.cards));(app.store.artifact_root/key.split(':')[1]).write_bytes(b'corrupt')
    with pytest.raises(RepoError):epoch.replay(rid,epoch.live.snapshot(rid))
    assert app.store.messages(rid)==original
    app.close()
