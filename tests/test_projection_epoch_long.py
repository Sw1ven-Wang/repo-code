"""Offline lifecycle tests. None of these are model-quality or paid A/B results."""
import json
import os
import time
from pathlib import Path

import pytest

from repo_code.context import ContextEngine, estimate_tokens, groups
from repo_code.live_compact_state import LiveProjection
from repo_code.models import Assistant, ToolCall
from repo_code.projection_epoch import EpochProjection
from repo_code.util import RepoError, digest
from epoch_helpers import new_app, raw_hash, tool

WINDOW = 262144
RESERVE = 4096


def report(name, body):
    root = os.environ.get('EPOCH_REPORT_DIR')
    if root:
        path = Path(root) / (name + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding='utf-8')


@pytest.mark.parametrize('mode,count', [('live-experimental', 46), ('live-experimental-v2', 150)])
async def test_synthetic_continuous_turns(tmp_path, mode, count):
    app, run = new_app(tmp_path, mode=mode, source_lines=1200)
    rid = run['id']
    from repo_code.providers import OpenAIProvider
    native=OpenAIProvider.__new__(OpenAIProvider)
    native.model='offline-wire';native.max_tokens=RESERVE;native.temperature=None
    native.reasoning_effort=None;native.thinking=None
    serialize=lambda s,m,t:native.build_request(s,m,t)[0]
    engine = ContextEngine(app.store, app.runtime,request_serializer=serialize,output_reserve=RESERVE)
    # Use the frozen Host tool schema as well as the actual fixed output reserve.
    from repo_code.tool_models import definitions
    tools = definitions()
    curve = []
    failure = None
    started = time.monotonic()
    try:
        for stage in range(1, count + 1):
            if stage > 1:
                prompt=f'Task {stage}: preserve API. Correction: no network or hidden tests.'
                # A public, required input burst tests admission against the
                # accumulated uncut prefix. Identical input in both modes;
                # synthetic sizes are never presented as the real B0 trace.
                if stage==46:prompt+='\nPublic interface constraint example. '*6500
                app.submit_turn(rid, str(stage), prompt)
            for n in range(4):
                assert (await tool(app, rid, f'{stage}-{n}', arguments={
                    'path': 'file.py', 'start': stage * 4 + n + 1, 'lines': 500}))['status'] == 'SUCCEEDED'
            if stage % 10 == 0:
                failed = await tool(app, rid, f'error-{stage}', arguments={'path': 'missing.py'})
                assert failed['status'] != 'SUCCEEDED'
                assert (await tool(app, rid, f'retry-{stage}'))['status'] == 'SUCCEEDED'
                edited = await tool(app, rid, f'edit-{stage}', name='edit', arguments={
                    'path': 'file.py', 'old_text': f'counter = {stage // 10 - 1}',
                    'new_text': f'counter = {stage // 10}'})
                assert edited['status'] == 'SUCCEEDED'
            # v1 commits a Cut while this user stage is active; the next Cut
            # locks the intervening final reply and skipped tool groups.
            before = raw_hash(app, rid)
            tick = time.monotonic()
            try:
                system, messages = engine.build(rid, tools, {'max_tokens': WINDOW, 'reserve_tokens': RESERVE})
            except RepoError as exc:
                failure = {'stage': stage, 'code': exc.code, 'detail': str(exc)}
                assert mode == 'live-experimental', failure
                break
            trace = app.store.get('projection', rid)['live_compact']
            assert raw_hash(app, rid) == before
            assert estimate_tokens(serialize(system, messages, tools)) + RESERVE <= WINDOW
            tail_id=f'call-edit-{stage}' if stage % 10 == 0 else f'call-{stage}-3'
            assert any(m.get('tool_call_id') == tail_id for m in messages)
            groups(messages)
            if mode.endswith('v2'):
                assert [m for m in messages if m['role'] == 'user' and m['content'].startswith('Task ')] == [
                    m for m in app.store.messages(rid) if m['role'] == 'user']
            curve.append({'stage': stage, 'provider_estimated_tokens': estimate_tokens(serialize(system, messages, tools)),
                'raw_history_tokens': estimate_tokens(app.store.messages(rid)),
                'generation': trace.get('epoch_generation', 0), 'cut_count': len(trace.get('cuts', [])),
                'reason': trace['reason'], 'seconds': round(time.monotonic() - tick, 4)})
            app.store.append_message(rid, {'role': 'assistant', 'content':
                'MODEL_PROPOSED / UNVERIFIED. ' + 'Historical discussion, not acceptance evidence. ' * 650})
            app.runtime.wait_for_user(rid)
        if mode.endswith('v2'):
            assert len(curve) == 150 and not failure
            assert curve[-1]['generation'] >= 3
            for n in (46, 75, 100, 125, 150):
                assert curve[n - 1]['provider_estimated_tokens'] + RESERVE <= WINDOW
            epoch = EpochProjection(app.store, app.runtime)
            for value in app.store.values('projection_epoch'):
                if value['state'] != 'COMMITTED':
                    continue
                restored = epoch.restore(rid, value['id'])
                end = int(value['body']['source_end'])
                expected = [r['data'] for r in epoch.live.snapshot(rid) if int(r['id']) <= end]
                assert restored == expected
            assert app.provider.calls == 0
        else:
            assert failure and failure['code'] == 'CONTEXT_BUDGET_EXCEEDED'
            assert app.store.get('live_compact_head', rid)
    finally:
        report('synthetic-' + mode, {'category': 'SYNTHETIC_REPRO', 'model_calls': 0,
            'requested_stages': count, 'completed_stages': len(curve), 'failure': failure,
            'window': WINDOW, 'output_reserve': RESERVE, 'curve': curve,
            'serialization':'OpenAIProvider.build_request offline native chat-completions envelope',
            'required_input_burst_stage':46,'synthetic_not_original_trace':True,
            'seconds': round(time.monotonic() - started, 3), 'state_root_private': str(app.store.root)})
        app.close()


async def test_twenty_stages_through_execute_and_native_wire(tmp_path, monkeypatch):
    from repo_code.governance import DEFAULTS
    from repo_code.providers import OpenAIProvider
    monkeypatch.setitem(DEFAULTS, 'context_policy', {'kind': 'config', 'max_tokens': WINDOW, 'reserve_tokens': RESERVE})
    app, run = new_app(tmp_path)
    rid = run['id']
    app.agent.context_ceiling=WINDOW
    received = []

    def respond(system, messages, tools, index):
        groups(messages)
        received.append(estimate_tokens(native.build_request(system,messages,tools)[0]))
        if index % 2 == 0:
            return Assistant(tool_calls=[ToolCall(id=f'parallel-{index}-{n}', name='read',
                arguments={'path': 'file.py', 'lines': 500}) for n in range(2)])
        return Assistant(text='MODEL_PROPOSED, not verification. ' + 'Public task discussion. ' * 2400)

    app.provider.handler = respond
    native = OpenAIProvider.__new__(OpenAIProvider)
    native.model = 'offline-wire';native.max_tokens = RESERVE;native.temperature = None
    native.reasoning_effort = None;native.thinking = None
    app.agent.context.request_serializer = lambda s, m, t: native.build_request(s, m, t)[0]
    started = time.monotonic()
    for stage in range(1, 21):
        if stage > 1:
            app.submit_turn(rid, str(stage), f'Task {stage}: keep public interface unchanged.')
        result = await app.execute(rid)
        assert result['state'] == 'WAITING_USER', result
    epochs = app.store.values('projection_epoch')
    assert epochs and app.provider.calls == 40
    assert all(v + RESERVE <= WINDOW for v in received)
    attempts = app.store.values('live_compact_request_attempt')
    assert len(attempts) == 40 and all(a['status'] == 'COMPLETED' for a in attempts)
    assert any(a['projection_epoch_id'] for a in attempts)
    report('execute-20', {'category': 'OFFLINE_SCRIPTED_LIFECYCLE_NOT_MODEL_SCORE', 'stages': 20,
        'effective_window':WINDOW,'output_reserve':RESERVE,'native_serialization':True,
        'scripted_dispatches': 40, 'paid_calls': 0, 'epochs': len(epochs), 'request_tokens': received,
        'seconds': round(time.monotonic() - started, 3), 'state_root_private': str(app.store.root)})
    app.close()


async def test_thousand_real_broker_groups_restore_and_rebase(tmp_path):
    app, run = new_app(tmp_path, source_lines=1100)
    rid = run['id'];started = time.monotonic()
    for n in range(1000):
        assert (await tool(app, rid, n, arguments={'path': 'file.py', 'start': n + 1, 'lines': 2}))['status'] == 'SUCCEEDED'
    app.store.append_message(rid, {'role': 'assistant', 'content': 'Unverified discussion. ' * 18000})
    app.runtime.wait_for_user(rid)
    app.submit_turn(rid, 'active', 'Current request: no hidden acceptance data.')
    before = raw_hash(app, rid)
    epoch = EpochProjection(app.store, app.runtime)
    opts = dict(system='', tools=[], serialize=lambda s, m, t: [s, m, t], available_history=WINDOW - RESERVE)
    view, trace = epoch.project(rid, groups(app.store.messages(rid)), **opts)
    assert trace['projection_epoch_id'] and trace['provider_view_tokens'] < (WINDOW - RESERVE) * .6
    assert raw_hash(app, rid) == before
    assert epoch.restore(rid, trace['projection_epoch_id']) == app.store.messages(rid)
    assert sum(m['role'] == 'tool' for m in app.store.messages(rid)) == 1000
    groups([m for g in view for m in g])
    report('stress-1000', {'category': 'OFFLINE_STRESS', 'complete_tool_groups': 1000,
        'raw_history_tokens': trace['raw_history_tokens'], 'provider_view_tokens': trace['provider_view_tokens'],
        'raw_source_unchanged': True, 'source_restore_success_rate': 1.0, 'model_calls': 0,
        'seconds': round(time.monotonic() - started, 3), 'state_root_private': str(app.store.root)})
    app.close()
