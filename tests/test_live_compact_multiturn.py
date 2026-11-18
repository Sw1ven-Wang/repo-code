"""Real Broker receipts across completed user turns. No paid inference."""
import json
import pytest

from repo_code.benchmark import TrustedStageLoader,admit_budget
from repo_code.context import ContextEngine,groups
from repo_code.live_compact_state import LiveProjection
from repo_code.memory.repository import MemoryRepository
from repo_code.memory.working_state import History
from repo_code.models import ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.util import canonical,digest
from live_compact_helpers import source_hashes


@pytest.mark.parametrize('turns',[30,100])
async def test_completed_user_turns_cut_without_rewriting_users_or_checkpoint(tmp_path,turns,record_property):
    seed=tmp_path/'seed';seed.mkdir()
    provider=ScriptedProvider()
    app=RepoCode(tmp_path/'host',provider,sandbox='local',context_compaction='live-experimental')
    first='真实用户轮次 0：保留 Unicode Ω 和精确次序。'
    run=app.new_task(first,seed,conversation=True)
    rid=run['id'];loader=TrustedStageLoader(app.runtime)
    admit_budget(app.runtime,rid,experiment_digest=digest('offline-multiturn'),session_model_calls=100,
        session_tool_calls=2000,stage_model_calls=10,stage_tool_calls=20,wall_seconds=3600,stage_seconds=120)
    users=[];prefix=None;earlier=False
    for turn in range(turns):
        text=first if turn==0 else f'真实用户轮次 {turn}：修正前轮假设，保持API。'
        ws=app.runtime.workspace(app.runtime.run(rid))
        payload=(f'# turn {turn} unique fact\n'+'value = "Ω\\n\\\\quoted"\n'*400).encode()
        loader.admit(rid,f'turn-{turn}',{f't{turn}.py':payload},[f't{turn}.py'],
            prompt_digest=digest(text),expected_before=ws.snapshot(),initial=turn==0)
        if turn:app.submit_turn(rid,f'u{turn}',text)
        users.append(text)
        # Two to five groups, including real errors and a valid parallel group.
        for index in range(2+turn%4):
            names=[f't{turn}.py']
            if index==1 and turn%7==0:names+=['missing.py']
            calls=[ToolCall(id=f'c-{turn}-{index}-{j}',name='read',arguments={'path':name,'lines':450}) for j,name in enumerate(names)]
            app.store.append_message(rid,{'role':'assistant','content':'MODEL_PROPOSED only','tool_calls':[c.model_dump(exclude_none=True) for c in calls]})
            for call in calls:
                req=app.broker.register_call(rid,f't-{turn}-{index}',call)
                receipt=await app.broker.submit(req);app.broker.append_result(req,receipt)
        before=source_hashes(app,rid)
        live=LiveProjection(app.store,app.runtime);slots=groups(app.store.messages(rid))
        system,messages=ContextEngine(app.store,app.runtime).build(rid,[],{'max_tokens':32000})
        trace=app.store.get('projection',rid)['live_compact']
        view,_,_=live.apply(rid,live.snapshot(rid),slots,app.context_compaction['profile_digest'])
        groups(messages)
        from repo_code.context import estimate_tokens
        assert estimate_tokens([system,messages,[]])+4096<=32000
        assert source_hashes(app,rid)==before
        assert [m['content'] for m in app.store.messages(rid) if m['role']=='user']==users
        if prefix is not None:assert [m for g in view[:prefix[0]] for m in g]==prefix[1]
        if trace['head']:
            head=trace['head'];prefix=(head['group_end']+1,[m for g in view[:head['group_end']+1] for m in g])
        for cut in app.store.values('live_compact_cut'):
            if cut['cut_state']!='COMMITTED':continue
            raw=json.loads(app.store.read_blob(cut['archive_ref']))
            assert digest(raw)==cut['archive_ref']
            assert not any(m['role']=='user' or m.get('is_error') for m in raw['messages'])
            assert all(not m.get('content','').startswith('{"is_error":true') for m in raw['messages'])
            # Every card covers a single, completed earlier turn.
            call_ids=[m['tool_call_id'] for m in raw['messages'] if m['role']=='tool']
            assert len({c.split('-')[1] for c in call_ids})==1
            assert int(call_ids[0].split('-')[1])<turn
            earlier=True
        groups([m for g in view for m in g])
        app.runtime.wait_for_user(rid)
        if turn==turns//2:
            app.close();app=RepoCode(tmp_path/'host',provider,sandbox='local',context_compaction='live-experimental')
            loader=TrustedStageLoader(app.runtime)
    assert earlier and provider.calls==0
    cuts=[x for x in app.store.values('live_compact_cut') if x['cut_state']=='COMMITTED']
    hist=History(MemoryRepository(app.store),app.runtime)
    found=hist.search(rid,'真实用户轮次 0')
    assert found and hist.resolve(rid,found[0]['reference'])['message']['content']==first
    record_property('committed_cuts',len(cuts));record_property('actual_broker_tool_calls',app.runtime.run(rid)['data']['budget_used']['tool_calls'])
    app.close()
