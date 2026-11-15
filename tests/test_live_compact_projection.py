import json
import pytest

from repo_code.context import ContextEngine,groups
from repo_code.live_compact_state import LiveProjection
from repo_code.models import ToolCall
from repo_code.service import RepoCode
from repo_code.providers import ScriptedProvider
from repo_code.util import RepoError,digest
from live_compact_helpers import history,source_hashes,low_profile


async def test_projection_preserves_original_ledger_and_retrieves_exact_source(tmp_path):
    app,run=await history(tmp_path,profile=low_profile())
    before=source_hashes(app,run['id']);raw=app.store.messages(run['id'])
    engine=ContextEngine(app.store,app.runtime)
    system,messages=engine.build(run['id'],[],{'max_tokens':32000})
    trace=app.store.get('projection',run['id'])['live_compact']
    assert trace['cut_count']==1 and trace['cuts']
    assert source_hashes(app,run['id'])==before and app.store.messages(run['id'])==raw
    groups(messages)
    cut=app.store.get('live_compact_cut',trace['cuts'][0])
    req=app.broker.register_call(run['id'],'restore',ToolCall(id='restore',name='artifact.read',arguments={'artifact':cut['archive_ref'],'length':16000}))
    receipt=await app.broker.submit(req)
    assert receipt['status']=='SUCCEEDED'
    archive=json.loads(receipt['result']['content'])
    assert archive['source_message_ids']==cut['source_message_ids']
    assert archive['messages']==raw[9:11]


async def test_restart_and_repeated_build_have_identical_cards_and_monotonic_head(tmp_path):
    app,run=await history(tmp_path,profile=low_profile())
    engine=ContextEngine(app.store,app.runtime);first=engine.build(run['id'],[],{'max_tokens':32000})
    old=app.store.get('live_compact_head',run['id'])
    second=engine.build(run['id'],[],{'max_tokens':32000})
    assert first[1]==second[1]
    assert app.store.get('projection',run['id'])['live_compact']['reason']=='UNCHANGED_SNAPSHOT'
    restarted=RepoCode(tmp_path/'host',ScriptedProvider(),sandbox='local',context_compaction='live-experimental',live_compact_profile=low_profile())
    assert ContextEngine(restarted.store,restarted.runtime).build(run['id'],[],{'max_tokens':32000})[1]==first[1]
    assert restarted.store.get('live_compact_head',run['id'])==old
    assert len(restarted.store.values('live_compact_cut'))==1


async def test_corrupt_or_missing_committed_source_fails_closed(tmp_path):
    app,run=await history(tmp_path,profile=low_profile())
    engine=ContextEngine(app.store,app.runtime);engine.build(run['id'],[],{'max_tokens':32000})
    cut=app.store.values('live_compact_cut')[0]
    (app.store.artifact_root/cut['archive_ref'][7:]).write_bytes(b'corrupt')
    with pytest.raises(RepoError,match='ARTIFACT_CORRUPTION'):engine.build(run['id'],[],{'max_tokens':32000})


async def test_scope_denied_and_unsettled_operations_do_not_cut(tmp_path):
    app,run=await history(tmp_path,profile=low_profile())
    req=app.broker.register_call(run['id'],'pending',ToolCall(id='pending',name='read',arguments={'path':'f0.py'}))
    app.store.put('pending_turn',run['id'],{'requests':[req.model_dump()]})
    ContextEngine(app.store,app.runtime).build(run['id'],[],{'max_tokens':32000})
    trace=app.store.get('projection',run['id'])['live_compact']
    assert trace['reason']=='HOST_NOT_SETTLED' and not trace['cuts']


async def test_default_legacy_is_unchanged_and_resume_requires_same_profile(tmp_path):
    app,run=await history(tmp_path,mode='legacy')
    assert 'context_compaction' not in run['data']['manifest']
    engine=ContextEngine(app.store,app.runtime);engine.build(run['id'],[],{'max_tokens':32000})
    assert 'live_compact' not in app.store.get('projection',run['id'])
    switched=RepoCode(tmp_path/'host',ScriptedProvider(),sandbox='local',context_compaction='live-experimental')
    with pytest.raises(RepoError,match='Resume with the original'):await switched.execute(run['id'])


async def test_epoch_and_revocation_fence_reject_stale_dispatch(tmp_path):
    app,run=await history(tmp_path,profile=low_profile())
    live=LiveProjection(app.store,app.runtime)
    token={'fence':live.fence(run['id'])}
    app.runtime.admit(run['id'],'steer-event','STEER','Keep Unicode too')
    with pytest.raises(RepoError,match='LIVE_COMPACT_STALE'):live.assert_current(run['id'],token)


async def test_checkpoint_is_never_refolded_on_budget_pressure(tmp_path):
    app,run=await history(tmp_path,profile=low_profile())
    engine=ContextEngine(app.store,app.runtime);engine.build(run['id'],[],{'max_tokens':24000})
    head=app.store.get('live_compact_head',run['id'])
    with pytest.raises(RepoError):
        engine.build(run['id'],[],{'max_tokens':6000,'reserve_tokens':4096})
    assert app.store.get('live_compact_head',run['id'])==head


async def test_native_prefix_metric_is_not_a_cache_hit(tmp_path):
    app,run=await history(tmp_path)
    live=LiveProjection(app.store,app.runtime)
    live.record_request(run['id'],{'messages':['abc']})
    metric=live.record_request(run['id'],{'messages':['abcd']})
    assert metric['common_prefix_bytes']>0 and not metric['cache_hit_inferred']
    assert metric['usage'] is None


@pytest.mark.parametrize('mode',['legacy','live-experimental'])
@pytest.mark.parametrize('memory_enabled',[False,True])
async def test_benchmark_trials_inherit_selected_compaction_profile(tmp_path,monkeypatch,mode,memory_enabled):
    from repo_code import evaluation
    from repo_code.memory.models import MemoryProfile
    profile=low_profile() if mode=='live-experimental' else None
    app=RepoCode(tmp_path/'host',ScriptedProvider(),sandbox='local',context_compaction=mode,
        live_compact_profile=profile,
        memory_profile=MemoryProfile(mode='lexical_only',learning_enabled=False) if memory_enabled else None)
    class Captured(BaseException):pass
    seen={}
    def capture(*args,**kwargs):
        seen.update(kwargs)
        raise Captured()
    monkeypatch.setattr(evaluation,'RepoCode',capture)
    try:
        runner=evaluation.TrialRunner(app,lambda _:ScriptedProvider())
        with pytest.raises(Captured):
            await runner.run({'id':'public-task','goal':'Public input','files':{}},app.releases.active()['digest'],'fixture')
        assert seen['context_compaction']==mode
        assert seen['live_compact_profile']==profile
        assert bool(seen['memory_profile'])==memory_enabled
        assert app.provider.calls==0
    finally:app.close()
