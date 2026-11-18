"""Incremental progress before low water; bounded scans and exact final admission."""
import copy

import pytest

from repo_code.context import estimate_tokens, groups
from repo_code.live_compact import LiveCompactProfile, plan_cut
from repo_code.live_compact_state import LiveProjection
from repo_code.memory.budget import ContextBudgetCoordinator
from repo_code.util import digest
from live_compact_helpers import records, history, source_hashes
from test_additive_context import engine


def test_incremental_trigger_does_not_wait_for_forecast_watermark():
    rows=records(count=8);slots=groups([r['data'] for r in rows])
    profile=LiveCompactProfile(preserve_reasoning=True,trigger_ratio=0.60,forecast_ratio=0.75)
    serialize=lambda s,m,t:{'system':s,'messages':m,'tools':t}
    history_cost=estimate_tokens(serialize('',[m for g in slots for m in g],[]))-estimate_tokens(serialize('',[],[]))
    available=int(history_cost/0.65)
    args=dict(system='',tools=[],serialize=serialize,profile=profile,available_history=available,scan_start=0,oldest_first=True)
    assert plan_cut(rows,slots,**args).reason=='BELOW_TRIGGER'
    result=plan_cut(rows,slots,trigger_on_history=True,**args)
    assert result.proposal and result.proposal.group_start==1


def test_old_prefix_and_forward_windows_do_not_scan_only_the_tail():
    rows=records(count=90);slots=groups([r['data'] for r in rows])
    profile=LiveCompactProfile(preserve_reasoning=True,max_scan_groups=16)
    args=dict(system='',tools=[],serialize=lambda s,m,t:[s,m,t],profile=profile,available_history=18000,oldest_first=True)
    tail=plan_cut(rows,slots,**args)
    oldest=plan_cut(rows,slots,scan_start=0,trigger_on_history=True,**args)
    assert tail.proposal.group_start>=75 and oldest.proposal.group_start==1
    for row in rows[:31]: row['references_verified']=False
    assert plan_cut(rows,slots,scan_start=0,trigger_on_history=True,**args).reason=='NO_ELIGIBLE_SPAN'
    assert plan_cut(rows,slots,scan_start=16,trigger_on_history=True,**args).proposal.group_start==16


async def test_additive_cuts_before_shared_high_watermark(tmp_path):
    app,run=await history(tmp_path,mode='live-additive',count=8,
        profile={'preserve_reasoning':True,'trigger_ratio':0.1,'forecast_ratio':0.2})
    rid=run['id'];e,p=engine(app);before=source_hashes(app,rid)
    system,view=e.build(rid,[],{'max_tokens':60000,'reserve_tokens':1024})
    projection=app.store.get('projection',rid);trace=projection['live_compact']
    assert trace['cut_count']>0 and not projection['capacity']['governance_triggered']
    assert not projection['folded_groups'] and not trace.get('request_rebase')
    ContextBudgetCoordinator(p,60000,1024,0).assert_fits(system,view,[])
    assert source_hashes(app,rid)==before and app.provider.calls==0
    app.close()


async def test_cut_commits_with_uncompressible_prefix_then_legacy_recovers(tmp_path):
    # A large historical prose prefix cannot be reduced by a local tool card.
    # All candidate minimum-prefix checks would fail low water in the old code.
    from epoch_helpers import new_app,tool,raw_hash
    app,run=new_app(tmp_path,mode='live-additive',source_lines=400,
        live_profile={'preserve_reasoning':True,'trigger_ratio':0.1,'forecast_ratio':0.2,'max_cuts':2,'max_scan_groups':8})
    rid=run['id']
    app.store.append_message(rid,{'role':'assistant','content':'old discussion '*15000,'reasoning_content':'exact old reasoning'})
    for n in range(7):
        assert (await tool(app,rid,n,arguments={'path':'file.py','start':n+1,'lines':350}))['status']=='SUCCEEDED'
    app.runtime.wait_for_user(rid);app.submit_turn(rid,'current','Current task: retain required evidence.')
    before=raw_hash(app,rid);e,p=engine(app)
    system,view=e.build(rid,[],{'max_tokens':20000,'reserve_tokens':1024})
    projection=app.store.get('projection',rid);trace=projection['live_compact']
    assert trace['cut_count']>0 and trace['request_rebase']=='RAW_SOURCE_LEGACY_FOLD'
    assert projection['folded_groups']>0 and projection['capacity']['low_watermark_reached']
    assert trace['candidate_scan_windows'][0][0]==0
    assert all(end-start<=8 for start,end in trace['candidate_scan_windows'])
    assert len(trace['candidate_scan_windows'])<=2
    head=copy.deepcopy(app.store.get('live_compact_head',rid))
    body=LiveProjection(app.store,app.runtime).assert_current(rid,projection['request_projection'])
    assert set(app.store.get('live_compact_cut',head['id'])['source_message_ids']) & set(body['folded_source_ids'])
    assert raw_hash(app,rid)==before
    ContextBudgetCoordinator(p,20000,1024,0).assert_fits(system,view,[])
    groups(p.build_request(system,view,[])[0]['messages'])
    # Reload persistent state: the committed checkpoint and exact source remain
    # verifiable, and a repeated build neither recommits nor grows the chain.
    from repo_code.storage import Store
    restarted=LiveProjection(Store(app.store.root),app.runtime)
    restarted.assert_current(rid,projection['request_projection'])
    e.build(rid,[],{'max_tokens':20000,'reserve_tokens':1024})
    assert app.store.get('live_compact_head',rid)==head
    assert len(app.store.values('live_compact_cut'))==trace['cut_count']
    assert app.provider.calls==0
    app.close()


async def test_scan_cursor_advances_past_ineligible_windows_and_resets_on_progress(tmp_path):
    from epoch_helpers import new_app,tool
    app,run=new_app(tmp_path,mode='live-additive',source_lines=400,
        live_profile={'preserve_reasoning':True,'trigger_ratio':0.1,'forecast_ratio':0.2,'max_cuts':1,'max_scan_groups':4})
    rid=run['id']
    for _ in range(7):app.store.append_message(rid,{'role':'assistant','content':'old uncompressible prose '*150})
    for n in range(5):
        assert (await tool(app,rid,n,arguments={'path':'file.py','start':n+1,'lines':350}))['status']=='SUCCEEDED'
    app.runtime.wait_for_user(rid);app.submit_turn(rid,'now','Current task.')
    e,p=engine(app)
    for expected in [4,8]:
        e.build(rid,[],{'max_tokens':16000,'reserve_tokens':1024})
        state=app.store.get('live_compact_maintenance',rid)
        assert state['scan_next']==expected and not app.store.get('live_compact_head',rid)
        app.store.append_message(rid,{'role':'assistant','content':'new unverified note'})
    e.build(rid,[],{'max_tokens':16000,'reserve_tokens':1024})
    head=app.store.get('live_compact_head',rid)
    assert head and head['group_end']==8
    assert app.store.get('live_compact_maintenance',rid)['scan_next']==9
    app.close()
