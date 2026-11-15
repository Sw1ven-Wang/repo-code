import pytest
from repo_code.service import RepoCode
from repo_code.providers import ScriptedProvider
from repo_code.models import Assistant, Budget
from repo_code.util import RepoError

def fixture(tmp_path,budget=None,conversation=True,memory=False):
    source=tmp_path/'source';source.mkdir()
    p=ScriptedProvider(handler=lambda *a:Assistant(text='Acknowledged.'))
    from repo_code.memory.models import MemoryProfile
    profile=MemoryProfile(mode='lexical_only',learning_enabled=False) if memory else None
    app=RepoCode(tmp_path/'state',p,'local',memory_profile=profile)
    run=app.new_task('First turn',source,budget=budget,conversation=conversation)
    return app,run['id'],p

@pytest.mark.asyncio
@pytest.mark.parametrize('memory',[False,True])
async def test_persistent_history_and_idempotency(tmp_path,memory):
    app,rid,p=fixture(tmp_path,memory=memory)
    assert (await app.execute(rid))['state']=='WAITING_USER'
    app.submit_turn(rid,'2','Second turn')
    app.submit_turn(rid,'2','Second turn')
    assert (await app.execute(rid))['budget_used']['model_calls']==2
    app.submit_turn(rid,'2','Second turn')
    await app.execute(rid)
    assert p.calls==2
    assert [m['content'] for m in app.store.messages(rid) if m['role']=='user']==['First turn','Second turn']
    with pytest.raises(RepoError,match='IDEMPOTENCY_CONFLICT'):app.submit_turn(rid,'2','Different')
    assert app.finish_conversation(rid)['state']=='PARTIAL'
    with pytest.raises(RepoError,match='CONVERSATION_NOT_WAITING'):app.submit_turn(rid,'3','Third')

@pytest.mark.asyncio
async def test_default_single_turn_unchanged(tmp_path):
    app,rid,p=fixture(tmp_path,conversation=False)
    assert (await app.execute(rid))['state']=='PARTIAL'

@pytest.mark.asyncio
async def test_budget_does_not_reset(tmp_path):
    app,rid,p=fixture(tmp_path,Budget(model_calls=1,tool_calls=2,wall_seconds=60))
    await app.execute(rid);app.submit_turn(rid,'2','Second')
    result=await app.execute(rid)
    assert result['reason']=='BUDGET_EXHAUSTED' and p.calls==1

@pytest.mark.asyncio
async def test_unsettled_resource_blocks_next_turn(tmp_path,monkeypatch):
    app,rid,p=fixture(tmp_path);await app.execute(rid)
    monkeypatch.setattr(app.sandbox,'resources_stopped',lambda _:False)
    with pytest.raises(RepoError,match='RESOURCE_LIFETIME_UNKNOWN'):app.submit_turn(rid,'2','Second')
    assert len(app.store.messages(rid))==2

@pytest.mark.asyncio
async def test_cancel_blocks_followup(tmp_path):
    app,rid,p=fixture(tmp_path);await app.execute(rid)
    app.runtime.admit(rid,'cancel','CANCEL')
    with pytest.raises(RepoError):app.submit_turn(rid,'2','Second')
    assert p.calls==1

def test_subscription_output_cap_is_serialized():
    from types import SimpleNamespace
    from repo_code.chatgpt_provider import ChatGPTPlanProvider
    p=ChatGPTPlanProvider('m',auth=SimpleNamespace(),max_output_tokens=4096)
    assert p.build_request('s',[{'role':'user','content':'u'}],[])[0]['max_output_tokens']==4096
    assert 'max_output_tokens' not in ChatGPTPlanProvider('m',auth=SimpleNamespace()).build_request('s',[],[])[0]

@pytest.mark.asyncio
async def test_active_clock_does_not_reset_between_turns(tmp_path,monkeypatch):
    import repo_code.runtime as module
    clock=[100.0];monkeypatch.setattr(module,'now',lambda:clock[0])
    app,rid,p=fixture(tmp_path,Budget(wall_seconds=10))
    clock[0]=106.0;await app.execute(rid)
    clock[0]=200.0;app.submit_turn(rid,'2','Second')
    assert app.runtime.active_seconds(app.runtime.run(rid))==6.0
    clock[0]=205.0
    assert (await app.execute(rid))['reason']=='BUDGET_EXHAUSTED'
