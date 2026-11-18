"""Offline integration/safety evidence. No model benchmark claims."""
import copy
import json
import pytest

from repo_code.context import ContextEngine,estimate_tokens,groups
from repo_code.live_compact_state import LiveProjection
from repo_code.memory.budget import ContextBudgetCoordinator
from repo_code.providers import OpenAIProvider,ScriptedProvider
from repo_code.service import RepoCode
from repo_code.storage import Store
from repo_code.util import RepoError,Faults,digest
from epoch_helpers import closed_history,new_app,tool,raw_hash


def engine(app):
    # Actual OpenAI native request builder, without constructing an SDK/client.
    p=OpenAIProvider.__new__(OpenAIProvider)
    p.model='offline';p.max_tokens=1024;p.temperature=None
    p.reasoning_effort='low';p.thinking=None;p.preserve_reasoning=True
    return ContextEngine(app.store,app.runtime,lambda s,m,t:p.build_request(s,m,t)[0],1024,provider=p),p


async def fixture(tmp_path,mode='live-additive',**kwargs):
    return await closed_history(tmp_path,mode=mode,count=8,size=12000,**kwargs)


async def test_live_cut_precedes_fold_and_head_does_not_lock_raw_history(tmp_path):
    app,run=await fixture(tmp_path,live_profile={'trigger_ratio':0.1,'forecast_ratio':0.2})
    rid=run['id'];e,p=engine(app);before=raw_hash(app,rid);raw=copy.deepcopy(app.store.messages(rid))
    system,view=e.build(rid,[],{'max_tokens':30000,'reserve_tokens':1024,'history_recent_groups':2})
    projection=app.store.get('projection',rid);trace=projection['live_compact']
    assert trace['cuts'] and trace['request_rebase']=='RAW_SOURCE_LEGACY_FOLD'
    assert projection['folded_groups']>0
    head=app.store.get('live_compact_head',rid)
    token=projection['request_projection'];live=LiveProjection(app.store,app.runtime)
    body=live.assert_request_view(rid,token)
    cut=app.store.get('live_compact_cut',head['id'])
    assert set(cut['source_message_ids']) & set(body['folded_source_ids'])
    assert app.store.get('live_compact_head',rid)==head
    assert raw_hash(app,rid)==before and app.store.messages(rid)==raw
    assert app.provider.calls==0
    ContextBudgetCoordinator(p,30000,1024,0).assert_fits(system,view,[])
    groups(p.build_request(system,view,[])[0]['messages'])
    assert projection['capacity']['low_watermark_reached']
    archive=app.store.read_json(body['source_ref'])
    assert archive['messages']==raw
    # Raw messages are restored, not an archive of generated cards.
    index=app.store.read_json(body['summary_ref'])
    assert not any('live_compact_card' in app.store.read_blob(g['group_artifact']).decode() for g in index['groups'])
    assert app.store.get('live_compact_cut',head['id'])==cut


async def test_live_success_does_not_need_legacy_fold(tmp_path):
    from live_compact_helpers import history
    app,run=await history(tmp_path,mode='live-additive',count=8,profile={'trigger_ratio':0.1,'forecast_ratio':0.15,'max_cuts':8})
    rid=run['id']
    e,p=engine(app)
    system,view=e.build(rid,[],{'max_tokens':20000,'reserve_tokens':1024})
    projection=app.store.get('projection',rid)
    assert projection['live_compact']['cuts']
    assert projection['folded_groups']==0
    assert not projection['live_compact'].get('request_rebase')
    assert estimate_tokens(p.build_request(system,view,[])[0])+1024<=20000


async def test_live_disabled_baseline_can_fold_same_fixture(tmp_path):
    app,run=await fixture(tmp_path,mode='legacy');e,p=engine(app)
    system,view=e.build(run['id'],[],{'max_tokens':30000,'reserve_tokens':1024,'history_recent_groups':2})
    projection=app.store.get('projection',run['id'])
    assert projection['folded_groups']>0 and 'live_compact' not in projection
    assert projection['capacity']['low_watermark_reached']
    LiveProjection(app.store,app.runtime).assert_current(run['id'],projection['request_projection'])


@pytest.mark.parametrize('change',['message','state','authorization','revocation','operation','new_projection'])
async def test_dispatch_rejects_stale_final_projection(tmp_path,change):
    app,run=await fixture(tmp_path);rid=run['id'];e,p=engine(app)
    e.build(rid,[],{'max_tokens':20000,'reserve_tokens':1024})
    token=app.store.get('projection',rid)['request_projection'];live=LiveProjection(app.store,app.runtime)
    if change=='message':app.store.append_message(rid,{'role':'user','content':'New correction'})
    elif change=='state':app.runtime.wait_for_user(rid)
    elif change=='authorization':
        with app.store.tx() as c:
            r=app.runtime.run(rid,c);r['data']['authorization']['workspace_write']=False;app.runtime.update(c,r,data=r['data'],control=True)
    elif change=='revocation':app.store.put('revocation','message:'+live.snapshot(rid)[0]['id'],{'reason':'test'})
    elif change=='operation':
        with app.store.tx() as c:c.execute("UPDATE operations SET state='UNCERTAIN' WHERE run_id=?",(rid,))
    elif change=='new_projection':e.build(rid,[],{'max_tokens':21000,'reserve_tokens':1024})
    with pytest.raises(RepoError):live.assert_current(rid,token)


@pytest.mark.parametrize('corruption',['request','raw_recovery','original_owned_result'])
async def test_restart_replay_corruption_and_exact_request_binding(tmp_path,corruption):
    app,run=await fixture(tmp_path);rid=run['id'];e,p=engine(app)
    system,view=e.build(rid,[],{'max_tokens':20000,'reserve_tokens':1024})
    token=app.store.get('projection',rid)['request_projection']
    restarted=LiveProjection(Store(app.store.root),app.runtime)
    body=restarted.assert_current(rid,token)
    wire=p.build_request(system,view,[])[0]
    restarted.record_request(rid,wire,projection=token)
    bad=copy.deepcopy(wire);bad['messages'][-1]['content']+='mutated'
    with pytest.raises(RepoError,match='REQUEST_PROJECTION_MISMATCH'):restarted.record_request(rid,bad,projection=token)
    ref=body['request_ref'] if corruption=='request' else app.store.read_json(
        app.store.read_json(body['summary_ref'])['groups'][0]['group_artifact'])['raw_source_ref']
    if corruption=='original_owned_result':
        rows=restarted.snapshot(rid)
        ref=next(json.loads(r['data']['content'])['result_artifact'] for r in rows
            if r['id'] in body['folded_source_ids'] and r['data']['role']=='tool')
    (app.store.artifact_root/ref.split(':')[1]).write_bytes(b'corrupt')
    with pytest.raises(RepoError):restarted.assert_current(rid,token)


@pytest.mark.parametrize('point',['request_view_before_source','request_view_before_cas','request_view_after_cas'])
async def test_projection_publication_is_atomic_on_crash(tmp_path,point):
    app,run=await fixture(tmp_path);rid=run['id'];e,p=engine(app)
    system,view=e.build(rid,[],{'max_tokens':20000,'reserve_tokens':1024})
    old=app.store.get('active_request_projection',rid)
    live=LiveProjection(app.store,app.runtime,Faults(lambda name:(_ for _ in ()).throw(KeyboardInterrupt()) if name==point else None))
    records=live.snapshot(rid);fence=live.fence(rid)
    with pytest.raises(KeyboardInterrupt):
        live.commit_request_view(rid,records,p.build_request(system,view,[])[0],[],None,None,fence)
    pointer=app.store.get('active_request_projection',rid)
    assert pointer==old if point!='request_view_after_cas' else pointer!=old
    value=app.store.get('context_request_projection',pointer['id'])
    assert app.store.verify(value['body'],value['signature'])
    assert app.store.read_json(value['body']['source_ref'])['messages']==app.store.messages(rid)


async def test_revoked_folded_archive_cannot_be_retrieved(tmp_path):
    from repo_code.models import ToolCall
    app,run=await fixture(tmp_path);rid=run['id'];e,p=engine(app)
    e.build(rid,[],{'max_tokens':20000,'reserve_tokens':1024})
    live=LiveProjection(app.store,app.runtime);body=live.assert_current(rid,app.store.get('projection',rid)['request_projection'])
    ref=app.store.read_json(body['summary_ref'])['groups'][0]['group_artifact']
    revoked_id=app.store.read_json(ref)['source_message_ids'][0]
    request=app.broker.register_call(rid,'stale-read',ToolCall(id='stale-read',name='artifact.read',arguments={'artifact':ref}))
    app.store.put('revocation','message:'+revoked_id,{'reason':'revoked'})
    receipt=await app.broker.submit(request)
    assert receipt['status']=='FAILED' and receipt['result']['code']=='LIVE_COMPACT_SOURCE_REVOKED'


@pytest.mark.parametrize('field',[{'native_blocks':[{'type':'thinking','opaque':'protected'}]},{'unknown_signed_field':'protected'}])
async def test_unknown_native_required_context_fails_instead_of_folding(tmp_path,field):
    app,run=new_app(tmp_path,mode='live-additive');rid=run['id'];e,p=engine(app)
    app.store.append_message(rid,{'role':'assistant','content':'protected '*20000,**field})
    app.store.append_message(rid,{'role':'assistant','content':'recent'})
    before=raw_hash(app,rid)
    with pytest.raises(RepoError):e.build(rid,[],{'max_tokens':12000,'reserve_tokens':1024})
    assert raw_hash(app,rid)==before and not app.store.get('active_request_projection',rid)


async def test_l1_raw_episode_evidence_pipeline_unchanged_after_fold(tmp_path):
    from repo_code.episodes import EpisodeBuilder
    from repo_code.memory.evidence_pipeline import EvidencePipeline
    from repo_code.memory.consolidation import Consolidator
    from repo_code.memory.repository import MemoryRepository
    from repo_code.memory.models import MemoryProfile
    app,run=await fixture(tmp_path);rid=run['id']
    await tool(app,rid,'failure',arguments={'path':'missing.py'})
    app.runtime.admit(rid,'correction','STEER','Keep the public interface unchanged.')
    app.runtime.finish_control('correction','execution_only')
    builder=EpisodeBuilder(app.store)
    ids=builder.capture_run(rid,pipeline='legacy');assert ids
    eid=builder.capture_causal(ids[0])
    owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
    pipe=EvidencePipeline(owner)
    before=pipe.build(eid)
    episode=copy.deepcopy(app.store.get('l1_episode',eid))
    episodes={key:copy.deepcopy(app.store.get('l1_episode',key)) for key in ids}
    raw_before=raw_hash(app,rid)
    e,p=engine(app);e.build(rid,[],{'max_tokens':20000,'reserve_tokens':1024})
    assert app.store.get('projection',rid)['folded_groups']>0
    assert raw_hash(app,rid)==raw_before
    after=pipe.build(eid)
    assert before==after
    assert app.store.get('l1_episode',eid)==episode
    assert builder.capture_run(rid,pipeline='legacy')==ids
    assert {key:app.store.get('l1_episode',key) for key in ids}==episodes


@pytest.mark.parametrize('policy',[{'history_high_watermark':0.5,'history_low_watermark':0.7},
    {'history_high_watermark':1.0},{'history_recent_groups':0}])
async def test_invalid_capacity_configuration_rejected(tmp_path,policy):
    app,run=new_app(tmp_path,mode='live-additive');e,p=engine(app)
    with pytest.raises(RepoError,match='INVALID_CONTEXT_WATERMARKS'):e.build(run['id'],[],policy)


async def test_80_continuous_turns_rebase_is_bounded_and_restartable(tmp_path,record_property):
    app,run=new_app(tmp_path,mode='live-additive',source_lines=160,
        live_profile={'preserve_reasoning':True,'trigger_ratio':0.1,'forecast_ratio':0.2})
    rid=run['id'];e,p=engine(app);curve=[];folds=[]
    for stage in range(1,81):
        if stage>1:app.submit_turn(rid,str(stage),'Public task '+str(stage)+': preserve interface.')
        await tool(app,rid,stage)
        # Whole known string-reasoning groups can be archived, but retained
        # native fields are never edited or partially detached from results.
        app.store.append_message(rid,{'role':'assistant','content':'MODEL_PROPOSED_UNVERIFIED '+('historical discussion '*260),
            'reasoning_content':'known string reasoning '+str(stage)})
        before=raw_hash(app,rid)
        system,view=e.build(rid,[],{'max_tokens':14000,'reserve_tokens':1024})
        native=p.build_request(system,view,[])[0]
        groups(native['messages']);used=estimate_tokens(native)
        assert used+1024<=14000 and raw_hash(app,rid)==before
        projection=app.store.get('projection',rid)
        body=LiveProjection(app.store,app.runtime).assert_current(rid,projection['request_projection'])
        assert body['source_digest']==digest(LiveProjection(app.store,app.runtime).snapshot(rid))
        curve.append(used);folds.append(projection['folded_groups'])
        app.runtime.wait_for_user(rid)
        if stage==40:
            app.close()
            app=RepoCode(app.store.root,ScriptedProvider(),sandbox='local',context_compaction='live-additive',
                live_compact_profile={'preserve_reasoning':True,'trigger_ratio':0.1,'forecast_ratio':0.2})
            e,p=engine(app)
    assert folds[-1]>folds[10]>0 and len(app.store.values('context_request_projection'))==80
    for value in app.store.values('run_artifact'):
        if value.get('kind')=='history_group':
            with app.store.connect() as c:
                parents=[r[0] for r in c.execute('SELECT parent FROM lineage WHERE child=?',(value['artifact'],))]
            assert parents==[app.store.read_json(value['artifact'])['raw_source_ref']]
    assert max(curve[40:])<=12976 and app.provider.calls==0
    record_property('stages',80);record_property('peak_native_request_estimated_tokens',max(curve))
    record_property('last_folded_groups',folds[-1]);record_property('paid_calls',0)
    (tmp_path/'curve.json').write_text(json.dumps({'native_request_estimates':curve,'folded_groups':folds}),encoding='utf-8')


async def test_repeated_identical_groups_keep_exact_source_ids(tmp_path):
    app,run=new_app(tmp_path,mode='live-additive');rid=run['id'];e,p=engine(app)
    repeated={'role':'assistant','content':'same unverified discussion '*300}
    for _ in range(20):app.store.append_message(rid,repeated)
    original=LiveProjection(app.store,app.runtime).snapshot(rid)
    e.build(rid,[],{'max_tokens':14000,'reserve_tokens':1024})
    body=LiveProjection(app.store,app.runtime).assert_current(rid,app.store.get('projection',rid)['request_projection'])
    index=app.store.read_json(body['summary_ref']);actual=[]
    for entry in index['groups']:
        group=app.store.read_json(entry['group_artifact'])
        raw=app.store.read_json(group['raw_source_ref'])
        assert raw['source_message_ids']==group['source_message_ids']
        assert raw['messages']==[r['data'] for r in original if r['id'] in raw['source_message_ids']]
        actual.extend(group['source_message_ids'])
    assert set(actual)==set(body['folded_source_ids']) and len(actual)==len(set(actual))
    assert original[-1]['id'] not in body['folded_source_ids']


async def test_corrupt_live_checkpoint_is_not_a_capacity_fallback(tmp_path):
    app,run=await fixture(tmp_path,live_profile={'trigger_ratio':0.1,'forecast_ratio':0.2});rid=run['id'];e,p=engine(app)
    e.build(rid,[],{'max_tokens':30000,'reserve_tokens':1024})
    head=app.store.get('live_compact_head',rid);assert head
    cut=app.store.get('live_compact_cut',head['id'])
    (app.store.artifact_root/cut['archive_ref'].split(':')[1]).write_bytes(b'corrupt')
    with pytest.raises(RepoError):e.build(rid,[],{'max_tokens':30000,'reserve_tokens':1024})


async def test_no_trigger_preserves_baseline_request(tmp_path,monkeypatch):
    from repo_code.live_compact_state import configuration
    app,run=new_app(tmp_path,mode='legacy');rid=run['id'];e,p=engine(app)
    await tool(app,rid,1)
    from repo_code.context import runtime_feedback
    status=runtime_feedback(app.store,app.runtime,app.runtime.run(rid),p)
    monkeypatch.setattr('repo_code.context.runtime_feedback',lambda *args:copy.deepcopy(status))
    before=e.build(rid,[],{'max_tokens':60000,'reserve_tokens':1024})
    # Local fixture only: compare both switches on identical source/state, not
    # a supported migration of a real Run or a paid continuation.
    with app.store.tx() as c:
        r=app.runtime.run(rid,c);r['data']['manifest']['context_compaction']=configuration('live-additive')
        app.runtime.update(c,r,data=r['data'])
    after=e.build(rid,[],{'max_tokens':60000,'reserve_tokens':1024})
    assert before==after
    assert not app.store.get('projection',rid)['live_compact']['cuts']
