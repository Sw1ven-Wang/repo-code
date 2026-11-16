import pytest

from repo_code.context import groups
from repo_code.live_compact_state import LiveProjection
from repo_code.memory.models import MemoryProfile
from repo_code.memory.repository import MemoryRepository
from repo_code.memory.working_state import History
from repo_code.util import digest
from live_compact_helpers import history,low_profile,source_hashes


async def test_memory_history_and_reducer_still_consume_original_source(tmp_path):
    profile=MemoryProfile(mode='lexical_only',learning_enabled=False,semantic_consolidation=False,
        max_tokens=32000,reserve_tokens=4096,evidence_tokens=8000,auxiliary_calls=0)
    app,run=await history(tmp_path,profile=low_profile(),memory_profile=profile)
    original=app.memory.history.records(run['id']);raw_hash=source_hashes(app,run['id'])
    system,messages,manifest=await app.memory.prepare_context(run['id'],[],policy={'max_tokens':32000})
    trace=app.store.get('projection',run['id'])['live_compact']
    assert trace['cuts'],trace
    assert app.provider.calls==0  # no semantic-summary or local-card model call
    assert source_hashes(app,run['id'])==raw_hash
    assert app.memory.history.records(run['id'])==original
    assert not manifest['summary_node_ids'] and manifest['summary_cutoff']==0
    for record in original:
        assert 'live_compact_card' not in str(record.get('content',''))
    groups(messages)


async def test_l1_episode_bytes_and_history_refs_do_not_change_after_projection(tmp_path):
    from repo_code.episodes import EpisodeBuilder
    from repo_code.models import ToolCall
    app,run=await history(tmp_path,profile=low_profile())
    call=ToolCall(id='failed-read',name='read',arguments={'path':'missing.py'})
    app.store.append_message(run['id'],{'role':'assistant','content':'','tool_calls':[call.model_dump()]})
    req=app.broker.register_call(run['id'],'failed-read',call)
    receipt=await app.broker.submit(req)
    assert receipt['status']=='FAILED'
    app.broker.append_result(req,receipt)
    app.runtime.admit(run['id'],'user-correction','STEER','Keep existing function names.')
    assert app.runtime.finish_control('user-correction','execution_only')['state']=='RUNNING'
    repo=MemoryRepository(app.store);history_view=History(repo,app.runtime)
    original=history_view.records(run['id']);before=source_hashes(app,run['id'])
    builder=EpisodeBuilder(app.store);ids=builder.capture_run(run['id'])
    assert ids
    episodes={key:app.store.get('l1_episode',key) for key in ids}
    slots=groups(app.store.messages(run['id']))
    _,trace=LiveProjection(app.store,app.runtime).project(run['id'],slots,system='',tools=[],
        serialize=lambda s,m,t:[s,m,t],available_history=20000)
    assert trace['cuts'] and source_hashes(app,run['id'])==before
    assert history_view.records(run['id'])==original
    assert builder.capture_run(run['id'])==ids
    assert {key:app.store.get('l1_episode',key) for key in ids}==episodes
