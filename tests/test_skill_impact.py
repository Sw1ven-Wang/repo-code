"""Skill Impact contract and report regression (I01–I15)."""
import copy
from contextlib import closing
import json
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from repo_code.acceptance import GRADER_VERSION
from repo_code.skill_impact import (collect_run_skill_trace, compare_skill_pairs, impact_plan,
                                   _observed_usage, persist_skill_impact)
from repo_code.storage import Store
from repo_code.util import canonical, digest


@pytest.fixture
def trace_store(tmp_path):
    store = Store(tmp_path / 'state')
    manifest = {'release_digest': 'release', 'contract_digest': 'grader-contract', 'environment_digest': 'docker'}
    run = 'skill-run'
    with store.tx() as db:
        db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)',
                   (run, 'task', 'SUCCEEDED', 0, 7, 5, 0,
                    canonical({'manifest': manifest, 'visibility': 'DEV'}).decode()))
    common = {'release_digest': 'release', 'contract_digest': 'grader-contract'}
    refs = []
    for name in ('target', 'other'):
        contract = {'steps': ['Inspect'], 'step_tools': ['read'],
                    'step_rules': [{'tool': 'read', 'arguments_match': {'path': 'main.py'}}],
                    'skill_id': name, 'binding_slots': {'source': {'kind': 'path'}}}
        ref = digest(contract)
        store.put('capability_contract', ref, contract)
        refs.append(ref)

    def route(ref, *, considered=True, offered=True, bind=True, apply=True, bad_signature=False, receipt_run=None):
        id_ = digest([ref, run])
        selected = [ref] if offered else []
        store.put('capability_route', id_, {'id': id_, 'run_id': run, **common, 'control_epoch': 7,
                  'considered_contract_refs': [ref] if considered else [], 'contract_refs': selected})
        if not offered:
            return None
        with store.tx() as db:
            seq = store.emit(db, run, 'capability.offered', {'route_id': id_})
        store.put('capability_offer', id_, {'route_id': id_, 'run_id': run, **common, 'control_epoch': 7,
                  'offer_sequence': seq, 'offered': True, 'contract_refs': selected})
        key = None
        if bind:
            key = digest([run, id_, ref])
            with store.tx() as db:
                seq = store.emit(db, run, 'capability.bound', {'contract_ref': ref, 'binding_ref': key})
            payload = {'sequence': seq, 'run_id': run, 'route_id': id_, 'contract_ref': ref,
                       'environment': 'docker', 'control_epoch': 7,
                       'step_rules': store.get('capability_contract', ref)['step_rules']}
            store.put('capability_binding', key, {'payload': payload,
                      'signature': 'tampered' if bad_signature else store.sign(payload)})
        proof = []
        if apply:
            op = 'op-' + id_
            args = {'path': 'main.py'}
            request = {'tool_name': 'read', 'arguments': args}
            result = {'content': 'visible file'}
            with store.tx() as db:
                db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?)',
                           (op, op, run, 'SUCCEEDED', 'semantic', canonical(request).decode(),
                            canonical(result).decode(), 7, 0))
                e = store.emit(db, run, 'tool.operation_finished',
                               {'operation_id': op, 'tool_name': 'read', 'action_digest': digest(['read', args]),
                                'result': result})
                event_id = db.execute('SELECT event_id FROM events WHERE sequence=?', (e,)).fetchone()[0]
            proof = [{'event_id': event_id, 'sequence': e, 'tool': 'read', 'operation_id': op}]
        receipt = {'run_id': receipt_run or run, 'capability_contract_ref': ref, 'route_id': id_, **common,
                   'offered': True, 'historical': False, 'binding_ref': key, 'step_evidence': proof,
                   'applied': apply, 'verified': False}
        rid = digest(receipt)
        store.put('application_receipt', rid, receipt)
        return rid

    return store, run, refs[0], refs[1], route


@pytest.mark.parametrize('bind,apply', [(False, False), (True, False), (True, True)])
def test_target_funnel_distinguishes_offer_binding_and_executed_steps(trace_store, bind, apply):
    """I02, I03, I05."""
    store, run, target, _, route = trace_store
    rid = route(target, bind=bind, apply=apply)
    traces, complete = collect_run_skill_trace(store, run, {'application_receipts': [rid]})
    trace = traces[target]
    assert complete and trace['source_status'] == 'COMPLETE'
    assert (trace['retrieved'], trace['offered'], trace['bound'], trace['applied'], trace['verified']) == (
        True, True, bind, apply, False)
    assert trace['receipt_refs'] == [rid]
    assert len(trace['event_refs']) == 1 + bind + apply


def test_other_skill_never_applies_to_target(trace_store):
    """I01, I04: a different Skill's success cannot satisfy the target."""
    from repo_code.skill_impact import normalize_trial
    store, run, target, other, route = trace_store
    rid = route(other)
    traces, complete = collect_run_skill_trace(store, run, {'application_receipts': [rid]})
    sample = {'skill_traces': traces, 'skill_trace_inventory_complete': complete}
    assert traces[other]['applied'] is True and target not in traces
    assert normalize_trial(sample, target)['trace']['applied'] is False
    assert normalize_trial(sample, target)['trace']['offered'] is False


@pytest.mark.parametrize('fault', ['signature', 'wrong-run', 'missing-receipt', 'wrong-operation'])
def test_binding_and_receipt_evidence_fail_closed(trace_store, fault):
    """I12: wrong Run and invalid proof cannot count as executed."""
    store, run, target, _, route = trace_store
    rid = route(target, bad_signature=fault == 'signature', receipt_run='different-run' if fault == 'wrong-run' else None)
    if fault == 'wrong-operation':
        with store.tx() as db:
            db.execute("UPDATE operations SET run_id='different-run'")
    traces, _ = collect_run_skill_trace(store, run, {'application_receipts': [] if fault == 'missing-receipt' else [rid]})
    trace = traces[target]
    assert trace['offered'] is True
    assert trace['applied'] is None and trace['source_status'] == 'INCOMPLETE'
    assert trace['bound'] is (None if fault == 'signature' else True)


def _dataset(*, statuses=(('FAIL', 'PASS'),), expected=True, evidence='model', activated=True):
    candidate = SimpleNamespace(base_release_digest='base', candidate_release_digest='treatment')
    tasks = [{'id': f'task-{i}', 'expected_skill_activation': expected, 'goal': 'Repair visible code'}
             for i in range(len(statuses))]
    plan = impact_plan('S2', tasks, candidate)
    trials = []
    for item, outcomes in zip(plan['tasks'], statuses):
        for arm, status, release in zip(('A', 'B'), outcomes, ('base', 'treatment')):
            name = item['task_id'] + arm
            manifest = {'release_digest': release, 'budget': {'model_calls': 6},
                        'model_config_digest': 'model-config', 'environment_digest': 'docker',
                        'contract_digest': 'contract', 'workspace_baseline_digest': 'baseline'}
            active = arm == 'B' and activated
            trials.append({'task_id': item['task_id'], 'id': name, 'run_id': 'run-'+name,
                           'state_root': 'private-root-'+name, 'arm': arm, 'provider_kind': evidence,
                           'release': release, 'cohort': 'frozen-cohort', 'manifest': manifest,
                           'passed': status == 'PASS', 'state': 'SUCCEEDED' if status == 'PASS' else 'PARTIAL',
                           'impact_context': {'task_digest': item['task_digest'], 'grader_version': GRADER_VERSION,
                                              'policy_digest': 'frozen-runtime'},
                           'impact_grade': {'status': status},
                           'impact_usage': {'settled': True, 'input_tokens': 128, 'output_tokens': 4},
                           'cost_vector': {'model_attempts': 1, 'tool_calls': 3, 'wall_seconds': 4.5},
                           'skill_trace_inventory_complete': True,
                           'skill_traces': {'target': {'retrieved': active, 'applied': active, 'offered': active,
                                                      'bound': None, 'binding_required': False, 'verified': False,
                                                      'source_status': 'COMPLETE', 'receipt_refs': ['private-ref']}}})
    return trials, plan


def _compare(trials, plan, **kwargs):
    return compare_skill_pairs(trials, 'target', candidate='candidate', plan=plan, **kwargs)


def test_reports_both_directions_and_budget_noncompletion():
    """I06: no net-rate metric hides regressions."""
    trials, plan = _dataset(statuses=(('FAIL', 'PASS'), ('PASS', 'FAIL'), ('PASS', 'NOT_COMPLETED')))
    score = _compare(trials, plan)
    assert score['transitions'] == {'fail_to_pass': 1, 'pass_to_fail': 1,
                                    'incomplete_to_pass': 0, 'pass_to_incomplete': 1}
    assert score['verdict'] == 'HARMFUL_OBSERVED'
    assert score['outcomes_b'] == {'PASS': 1, 'FAIL': 1, 'NOT_COMPLETED': 1}


def test_negative_frozen_task_counts_misfire():
    """I07."""
    trials, plan = _dataset(expected=False)
    score = _compare(trials, plan, phase='S3')
    assert score['negative']['offered'] == score['negative']['applied'] == 1
    assert score['verdict'] == 'HARMFUL_OBSERVED'


def test_s2_negative_probe_does_not_make_task_negative():
    """I08."""
    cand = SimpleNamespace(base_release_digest='base', candidate_release_digest='candidate')
    task = {'id': 's2', 'probes': [{'kind': 'NEGATIVE'}]}
    plan = impact_plan('S2', [task, {**task, 'id': 'explicit', 'expected_skill_activation': True}], cand)
    assert [t['expected_activation'] for t in plan['tasks']] == [None, True]
    tasks = [{'id': role, 'skill_role': role} for role in ('CORE', 'TRANSFER', 'NEGATIVE', 'BOUNDARY')]
    assert [t['expected_activation'] for t in impact_plan('S3', tasks, cand)['tasks']] == [True, True, False, None]


@pytest.mark.parametrize('field', ['budget', 'model_config_digest', 'environment_digest', 'policy_digest',
                                 'grader_version', 'contract_digest', 'workspace_baseline_digest'])
def test_counterfactual_mismatches_never_have_lift(field):
    """I09."""
    trials, plan = _dataset()
    trial = trials[1]
    for source in ('manifest', 'impact_context'):
        if field in trial[source]:
            trial[source][field] = 'different'
    score = _compare(trials, plan)
    assert score['comparability'] == 'INCOMPARABLE' and score['skill_lift_pp'] is None
    assert 'MISMATCH_' + field.upper() in score['pairs'][0]['reasons']


def test_missing_duplicate_unplanned_and_reused_trials_are_explicit():
    trials, plan = _dataset(statuses=(('PASS', 'PASS'), ('PASS', 'PASS')))
    missing = _compare(trials[:-1], plan)
    assert missing['comparability'] == 'PARTIAL' and missing['skill_lift_pp'] is None
    assert 'MISSING_ARM' in missing['pairs'][1]['reasons']
    duplicate = _compare(trials + [trials[-1]], plan)
    assert 'DUPLICATE_OR_INVALID_ARM' in duplicate['pairs'][1]['reasons']
    unplanned = copy.deepcopy(trials[-1])
    unplanned['task_id'] = 'not-frozen'
    assert 'UNPLANNED_PAIR' in _compare(trials + [unplanned], plan)['pairs'][-1]['reasons']
    trials[1]['run_id'] = trials[0]['run_id']
    assert 'TRIAL_NOT_ISOLATED_OR_REUSED' in _compare(trials, plan)['pairs'][0]['reasons']


def test_confirmed_partial_usage_does_not_set_unobserved_or_unsettled_tokens_to_zero():
    """I10."""
    events = {1: {'type': 'model.completed', 'data': {'usage': {'prompt_tokens': 8, 'completion_tokens': 1}}},
              2: {'type': 'model.failed', 'data': {'provider_details': {'error_type': 'APIConnectionError'}}}}
    observed = _observed_usage(events, {'cost': {'model_calls': 2}})
    assert observed == dict(settled=False, input_tokens=None, output_tokens=None, cache_tokens=None)
    events[2]['data']['provider_details']['dispatch_status'] = 'NOT_DISPATCHED'
    assert _observed_usage(events, {'cost': {'model_calls': 2}})['input_tokens'] == 8
    observed_records = [{'purpose': 'main', 'status': 'COMPLETED', 'settled_tokens': 9,
                         'usage': {'prompt_tokens': 8, 'completion_tokens': 1}}]
    assert _observed_usage(events, {'cost': {'model_calls': 2}}, observed_records)['input_tokens'] == 8
    events[2]['data']['provider_details'].clear()
    assert _observed_usage(events, {'cost': {'model_calls': 2}}, observed_records)['settled'] is False
    trials, plan = _dataset()
    trials[1]['impact_usage'] = observed
    assert _compare(trials, plan)['cost'] is None
    trials[1]['impact_usage'] = {'settled': True, 'input_tokens': 8, 'output_tokens': 0}
    score = _compare(trials, plan)
    assert score['cost']['output_tokens']['b'] == 0 and score['cost']['cache_tokens']['delta'] is None
    assert score['cost']['actual_cost']['delta'] is None


def test_independent_pass_with_skill_unused_or_on_scripted_provider():
    """I01, I11 and I13 (native gate regression is also covered by its own suite)."""
    trials, plan = _dataset(statuses=(('PASS', 'PASS'),), activated=False)
    score = _compare(trials, plan)
    assert score['pass_b'] == 1 and score['verdict'] == 'INCONCLUSIVE'
    trials, plan = _dataset(evidence='scripted_test_double')
    score = _compare(trials, plan)
    assert score['descriptive_lift_pp'] == 100 and score['skill_lift_pp'] is None
    assert score['verdict'] == 'INCONCLUSIVE' and score['evidence_class'] == 'INTEGRATION_ONLY'
    for trial in trials:
        trial['provider_kind'] = 'OpenAIProvider'
    assert _compare(trials, plan)['verdict'] == 'BENEFICIAL_OBSERVED'
    assert _compare(trials, plan, evidence_class='LOCAL_DIAGNOSTIC')['skill_lift_pp'] is None


def test_report_cli_is_repeatable_readonly_and_legacy_unknown(tmp_path):
    """I14 and I15. Run the actual CLI against a closed original Host."""
    store = Store(tmp_path / 'state')
    trials, plan = _dataset()
    stage = {'stage': 'S2', 'passed': False, 'candidate': 'candidate', 'contract_ref': 'target',
             'evidence_class': 'MODEL_TASK_EVALUATION', 'trials': trials}
    report = _compare(trials, plan)
    ref = persist_skill_impact(store, report, trials, plan)
    assert ref == persist_skill_impact(store, report, trials, plan)
    store.put('skill_s2', 's2', stage)
    store.put('skill_impact_stage', digest(stage), {'impact_ref': ref})
    old = {k: v for k, v in stage.items() if k != 'trials'}
    old.update(stage='S3', trials=[{k: v for k, v in t.items() if not k.startswith(('skill_', 'impact_'))}
                                    | {'capability_applied': True} for t in trials])
    store.put('skill_s3', 's3', old)
    # Move the fixture to rollback journal: the CLI may not create WAL sidecars.
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
    base = {p.name: digest(p.read_bytes()) for p in store.root.iterdir() if p.is_file()}
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(root/'src')}
    command = [sys.executable, str(root/'scripts/skill_impact_lite.py'), 'report',
               '--state', str(store.root), '--candidate-id', 'candidate']
    for phase, folder in [('s2', 'first'), ('s2', 'second'), ('s3', 'old')]:
        run = subprocess.run(command + ['--phase', phase, '--out', str(tmp_path/folder)],
                             capture_output=True, env=env, timeout=25)
        assert run.returncode == 0, run.stderr.decode(errors='replace')
    for p in (tmp_path/'first').iterdir():
        assert p.read_bytes() == (tmp_path/'second'/p.name).read_bytes()
        assert not any(secret in p.read_bytes() for secret in (b'private-root', b'private-ref'))
    old_summary = json.loads((tmp_path/'old'/'summary.json').read_bytes())
    assert old_summary['verdict'] == 'INCONCLUSIVE' and old_summary['skill_lift_pp'] is None
    assert 'TARGET_TRACE_INCOMPLETE' in old_summary['limitations']
    assert b',true,true,' not in (tmp_path/'old'/'results.csv').read_bytes()
    assert base == {p.name: digest(p.read_bytes()) for p in store.root.iterdir() if p.is_file()}
