"""Reasoning-preserving projections retain protocol identity across restart."""
import json

import pytest

from repo_code.context import ContextEngine, groups, mandatory_native_group
from repo_code.live_compact import LiveCompactProfile, card_messages, plan_cut
from repo_code.live_compact_state import LiveProjection
from repo_code.providers import OpenAIProvider
from repo_code.util import RepoError, digest
from live_compact_helpers import history, records, source_hashes, low_profile


@pytest.mark.parametrize('reasoning',['','Exact reasoning Ω\n'])
def test_cut_preserves_each_assistant_and_tool_identity(reasoning):
    rows=records()
    for row in rows:
        if row['data']['role']=='assistant':row['data']['reasoning_content']=reasoning
    profile=LiveCompactProfile(preserve_reasoning=True)
    result=plan_cut(rows,groups([r['data'] for r in rows]),system='',tools=[],serialize=lambda s,m,t:[s,m,t],
                    profile=profile,available_history=18000)
    proposal=result.proposal;assert proposal
    source=[r['data'] for r in rows if r['id'] in proposal.source_message_ids]
    projected=card_messages(proposal.card);groups(projected)
    assert len(source)==len(projected)
    for before,after in zip(source,projected):
        if before['role']=='assistant':assert before==after
        else:
            assert {k:v for k,v in before.items() if k!='content'}=={k:v for k,v in after.items() if k!='content'}
            assert json.loads(after['content'])['archive_ref']==proposal.archive_ref
    provider=OpenAIProvider('gpt-6.1-terra',client=object())
    native=provider.build_request('',projected,[])[0]['messages'][1:]
    assert native[0]['reasoning_content']==reasoning
    assert proposal.group_start==1
    assert mandatory_native_group(projected)


@pytest.mark.parametrize('extra',[{'native_blocks':[{'type':'thinking','signature':'opaque'}]},
                                {'reasoning_content':{}},{'encrypted_content':'opaque'}])
def test_opt_in_still_rejects_opaque_protocol(extra):
    rows=records();rows[1]['data'].update(extra)
    assert plan_cut(rows,groups([r['data'] for r in rows]),system='',tools=[],serialize=lambda s,m,t:[s,m,t],
        profile=LiveCompactProfile(preserve_reasoning=True),available_history=18000).reason=='PROTOCOL_REBASE_UNVERIFIED'


def test_small_existing_projection_does_not_mask_next_eligible_result():
    rows=records();slots=groups([r['data'] for r in rows])
    slots[1]=[slots[1][0],{**slots[1][1],'content':'{}'}]
    result=plan_cut(rows,slots,system='',tools=[],serialize=lambda s,m,t:[s,m,t],
        profile=LiveCompactProfile(preserve_reasoning=True),available_history=18000)
    assert result.proposal and result.proposal.group_start==2


async def test_committed_projection_replays_and_tampering_is_rejected(tmp_path):
    profile={**low_profile(),'preserve_reasoning':True}
    app,run=await history(tmp_path,profile=profile)
    # Add exact native fields before the first projection, as the Provider does.
    with app.store.tx() as c:
        for row in c.execute('SELECT id,data FROM messages WHERE run_id=?',(run['id'],)).fetchall():
            data=json.loads(row['data'])
            if data['role']=='assistant':
                data['reasoning_content']='retained native Ω'
                c.execute('UPDATE messages SET data=? WHERE id=?',(json.dumps(data),row['id']))
    before=source_hashes(app,run['id']);raw=app.store.messages(run['id'])
    engine=ContextEngine(app.store,app.runtime)
    _,projected=engine.build(run['id'],[],{'max_tokens':32000})
    trace=app.store.get('projection',run['id'])['live_compact'];assert trace['cut_count']>0
    assert [m for m in projected if m['role']=='assistant']==[m for m in raw if m['role']=='assistant']
    assert source_hashes(app,run['id'])==before
    live=LiveProjection(app.store,app.runtime);snapshot=live.snapshot(run['id'])
    replay=live.apply(run['id'],snapshot,groups(raw),digest(LiveCompactProfile(**profile)))[0]
    assert [m for g in replay for m in g]==projected[:-1]
    cut=app.store.values('live_compact_cut')[0]
    card=json.loads(app.store.read_blob(cut['card_digest']))
    card['messages'][0]['reasoning_content']='forged'
    forged=app.store.json_blob(card)
    app.store.put('run_artifact',digest([run['id'],forged]),{'run_id':run['id'],'artifact':forged})
    cut['card_digest']=forged;app.store.put('live_compact_cut',cut['id'],cut)
    with pytest.raises(RepoError,match='LIVE_COMPACT_CARD_CORRUPT'):
        live.apply(run['id'],snapshot,groups(raw),digest(LiveCompactProfile(**profile)))
    app.close()


def test_frozen_live_fallback_keeps_original_reasoning_floor(tmp_path):
    from repo_code.service import RepoCode
    from repo_code.providers import ScriptedProvider
    seed=tmp_path/'seed';seed.mkdir();app=RepoCode(tmp_path/'host',ScriptedProvider(),sandbox='local',
        context_compaction='live-experimental',live_compact_profile={'preserve_reasoning':True})
    app.provider.preserve_reasoning=True
    run=app.new_task('mandatory protocol',seed)
    app.store.append_message(run['id'],{'role':'assistant','content':'','reasoning_content':'Ω'*24000})
    app.store.append_message(run['id'],{'role':'user','content':'next'})
    with pytest.raises(RepoError) as caught:
        ContextEngine(app.store,app.runtime,provider=app.provider).build(run['id'],[],{'max_tokens':10000})
    assert caught.value.code=='CONTEXT_BUDGET_EXCEEDED'
    assert len(app.store.messages(run['id']))==3
    app.close()


@pytest.mark.parametrize('mode',['legacy','live-additive'])
def test_shared_fallback_archives_known_reasoning_with_exact_raw_source(tmp_path,mode):
    # The additive contract permits whole old known-string groups to leave the
    # request. It never permits editing a retained field or guessing an opaque
    # native protocol. Frozen experimental mode retains its original floor above.
    from repo_code.service import RepoCode
    from repo_code.providers import ScriptedProvider
    from repo_code.memory.budget import ContextBudgetCoordinator
    seed=tmp_path/'seed';seed.mkdir()
    app=RepoCode(tmp_path/'host',ScriptedProvider(),sandbox='local',context_compaction=mode)
    provider=OpenAIProvider('offline',client=object(),max_tokens=1024)
    provider.preserve_reasoning=True
    run=app.new_task('known-string whole-group recovery',seed);rid=run['id']
    reasoning='Ω'*24000
    app.store.append_message(rid,{'role':'assistant','content':'','reasoning_content':reasoning})
    app.store.append_message(rid,{'role':'user','content':'current task'})
    raw=app.store.messages(rid)
    engine=ContextEngine(app.store,app.runtime,lambda s,m,t:provider.build_request(s,m,t)[0],1024,provider=provider)
    system,view=engine.build(rid,[],{'max_tokens':10000,'reserve_tokens':1024})
    ContextBudgetCoordinator(provider,10000,1024,0).assert_fits(system,view,[])
    groups(provider.build_request(system,view,[])[0]['messages'])
    assert app.store.messages(rid)==raw and view[-1]==raw[-1]
    token=app.store.get('projection',rid)['request_projection']
    body=LiveProjection(app.store,app.runtime).assert_current(rid,token)
    assert app.store.read_json(body['source_ref'])['messages']==raw
    index=app.store.read_json(body['summary_ref']);restored=[]
    for entry in index['groups']:
        group=app.store.read_json(entry['group_artifact'])
        assert group['data_not_authority'] is True
        restored.extend(app.store.read_json(group['raw_source_ref'])['messages'])
    assert raw[1] in restored and raw[1]['reasoning_content']==reasoning
    assert not any(m.get('reasoning_content') for m in view)
    assert app.provider.calls==0
    app.close()


@pytest.mark.parametrize('changes',[{'stderr':'must retain diagnostics'}, {'exit_code':1}, {'running':True}, {'is_error':True}])
def test_reasoning_mode_does_not_relax_result_safety(changes):
    rows=records(result_changes=changes)
    for row in rows:
        if row['data']['role']=='assistant':row['data']['reasoning_content']='exact'
    result=plan_cut(rows,groups([r['data'] for r in rows]),system='',tools=[],serialize=lambda s,m,t:[s,m,t],
        profile=LiveCompactProfile(preserve_reasoning=True),available_history=18000)
    assert not result.proposal
