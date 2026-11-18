import concurrent.futures
import copy
import json
import pytest
from repo_code.context import ContextEngine,estimate_tokens,groups
from repo_code.live_compact_state import LiveProjection,configuration
from repo_code.projection_epoch import EpochProjection
from repo_code.util import RepoError,Faults,digest
from repo_code.providers import OpenAIProvider
from epoch_helpers import closed_history,options,raw_hash,tool,new_app

async def test_epoch_covers_historical_assistant_and_tools_preserves_users_and_sources(tmp_path):
    app,run=await closed_history(tmp_path);rid=run['id'];epoch=EpochProjection(app.store,app.runtime)
    raw=app.store.messages(rid);before=raw_hash(app,rid)
    view,trace=epoch.project(rid,groups(raw),**options())
    assert trace['projection_epoch_id'] and trace['provider_view_tokens']<=24000
    assert any(v['level']>=2 for v in trace['coverage'])
    flat=[m for g in view for m in g];groups(flat)
    assert [m for m in flat if m['role']=='user' and not m['content'].startswith('{')]==[m for m in raw if m['role']=='user']
    assert raw_hash(app,rid)==before and app.store.messages(rid)==raw
    restored=epoch.restore(rid,trace['projection_epoch_id']);assert restored==raw
    again,value=epoch.replay(rid,epoch.live.snapshot(rid));assert view==again
    epoch.assert_current(rid,trace)
    # Provider-native serialization remains paired after entire closed groups go.
    provider=OpenAIProvider.__new__(OpenAIProvider)
    provider.model='offline';provider.max_tokens=4096;provider.temperature=None
    provider.reasoning_effort=None;provider.thinking=None
    wire,_=provider.build_request('test',flat,[])
    groups(wire['messages'])

async def test_epoch_context_engine_budget_and_dispatch_binding(tmp_path):
    app,run=await closed_history(tmp_path);rid=run['id']
    engine=ContextEngine(app.store,app.runtime)
    system,messages=engine.build(rid,[],{'max_tokens':40000,'reserve_tokens':4096})
    assert estimate_tokens([system,messages,[]])+4096<=40000
    trace=app.store.get('projection',rid)['live_compact'];live=LiveProjection(app.store,app.runtime)
    request={'system':system,'messages':messages,'tools':[]}
    attempt=live.record_request(rid,request,projection=trace)
    assert attempt['projection_epoch_id']==trace['projection_epoch_id']
    assert attempt['serialized_request_digest']==digest(request)
    live.settle_request(attempt,{'input_tokens':123,'output_tokens':2,'cached_tokens':10},0.2)
    assert app.store.get('live_compact_request',rid)['usage']['cached_tokens']==10
    app.store.append_message(rid,{'role':'user','content':'Correction: no network.'})
    with pytest.raises(RepoError,match='EPOCH_STALE'):live.assert_current(rid,trace)

@pytest.mark.parametrize('native',[{'reasoning_content':''},{'reasoning_content':'opaque'},
    {'native_blocks':[{'type':'thinking','data':'opaque'}]},{'unknown_native_key':True}])
async def test_unknown_native_protocol_never_rebased(tmp_path,native):
    app,run=await closed_history(tmp_path);rid=run['id']
    app.store.append_message(rid,{'role':'assistant','content':'opaque',**native});before=raw_hash(app,rid)
    with pytest.raises(RepoError,match='PROTOCOL_REBASE_UNVERIFIED'):
        EpochProjection(app.store,app.runtime).project(rid,groups(app.store.messages(rid)),**options())
    assert not app.store.get('active_projection_epoch',rid) and raw_hash(app,rid)==before

async def test_failed_results_remain_raw_and_never_claim_resolved(tmp_path):
    app,run=new_app(tmp_path);rid=run['id']
    await tool(app,rid,'fail',arguments={'path':'missing.py'})
    app.store.append_message(rid,{'role':'assistant','content':'Tests passed! '*5000})
    app.runtime.wait_for_user(rid);app.submit_turn(rid,'next','Correction: failure is unresolved.')
    raw=app.store.messages(rid);error=next(m for m in raw if m['role']=='tool')
    view,trace=EpochProjection(app.store,app.runtime).project(rid,groups(raw),**options(40000))
    assert error in [m for g in view for m in g]
    assert 'FAILED_THEN_RESOLVED' not in str(view)
    assert raw[-1] in view[-1]

async def test_required_active_context_stops_without_deleting_or_changing_reserve(tmp_path):
    app,run=new_app(tmp_path);rid=run['id']
    app.store.append_message(rid,{'role':'assistant','content':'active discussion '*12000})
    before=raw_hash(app,rid)
    with pytest.raises(RepoError,match='UNCOMPRESSIBLE_REQUIRED_CONTEXT'):
        EpochProjection(app.store,app.runtime).project(rid,groups(app.store.messages(rid)),**options())
    assert before==raw_hash(app,rid) and not app.store.get('active_projection_epoch',rid)

class Crash(BaseException):pass

@pytest.mark.parametrize('point',['epoch_before_source_index','epoch_after_cards','epoch_after_record','epoch_before_cas','epoch_after_activation'])
async def test_five_crash_points_restart_atomicity(tmp_path,point):
    app,run=await closed_history(tmp_path);rid=run['id'];before=raw_hash(app,rid)
    epoch=EpochProjection(app.store,app.runtime,Faults(lambda name:(_ for _ in ()).throw(Crash()) if name==point else None))
    with pytest.raises(Crash):epoch.project(rid,groups(app.store.messages(rid)),**options())
    restart=EpochProjection(app.store,app.runtime);restart.recover(rid)
    view,trace=restart.project(rid,groups(app.store.messages(rid)),**options())
    again,record=restart.replay(rid,restart.live.snapshot(rid))
    assert view==again and record['id']==trace['projection_epoch_id'] and raw_hash(app,rid)==before
    assert not any(v['state']=='PREPARED' for v in app.store.values('projection_epoch'))

async def test_concurrent_cas_and_stale_source(tmp_path):
    app,run=await closed_history(tmp_path);rid=run['id'];epoch=EpochProjection(app.store,app.runtime)
    proposal=epoch.prepare(rid,**options())
    def commit(_):
        try:return EpochProjection(app.store,app.runtime).commit(proposal)['id']
        except RepoError as exc:return exc.code
    with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(commit,range(2)))
    assert set(results)<={digest(proposal.body),'EPOCH_STALE'}
    assert app.store.get('active_projection_epoch',rid)['generation']==1
    stale=epoch.prepare(rid,**options())
    app.store.append_message(rid,{'role':'user','content':'Correction: changed constraint'})
    with pytest.raises(RepoError,match='EPOCH_STALE'):epoch.commit(stale)

@pytest.mark.parametrize('case',['archive','card','pointer','missing_pointer','receipt','revocation','authorization','dependency'])
async def test_corruption_revocation_receipt_and_fences(tmp_path,case):
    app,run=await closed_history(tmp_path);rid=run['id'];epoch=EpochProjection(app.store,app.runtime)
    _,trace=epoch.project(rid,groups(app.store.messages(rid)),**options())
    value=app.store.get('projection_epoch',trace['projection_epoch_id']);body=value['body']
    if case in {'archive','card'}:
        key=body['archive_ref'] if case=='archive' else next(s['card_digest'] for s in body['coverage'] if s['level'])
        (app.store.artifact_root/key.split(':')[1]).write_bytes(b'corrupt')
    if case=='pointer':app.store.put('active_projection_epoch',rid,{'id':'missing','generation':2})
    if case=='missing_pointer':
        with app.store.tx() as c:c.execute("DELETE FROM objects WHERE kind='active_projection_epoch' AND id=?",(rid,))
    if case=='dependency':
        source_ids={i for s in body['coverage'] if s['level'] for i in s['source_ids']}
        row=next(r for r in epoch.live.snapshot(rid) if r['id'] in source_ids and r['data']['role']=='tool')
        ref=json.loads(row['data']['content'])['result_artifact']
        with app.store.tx() as c:c.execute("DELETE FROM objects WHERE kind='run_artifact' AND id=?",(digest([rid,ref]),))
    if case=='revocation':app.store.put('revocation','message:'+body['coverage'][0]['source_ids'][0],{'reason':'test'})
    if case=='receipt':
        with app.store.tx() as c:c.execute("UPDATE operations SET state='UNCERTAIN' WHERE run_id=?",(rid,))
    if case=='authorization':
        with app.store.tx() as c:
            r=app.runtime.run(rid,c);r['data']['authorization']['workspace_write']=False;app.runtime.update(c,r,data=r['data'],control=True)
    with pytest.raises((RepoError,OSError)):
        epoch.assert_current(rid,trace)

async def test_l1_and_memory_history_are_raw_after_epoch(tmp_path):
    from repo_code.episodes import EpisodeBuilder
    from repo_code.memory.repository import MemoryRepository
    from repo_code.memory.working_state import History
    app,run=await closed_history(tmp_path);rid=run['id'];history=History(MemoryRepository(app.store),app.runtime)
    original=history.records(rid);builder=EpisodeBuilder(app.store);ids=builder.capture_run(rid)
    before={k:app.store.get('l1_episode',k) for k in ids};source=raw_hash(app,rid)
    EpochProjection(app.store,app.runtime).project(rid,groups(app.store.messages(rid)),**options())
    assert history.records(rid)==original and source==raw_hash(app,rid)
    assert builder.capture_run(rid)==ids and {k:app.store.get('l1_episode',k) for k in ids}==before

async def test_memory_epoch_shares_raw_coverage_without_auxiliary_model(tmp_path):
    from repo_code.memory.models import MemoryProfile
    profile=MemoryProfile(mode='lexical_only',learning_enabled=False,semantic_consolidation=False,
        max_tokens=64000,reserve_tokens=4096,evidence_tokens=2000,auxiliary_calls=0)
    app,run=await closed_history(tmp_path,memory_profile=profile,count=8);rid=run['id']
    raw=app.memory.history.records(rid)
    system,msg,manifest=await app.memory.prepare_context(rid,[],policy={'max_tokens':64000})
    assert manifest['projection_epoch_id'] and not manifest['summary_node_ids'] and app.provider.calls==0
    assert app.memory.history.records(rid)==raw and estimate_tokens(app.memory.serialized(system,msg,[]))+4096<=64000
    app.memory.admission(manifest)
    epoch=EpochProjection(app.store,app.runtime);p=epoch.prepare(rid,**options(60000));epoch.commit(p)
    with pytest.raises(RepoError,match='EPOCH_STALE'):app.memory.admission(manifest)

def test_v1_configs_unchanged_and_migration_rejected(tmp_path):
    assert configuration('live-experimental')['profile']['version']==1
    app,run=new_app(tmp_path,mode='live-experimental')
    with pytest.raises(RepoError,match='EPOCH_NEW_RUN_REQUIRED'):EpochProjection(app.store,app.runtime).prepare(run['id'],**options())

async def test_watermark_is_not_an_artificial_admission_failure(tmp_path):
    app,run=await closed_history(tmp_path);rid=run['id'];epoch=EpochProjection(app.store,app.runtime)
    # Retained recent groups fit H but cannot meet 0.60 H. Keep them intact.
    view,trace=epoch.project(rid,groups(app.store.messages(rid)),**options(32000))
    assert 19200<trace['provider_view_tokens']<=32000
    assert not trace['low_watermark_reached'] and view

@pytest.mark.parametrize('change',['revocation','writer','control'])
async def test_prepare_commit_rejects_changed_host_fence(tmp_path,change):
    app,run=await closed_history(tmp_path);rid=run['id'];epoch=EpochProjection(app.store,app.runtime)
    p=epoch.prepare(rid,**options())
    if change=='revocation':app.store.put('revocation','message:'+p.source[0]['id'],{'reason':'revoked during prepare'})
    else:
        with app.store.tx() as c:
            app.runtime.update(c,app.runtime.run(rid,c),**{change:True})
    with pytest.raises(RepoError,match='EPOCH_STALE'):epoch.commit(p)
    assert not epoch.pointer(rid)

async def test_memory_segments_use_raw_ranges_and_bind_epoch(tmp_path):
    from repo_code.memory.models import MemoryProfile
    profile=MemoryProfile(mode='lexical_only',learning_enabled=False,semantic_consolidation=False,max_tokens=64000,evidence_tokens=2000)
    app,run=await closed_history(tmp_path,count=8,memory_profile=profile);rid=run['id']
    epoch=EpochProjection(app.store,app.runtime)
    epoch.project(rid,groups(app.store.messages(rid)),**options(60000))
    history=app.memory.history.records(rid)
    state=app.memory.reducer.reduce(rid)[1]
    compactor=app.memory.compactor
    # A complete raw turn boundary, even though it is covered by an epoch.
    after=history[3]['seq'];cutoff=history[7]['seq']
    segment=[r for r in history if after<r['seq']<=cutoff]
    fence=compactor.fence(rid,segment,after)
    key,node=compactor.commit(fence,segment,[],state)
    assert node['raw_source_refs']==[r['ref'] for r in segment]
    assert not any('projection_' in str(r['message']) for r in segment)
    pending=compactor.fence(rid,segment,after)
    epoch.commit(epoch.prepare(rid,**options(60000)))
    with pytest.raises(RepoError,match='SUMMARY_STALE_FENCE'):compactor.commit(pending,segment,[],state)
    assert app.memory.repo.get('summary_nodes',key)==node
    overlap=[r for r in history if r['seq']<=cutoff]
    with pytest.raises(RepoError,match='SUMMARY_SEGMENT_OVERLAP'):
        compactor.commit(compactor.fence(rid,overlap),overlap,[],state)

@pytest.mark.parametrize('usage,expected',[({},None),({'prompt_tokens_details':{'cached_tokens':0}},0),
    ({'input_tokens_details':{'cached_tokens':13}},13),({'cache_read_input_tokens':7},7)])
def test_cache_usage_is_provider_reported_or_unknown(tmp_path,usage,expected):
    app,run=new_app(tmp_path);live=LiveProjection(app.store,app.runtime)
    attempt=live.record_request(run['id'],{'messages':[]})
    result=live.settle_request(attempt,usage,0.1)
    assert result['actual_cache_read_tokens']==expected and result['actual_cache_write_tokens'] is None
    assert result['cache_hit_inferred'] is False

@pytest.mark.parametrize('kind',['archive','card'])
async def test_stale_artifact_read_cannot_restore_a_revoked_original(tmp_path,kind):
    app,run=await closed_history(tmp_path);rid=run['id'];epoch=EpochProjection(app.store,app.runtime)
    _,trace=epoch.project(rid,groups(app.store.messages(rid)),**options())
    b=app.store.get('projection_epoch',trace['projection_epoch_id'])['body']
    span=next(s for s in b['coverage'] if s['level'])
    ref=b['archive_ref'] if kind=='archive' else span['card_digest']
    # Registration may precede revocation. Broker execution must check again.
    from repo_code.models import ToolCall
    req=app.broker.register_call(rid,'stale-source',ToolCall(id='stale-source',name='artifact.read',arguments={'artifact':ref}))
    app.store.put('revocation','message:'+span['source_ids'][0],{'reason':'revoked after request registration'})
    receipt=await app.broker.submit(req)
    assert receipt['status']=='FAILED' and receipt['result']['code']=='LIVE_COMPACT_SOURCE_REVOKED'
    assert not receipt['result'].get('content')
