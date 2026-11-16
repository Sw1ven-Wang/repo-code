import concurrent.futures
import pytest

from repo_code.context import groups
from repo_code.live_compact import LiveCompactProfile,plan_cut
from repo_code.live_compact_state import LiveProjection
from repo_code.util import Faults,RepoError
from live_compact_helpers import history,source_hashes,low_profile


class Crash(BaseException):pass


@pytest.mark.parametrize('point',['live_before_card','live_original_confirmed','live_card_written','live_before_commit','live_after_commit'])
async def test_five_crash_boundaries_restart_idempotently(tmp_path,point):
    app,run=await history(tmp_path,profile=low_profile());before=source_hashes(app,run['id'])
    live=LiveProjection(app.store,app.runtime,Faults(lambda name:(_ for _ in ()).throw(Crash()) if name==point else None))
    slots=groups(app.store.messages(run['id']))
    opts=dict(system='',tools=[],serialize=lambda s,m,t:[s,m,t],available_history=20000)
    with pytest.raises(Crash):live.project(run['id'],slots,**opts)
    restarted=LiveProjection(app.store,app.runtime)
    view,trace=restarted.project(run['id'],slots,**opts)
    assert trace['cuts'] and source_hashes(app,run['id'])==before
    again,next_trace=restarted.project(run['id'],slots,**opts)
    assert view==again and trace['head']==next_trace['head']
    assert len([x for x in app.store.values('live_compact_cut') if x['cut_state']=='COMMITTED'])==1
    assert not any(x['cut_state']=='PREPARED' for x in app.store.values('live_compact_cut'))


async def test_concurrent_sqlite_commit_has_one_head_and_no_duplicate_cut(tmp_path):
    app,run=await history(tmp_path,profile=low_profile());live=LiveProjection(app.store,app.runtime)
    source=live.snapshot(run['id']);slots=groups([r['data'] for r in source])
    proposal=plan_cut(live._verify_sources(run['id'],source),slots,system='',tools=[],
        serialize=lambda s,m,t:[s,m,t],profile=LiveCompactProfile.model_validate(low_profile()),available_history=20000).proposal
    fence=live.fence(run['id']);before=source_hashes(app,run['id'])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:LiveProjection(app.store,app.runtime).commit(run['id'],source,proposal,fence),range(2)))
    assert results[0]['id']==results[1]['id']
    assert len(app.store.values('live_compact_cut'))==1
    assert source_hashes(app,run['id'])==before


async def test_source_append_between_prepare_and_commit_is_rejected(tmp_path):
    app,run=await history(tmp_path,profile=low_profile());live=LiveProjection(app.store,app.runtime)
    rows=live.snapshot(run['id']);slots=groups([r['data'] for r in rows])
    proposal=plan_cut(live._verify_sources(run['id'],rows),slots,system='',tools=[],serialize=lambda s,m,t:[s,m,t],
        profile=LiveCompactProfile.model_validate(low_profile()),available_history=20000).proposal
    fence=live.fence(run['id'])
    app.store.append_message(run['id'],{'role':'user','content':'New constraint'})
    with pytest.raises(RepoError,match='LIVE_COMPACT_STALE'):live.commit(run['id'],rows,proposal,fence)
    assert not app.store.get('live_compact_head',run['id'])
