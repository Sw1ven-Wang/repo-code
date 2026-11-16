"""Review counterexamples: growing tails, disjoint summaries and request attempts."""
import copy
import json
import time

import pytest

from repo_code.context import ContextEngine, estimate_tokens, groups
from repo_code.live_compact import LiveCompactProfile, plan_cut
from repo_code.live_compact_state import LiveProjection
from repo_code.memory.models import MemoryProfile
from repo_code.models import Assistant, Budget, ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.util import RepoError, digest
from live_compact_helpers import history, low_profile, records, source_hashes


def planned(rows, **kw):
    return plan_cut(rows,groups([r['data'] for r in rows]),system='',tools=[],
        serialize=lambda s,m,t:[s,m,t],profile=LiveCompactProfile(**low_profile()),
        available_history=24000,**kw)


def test_adjacent_small_groups_share_one_card_without_splitting_protocol():
    rows=records(count=20,size=1600)
    result=planned(rows)
    p=result.proposal
    assert p and p.group_start<p.group_end
    source=[r for r in rows if r['id'] in p.source_message_ids]
    assert len(groups([r['data'] for r in source]))==p.group_end-p.group_start+1
    assert p.savings_estimate>0 and len(json.loads(p.card['content'])['observations'])>=2
    # An unsafe result is a barrier, never an omitted member of a joint span.
    for row in rows:
        if row['data']['role']=='tool':row['data']['is_error']=True
    assert planned(rows).proposal is None


def test_oversized_successful_result_has_complete_source_reference():
    rows=records(count=8,size=190000);before=copy.deepcopy(rows)
    p=planned(rows).proposal
    assert p and estimate_tokens([r['data'] for r in rows if r['id'] in p.source_message_ids])>32000
    assert json.loads(p.card['content'])['archive_ref']==p.archive_ref
    assert p.savings_estimate>32000 and rows==before


async def test_joint_span_is_durable_and_replays_all_original_groups(tmp_path):
    app,run=await history(tmp_path,count=20,read_lines=80,profile=low_profile())
    live=LiveProjection(app.store,app.runtime);before=source_hashes(app,run['id'])
    slots=groups(app.store.messages(run['id']))
    view,trace=live.project(run['id'],slots,system='',tools=[],serialize=lambda s,m,t:[s,m,t],available_history=24000)
    cut=app.store.get('live_compact_cut',trace['cuts'][0])
    assert cut['group_start']<cut['group_end']
    assert all(view[i]==[] for i in range(cut['group_start']+1,cut['group_end']+1))
    raw=json.loads(app.store.read_blob(cut['archive_ref']))
    assert len(groups(raw['messages']))==cut['group_end']-cut['group_start']+1
    restored,head,_=live.apply(run['id'],live.snapshot(run['id']),slots,app.context_compaction['profile_digest'])
    assert restored==view and head==trace['head'] and source_hashes(app,run['id'])==before
    groups([m for g in view for m in g]);app.close()


async def test_pressure_can_commit_multiple_bounded_cuts_before_fallback(tmp_path):
    app,run=await history(tmp_path,profile=low_profile());before=source_hashes(app,run['id'])
    live=LiveProjection(app.store,app.runtime);slots=groups(app.store.messages(run['id']))
    measure=lambda view:estimate_tokens(['',[m for g in view for m in g],[]])
    limit=measure(slots)-2500
    opts=dict(system='',tools=[],serialize=lambda s,m,t:[s,m,t],available_history=24000,
              fits_limit=lambda messages:estimate_tokens(['',messages,[]])<=limit)
    view,trace=live.project(run['id'],slots,**opts)
    assert 2<=trace['cut_count']<=4 and measure(view)<=limit
    again,repeated=live.project(run['id'],slots,**opts)
    assert again==view and repeated['cut_count']==0 and repeated['head']==trace['head']
    assert source_hashes(app,run['id'])==before;app.close()


@pytest.mark.parametrize('denial',['unowned','revoked'])
async def test_full_result_nested_sources_require_scope_and_revocation_checks(tmp_path,denial):
    app,run=await history(tmp_path,profile=low_profile());live=LiveProjection(app.store,app.runtime)
    row=copy.deepcopy(live.snapshot(run['id'])[2])
    nested=app.store.blob(b'nested source')
    if denial=='revoked':
        app.store.put('run_artifact',digest([run['id'],nested]),{'run_id':run['id'],'artifact':nested})
        app.store.put('revocation',nested,{'reason':'review regression'})
    full=app.store.json_blob({'content':'source '*50000,'artifact':nested,'is_error':False})
    app.store.put('run_artifact',digest([run['id'],full]),{'run_id':run['id'],'artifact':full})
    row['data']['content']=json.dumps({'artifact':full,'truncated':True,'is_error':False})
    assert not live._verify_sources(run['id'],[row])[0]['references_verified']
    app.close()


async def test_changed_receipt_state_invalidates_an_existing_card(tmp_path):
    app,run=await history(tmp_path,profile=low_profile());engine=ContextEngine(app.store,app.runtime)
    engine.build(run['id'],[],{'max_tokens':32000})
    cut=app.store.values('live_compact_cut')[0]
    with app.store.tx() as c:
        operation=c.execute('SELECT operation_id FROM messages WHERE id=?',(cut['source_message_ids'][-1],)).fetchone()[0]
        c.execute("UPDATE operations SET state='UNCERTAIN' WHERE id=?",(operation,))
    with pytest.raises(RepoError,match='LIVE_COMPACT_SOURCE_MISSING'):
        engine.build(run['id'],[],{'max_tokens':32000})
    app.close()


async def read_group(app,run,index,lines=2):
    call=ToolCall(id=f'growth-{index}',name='read',arguments={'path':'f0.py','start':index+1,'lines':lines})
    app.store.append_message(run['id'],{'role':'assistant','content':'Tentative read for the next hypothesis.',
                                     'tool_calls':[call.model_dump(exclude_none=True)]})
    request=app.broker.register_call(run['id'],f'growth-{index}',call)
    receipt=await app.broker.submit(request)
    assert receipt['status']=='SUCCEEDED',receipt
    app.broker.append_result(request,receipt)


async def test_checkpoint_allows_300_more_real_tool_groups_and_tail_folding(tmp_path,record_property):
    app,run=await history(tmp_path,profile=low_profile(),budget=Budget(tool_calls=1000,wall_seconds=1800))
    engine=ContextEngine(app.store,app.runtime)
    _,first=engine.build(run['id'],[],{'max_tokens':32000})
    head=app.store.get('live_compact_head',run['id'])
    prefix=first[:sum(len(g) for g in groups(first)[:head['group_end']+1])]
    durations=[];folded=False
    try:
        for index in range(300):
            await read_group(app,run,index)
            if index%20!=19:continue
            before=source_hashes(app,run['id']);start=time.monotonic()
            system,messages=engine.build(run['id'],[],{'max_tokens':32000})
            durations.append(time.monotonic()-start)
            assert messages[:len(prefix)]==prefix
            assert estimate_tokens([system,messages,[]])+4096<=32000
            assert source_hashes(app,run['id'])==before
            assert any(m.get('tool_call_id')==f'growth-{index}' for m in messages)
            folded |= app.store.get('projection',run['id'])['folded_groups']>0
            groups(messages)
        assert folded and app.provider.calls==0
        record_property('preparation_seconds_by_20_groups',json.dumps(durations))
    finally:app.close()


def extractor(captured):
    def respond(system,messages,tools,index):
        payload=json.loads(messages[0]['content']);rows=payload['new_closed_groups']
        assert not any('live_compact_card' in str(r) or 'memory_segment_data' in str(r) for r in rows)
        captured.append(rows)
        row=next(r for r in rows if r.get('quote_fields'))
        return Assistant(text=json.dumps({'changes':[{'action':'ADD','claim_id':'tail-hypothesis','kind':'HYPOTHESIS',
            'quote_id':row['quote_fields'][0]['quote_id'],'epistemic_status':'INFERRED'}],
            'retained_open_loop_ids':[x['id'] for x in payload['mandatory_open_loops']]}),
            usage={'input_tokens':100,'output_tokens':20})
    return respond


async def grow_assistant_tail(app,run,count=35):
    for index in range(count):
        app.store.append_message(run['id'],{'role':'assistant','content':f'Tentative debug hypothesis {index}. '+'inspect next source '*120})


async def test_live_semantic_live_semantic_lifecycle_and_restart_preserve_sources(tmp_path):
    captured=[];provider=ScriptedProvider(handler=extractor(captured))
    profile=MemoryProfile(mode='lexical_only',learning_enabled=False,max_tokens=32000,reserve_tokens=4096,
                          evidence_tokens=8000,auxiliary_calls=8)
    app,run=await history(tmp_path,profile=low_profile(),memory_profile=profile,provider=provider)
    await app.memory.prepare_context(run['id'],[])
    first_head=app.store.get('live_compact_head',run['id'])
    first_card=copy.deepcopy(app.store.get('live_compact_cut',first_head['id']))
    await grow_assistant_tail(app,run)
    before=source_hashes(app,run['id'])
    _,messages,manifest=await app.memory.prepare_context(run['id'],[])
    assert manifest['summary_segments'] and not manifest['summary_cutoff']
    first_segment=manifest['summary_segments'][0]
    assert first_segment['after_seq']==int(first_card['source_message_ids'][-1])
    assert source_hashes(app,run['id'])==before and captured
    assert all(int(r['ref']['source_id'].split(':')[1])>first_segment['after_seq'] for r in captured[0])
    assert any('memory_segment_data' in m.get('content','') for m in messages)
    for index in range(4):await read_group(app,run,index,lines=250)
    await app.memory.prepare_context(run['id'],[])
    second_head=app.store.get('live_compact_head',run['id'])
    assert second_head['group_end']>first_segment['group_end']
    await grow_assistant_tail(app,run)
    _,messages,manifest=await app.memory.prepare_context(run['id'],[])
    assert len(manifest['summary_segments'])==2
    assert app.store.get('live_compact_cut',first_head['id'])==first_card
    groups(messages)
    original=app.memory.history.records(run['id']);app.close()
    restarted=RepoCode(tmp_path/'host',ScriptedProvider(handler=extractor(captured)),sandbox='local',
        memory_profile=profile,context_compaction='live-experimental',live_compact_profile=low_profile())
    try:
        _,restored,final=await restarted.memory.prepare_context(run['id'],[])
        assert final['summary_segments']==manifest['summary_segments']
        assert restarted.memory.history.records(run['id'])==original
        groups(restored)
        restarted.memory.repo.revoke(first_segment['id'])
        with pytest.raises(RepoError,match='MEMORY_SOURCE_REVOKED'):
            await restarted.memory.prepare_context(run['id'],[])
    finally:restarted.close()


async def test_existing_prefix_summary_does_not_permanently_disable_live_cuts(tmp_path):
    captured=[];profile=MemoryProfile(mode='lexical_only',learning_enabled=False,max_tokens=32000,evidence_tokens=8000)
    app,run=await history(tmp_path,count=4,profile=low_profile(),memory_profile=profile,
                          provider=ScriptedProvider(handler=extractor(captured)))
    rows=app.memory.history.records(run['id']);state=app.memory.reducer.reduce(run['id'])[1]
    node,_=await app.memory.compactor.compact(run['id'],rows[-1]['seq'],state)
    for index in range(5):await read_group(app,run,index,lines=120)
    _,messages,manifest=await app.memory.prepare_context(run['id'],[])
    assert node in manifest['summary_node_ids']
    assert app.store.get('projection',run['id'])['live_compact']['cuts']
    assert app.store.get('live_compact_head',run['id'])['group_end']>=5
    groups(messages);app.close()


@pytest.mark.parametrize('change',['control','revocation'])
async def test_tail_semantic_commit_fences_control_and_revocation(tmp_path,change):
    captured=[];profile=MemoryProfile(mode='lexical_only',learning_enabled=False,max_tokens=32000,evidence_tokens=8000)
    app,run=await history(tmp_path,profile=low_profile(),memory_profile=profile,
                          provider=ScriptedProvider(handler=extractor(captured)))
    await app.memory.prepare_context(run['id'],[])
    head=app.store.get('live_compact_head',run['id'])
    cut=app.store.get('live_compact_cut',head['id'])
    after=int(cut['source_message_ids'][-1])
    rows=app.memory.history.records(run['id']);state=app.memory.reducer.reduce(run['id'])[1]
    def mutate(*args):
        if change=='control':
            with app.store.tx() as c:app.runtime.update(c,app.runtime.run(run['id'],c),control=True)
        else:app.memory.repo.revoke('changed-during-extraction')
        return extractor(captured)(*args)
    app.memory.compactor.provider=ScriptedProvider(handler=mutate)
    try:
        with pytest.raises(RepoError,match='SUMMARY_STALE_FENCE'):
            await app.memory.compactor.compact(run['id'],rows[-1]['seq'],state,after_seq=after)
        assert app.memory.repo.get('memory_control','summary_segments:'+run['id']) is None
        assert app.store.get('live_compact_head',run['id'])==head
    finally:app.close()


@pytest.mark.parametrize('memory_enabled',[False,True])
async def test_dynamic_budget_status_does_not_change_live_system_prefix(tmp_path,memory_enabled):
    profile=MemoryProfile(mode='lexical_only',learning_enabled=False) if memory_enabled else None
    source=tmp_path/'source';source.mkdir()
    app=RepoCode(tmp_path/'host',ScriptedProvider(),sandbox='local',memory_profile=profile,
                 context_compaction='live-experimental')
    run=app.new_task('Preserve the public API',source)
    async def prepare():
        if app.memory:
            system,messages,_=await app.memory.prepare_context(run['id'],[])
            return system,messages
        return ContextEngine(app.store,app.runtime).build(run['id'],[])
    try:
        first_system,first_messages=await prepare()
        app.runtime.consume(run['id'],'model_calls')
        second_system,second_messages=await prepare()
        assert first_system==second_system
        assert first_messages!=second_messages
        assert 'budget' in str(second_messages).lower()
        assert app.runtime.run(run['id'])['data']['budget_used']['model_calls']==1
    finally:app.close()


@pytest.mark.parametrize('failure',[False,True])
async def test_agent_records_each_attempt_and_known_failure_usage(tmp_path,failure):
    class ModelFailure(RuntimeError):usage={'input_tokens':17,'output_tokens':3,'cache_read_tokens':5}
    def model(*args):
        if failure:raise ModelFailure('fixture')
        return Assistant(text='Done',usage={'input_tokens':17,'output_tokens':3,'cache_read_tokens':5})
    source=tmp_path/'source';source.mkdir()
    app=RepoCode(tmp_path/'host',ScriptedProvider(handler=model),sandbox='local',context_compaction='live-experimental')
    run=app.new_task('Inspect task',source)
    try:
        if failure:
            with pytest.raises(ModelFailure):await app.execute(run['id'])
        else:await app.execute(run['id'])
        attempts=app.store.values('live_compact_request_attempt')
        assert len(attempts)==1
        assert attempts[0]['status']==('FAILED' if failure else 'COMPLETED')
        assert attempts[0]['usage']['cache_read_tokens']==5 and attempts[0]['usage_status']=='REPORTED'
        assert not attempts[0]['cache_hit_inferred']
        assert attempts[0]['projection']['history_rows']>=1
    finally:app.close()
