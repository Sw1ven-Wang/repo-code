"""Read-only Skill evidence and paired impact, separate from qualification."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
import json
from types import SimpleNamespace

from .acceptance import GRADER_VERSION, delivery_verified, evidence_matches
from .models import Contract, Evidence, VerificationIntent, Subject
from .util import RepoError, digest, now
from .workspace import Workspace

_ACTIVATION = ('retrieved', 'offered', 'bound', 'applied', 'verified')
_USAGE = ('input_tokens', 'output_tokens', 'cache_tokens')
_COST = ('model_attempts', 'tool_calls', *_USAGE, 'wall_seconds', 'actual_cost')
_COMPARABLE = ('budget', 'model_config_digest', 'environment_digest',
               'contract_digest', 'workspace_baseline_digest', 'task_digest',
               'grader_version', 'policy_digest')
_BUDGET_ENDS = {
    'BUDGET_EXHAUSTED', 'TOTAL_TOKEN_BUDGET_EXHAUSTED', 'CONTEXT_BUDGET_EXCEEDED',
    'TASK_TOTAL_BUDGET', 'TURN_BUDGET_EXHAUSTED', 'NO_PROGRESS', 'STAGNATION_AFTER_WRITE',
}
_MODEL_EVENTS = {'model.completed', 'model.failed', 'model.response_rejected',
                 'model.stale_response_discarded'}


def _signed(store, row):
    return bool(row and isinstance(row.get('signature'), str) and
                store.verify({k: v for k, v in row.items() if k != 'signature'}, row['signature']))


def _rows(store, run_id):
    # Unlike the interactive trace endpoint, do not stop at the first 10,000 events.
    with closing(store.connect()) as db:
        row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        run = {**dict(row), 'data': json.loads(row['data'])}
        events = {e['sequence']: {**dict(e), 'data': json.loads(e['data'])}
                  for e in db.execute('SELECT * FROM events WHERE run_id=? ORDER BY sequence', (run_id,))}
        records = defaultdict(dict)
        for item in db.execute(
            """SELECT kind,id,data FROM objects WHERE kind IN
               ('capability_route','capability_offer','application_receipt')
               AND json_extract(data,'$.run_id')=?""", (run_id,),
        ):
            records[item['kind']][item['id']] = json.loads(item['data'])
    return run, events, records


def _identity(row, manifest):
    return all(row.get(k) == manifest[k] for k in ('release_digest', 'contract_digest'))


def _binding(store, run, events, ref, route, offer, contract):
    if not contract.get('binding_slots'):
        return None, None, False
    key = digest([run['id'], route, ref])
    row = store.get('capability_binding', key)
    if row is None:
        return False, None, False
    payload = row.get('payload') or {}
    sequence = payload.get('sequence')
    event = events.get(sequence, {})
    expected = {'run_id': run['id'], 'route_id': route, 'contract_ref': ref,
                'environment': run['data']['manifest']['environment_digest'],
                'control_epoch': run['control_epoch']}
    valid = (isinstance(row.get('signature'), str) and store.verify(payload, row['signature'])
             and all(payload.get(k) == v for k, v in expected.items())
             and type(sequence) is int and sequence > offer['offer_sequence']
             and event.get('type') == 'capability.bound'
             and event['data'].get('contract_ref') == ref and event['data'].get('binding_ref') == key)
    if not valid:
        return None, None, True
    return True, payload, False


def _diff_proven(store, run, rule, args, result, recorded):
    from .capabilities import CapabilityRouter

    # Reuse the Host's existing proof rules with a Workspace that cannot persist
    # blobs. Snapshots and reads still use the same bytes and identifiers.
    view_store = SimpleNamespace(blob=digest, json_blob=digest, read_json=store.read_json, read_blob=store.read_blob)
    ws = Workspace(run['data']['workspace'], view_store)
    runtime = SimpleNamespace(workspace=lambda _: ws, sandbox=SimpleNamespace(
        environment_digest=run['data']['manifest']['environment_digest']))
    observer = SimpleNamespace(owner=SimpleNamespace(runtime=runtime))
    return bool(recorded and recorded == CapabilityRouter._diff_coverage_proof(observer, run, rule, args, result))


def _observed_steps(store, run, events, receipt, contract, binding, offer_sequence):
    run_id = run['id']
    proof = receipt.get('step_evidence') or []
    rules = (binding or contract).get('step_rules') or []
    tools = contract.get('step_tools') or []
    if not proof or len(proof) != len(tools) or len(proof) != len(rules) or len(proof) != len(contract['steps']):
        return False
    seen = set()
    previous = binding['sequence'] if binding else offer_sequence
    with closing(store.connect()) as db:
        for step, tool, rule in zip(proof, tools, rules):
            sequence, op_id = step.get('sequence'), step.get('operation_id')
            event = events.get(sequence, {})
            if (type(sequence) is not int or sequence <= previous or not op_id or op_id in seen
                    or event.get('type') != 'tool.operation_finished'
                    or event.get('event_id') != step.get('event_id')
                    or event['data'].get('operation_id') != op_id
                    or event['data'].get('tool_name') != tool or step.get('tool') != tool):
                return False
            row = db.execute("SELECT request,result FROM operations WHERE id=? AND run_id=? AND state='SUCCEEDED'",
                             (op_id, run_id)).fetchone()
            if not row:
                return False
            request, result = json.loads(row['request']), json.loads(row['result'])
            args = request.get('arguments') or {}
            if request.get('tool_name') != tool or event['data'].get('action_digest') != digest([tool, args]):
                return False
            if any(args.get(k) != v for k, v in rule.get('arguments_match', {}).items()):
                try:
                    valid_diff = tool == 'workspace.diff' and _diff_proven(
                        store, run, rule, args, result, step.get('equivalence_proof'))
                except (OSError, RepoError, KeyError, TypeError, ValueError):
                    valid_diff = False
                if not valid_diff:
                    return False
            if result.get('truncated'):
                artifact = result.get('result_artifact')
                owner = store.get('run_artifact', digest([run_id, artifact])) if artifact else None
                if not owner:
                    return False
                result = store.read_json(artifact)
            if (any(result.get(k) for k in ('truncated', 'is_error', 'timeout', 'cancelled', 'running'))
                    or tool == 'process.run' and result.get('exit_code') != 0):
                return False
            seen.add(op_id)
            previous = sequence
    return True


def _checked_decision(store, run, events, ref):
    """Read a signed decision tied to this run's immutable verification intent."""
    raw = store.get('intent', ref)
    decision = store.get('decision', ref)
    if not raw or not _signed(store, decision) or decision.get('intent_digest') != ref:
        return None
    intent = VerificationIntent.model_validate(raw)
    manifest = run['data']['manifest']
    if (intent.digest != ref or intent.run_id != run['id'] or not _identity(raw, manifest)
            or intent.control_epoch != run['control_epoch']
            or intent.writer_epoch != run['writer_epoch']
            or any(raw.get(k) != manifest.get(k) for k in (
                'environment_digest', 'task_spec_revision', 'knowledge_snapshot_digest', 'knowledge_index_digest'))
            or not any(e['type'] == 'verification.prepared' and e['data'].get('intent') == ref
                       for e in events.values())):
        return None
    contract = Contract.model_validate(store.read_json(intent.contract_digest))
    if contract.status != 'FROZEN' or intent.required_check_set_digest != digest(sorted(contract.required_checks)):
        return None
    checks = {c.id: c for c in contract.checks}
    evidence = {}
    for eid in decision.get('evidence_ids', []):
        raw_evidence = store.get('evidence', eid)
        if not raw_evidence:
            return None
        e = Evidence.model_validate(raw_evidence)
        check = checks.get(e.check_id)
        if (e.id != eid or not check or e.check_id in evidence or not _signed(store, raw_evidence)
                or any(raw_evidence.get(k) != raw.get(k) for k in (
                    'run_id', 'contract_digest', 'release_digest', 'environment_digest', 'task_spec_revision',
                    'knowledge_snapshot_digest', 'knowledge_index_digest'))
                or e.grader_digest != GRADER_VERSION or e.definition_digest != check.digest
                or e.config_digest != digest(check.definition) or e.authority not in check.permitted_authorities
                or e.authority in {'IN_PROCESS', 'SELF'} or e.subject != check.subject
                or not e.executed_ids or e.executed_ids != e.expected_ids or e.skipped_ids
                or e.subject == Subject.SOURCE and e.source_before != intent.source_digest
                or e.status not in {'PASSED', 'FAILED'}):
            return None
        if e.status == 'PASSED' and not evidence_matches(e, intent, contract, decision.get('artifact_digest'), store):
            return None
        if e.status == 'FAILED':
            # An execution timeout, cancellation, startup error or truncated result
            # is not a known wrong answer. Do not export the observations.
            observations = store.read_json(e.report_digest)
            if any(x.get('error_type') or any((x.get('observation') or {}).get(k)
                   for k in ('timeout', 'cancelled', 'truncated', 'is_error', 'output_truncated'))
                   for x in observations if isinstance(x, dict)):
                return None
        evidence[e.check_id] = e
    if set(evidence) != set(contract.required_checks):
        return None
    return intent, decision, contract, evidence


def _grade_run(store, run, events, report):
    delivery = report.get('delivery') or {}
    reason = report.get('reason')
    if report['state'] in {'PARTIAL', 'BLOCKED'} and reason in _BUDGET_ENDS:
        return {'status': 'NOT_COMPLETED', 'reason': reason}
    prepared = [e['data'].get('intent') for e in events.values() if e['type'] == 'verification.prepared']
    ref = delivery.get('intent_digest') or (prepared[-1] if prepared else None)
    score = _checked_decision(store, run, events, ref) if ref else None
    if score:
        intent, decision, contract, evidence = score
        if (report['state'] == 'SUCCEEDED' and delivery.get('outcome') == 'VERIFIED'
                and delivery.get('run_id') == run['id'] and delivery.get('source_digest') == intent.source_digest
                and delivery.get('evidence_ids') == decision['evidence_ids'] and decision['verified']
                and events.get(delivery.get('sequence'), {}).get('type') == 'delivery.committed'
                and events[delivery['sequence']]['data'].get('intent_digest') == ref
                and delivery_verified(intent, contract, evidence, decision.get('artifact_digest'), store)[0]):
            return {'status': 'PASS', 'decision_ref': ref}
        if (report['state'] in {'PARTIAL', 'FAILED'} and reason in {'INSUFFICIENT_EVIDENCE', 'REQUIRED_CHECK_FAILED_OR_INVALID'}
                and not decision['verified'] and any(e.status == 'FAILED' for e in evidence.values())
                and digest(Workspace(report['workspace'], SimpleNamespace(blob=digest)).manifest()) == intent.source_digest):
            return {'status': 'FAIL', 'decision_ref': ref}
    return {'status': 'UNKNOWN'}


def _trace(store, run, events, records, report, ref, complete):
    manifest = run['data']['manifest']
    routes = records['capability_route']
    offers = records['capability_offer']
    contract = store.get('capability_contract', ref)
    retrieved = any(ref in route.get('considered_contract_refs', []) and _identity(route, manifest)
                    for route in routes.values())
    relevant = {key for key, offer in offers.items() if ref in offer.get('contract_refs', [])}
    possible = {key for key, route in routes.items() if ref in route.get('contract_refs', [])}
    broken = bool(possible - relevant)
    states, receipts, used_events = [], [], []
    binding_required = bool(contract.get('binding_slots')) if contract else None
    reported = set(report.get('application_receipts') or [])
    for key in sorted(relevant):
        offer, route = offers[key], routes.get(key)
        event = events.get(offer.get('offer_sequence'), {})
        if (not contract or not route or route.get('id') != key or offer.get('route_id') != key
                or not _identity(route, manifest) or not _identity(offer, manifest)
                or ref not in route.get('contract_refs', [])
                or offer.get('control_epoch') != route.get('control_epoch')
                or offer.get('control_epoch') != run['control_epoch']
                or event.get('type') != 'capability.offered' or event['data'].get('route_id') != key
                or offer.get('offered') is not True):
            broken = True
            continue
        bound, binding, invalid = _binding(store, run, events, ref, key, offer, contract)
        used_events.append(event['event_id'])
        if binding:
            used_events.append(events[binding['sequence']]['event_id'])
        applied = verified = None
        for rid, receipt in records['application_receipt'].items():
            if rid not in reported or receipt.get('route_id') != key or receipt.get('capability_contract_ref') != ref:
                continue
            if (rid != digest(receipt) or not _identity(receipt, manifest) or receipt.get('historical') is not False
                    or receipt.get('offered') is not True
                    or receipt.get('binding_ref') != (digest([run['id'], key, ref]) if binding else None)):
                invalid = True
                continue
            receipts.append(rid)
            if receipt.get('applied') is False and not invalid:
                applied = False if applied is None else applied
                verified = False if verified is None else verified
            elif (receipt.get('applied') is True and (not binding_required or bound is True)
                    and _observed_steps(store, run, events, receipt, contract, binding, offer['offer_sequence'])):
                applied = True
                used_events.extend(step['event_id'] for step in receipt['step_evidence'])
                if receipt.get('verified') is False:
                    verified = False
                if receipt.get('verified') is True:
                    required = set(contract.get('required_probe_ids') or [])
                    for decision_ref in receipt.get('verification_refs', []):
                        checked = _checked_decision(store, run, events, decision_ref)
                        if (checked and checked[1]['verified'] and required
                                and required <= {k for k, e in checked[3].items() if e.status == 'PASSED'}
                                and delivery_verified(checked[0], checked[2], checked[3],
                                                      checked[1].get('artifact_digest'), store)[0]):
                            verified = True
                            break
            else:
                invalid = True
        broken |= invalid or applied is None or verified is None
        states.append((bound, applied, verified))

    def observed(index):
        values = [row[index] for row in states]
        if True in values:
            return True
        return False if complete and not broken and all(v is False for v in values) else None

    return {
        'retrieved': True if retrieved else False if complete and not broken else None,
        'offered': True if states else False if complete and not broken else None,
        'binding_required': binding_required, 'bound': observed(0) if binding_required else None,
        'applied': observed(1), 'verified': observed(2),
        'receipt_refs': sorted(receipts), 'event_refs': sorted(set(used_events)),
        'source_status': 'COMPLETE' if complete and not broken else 'INCOMPLETE',
    }


def collect_run_skill_trace(store, run_id, report, *, run_evidence=None):
    """Read original Host evidence before the isolated trial Store is closed."""
    run, events, records = run_evidence if run_evidence is not None else _rows(store, run_id)
    refs = {ref for row in records['capability_route'].values()
            for ref in row.get('considered_contract_refs', []) + row.get('contract_refs', [])}
    refs.update(ref for row in records['capability_offer'].values() for ref in row.get('contract_refs', []))
    complete = bool(records['capability_route']) and run['data']['visibility'] == 'DEV'
    event_offers = {e['data'].get('route_id') for e in events.values() if e['type'] == 'capability.offered'}
    complete &= event_offers == set(records['capability_offer'])
    complete &= all(key == row.get('id') and _identity(row, run['data']['manifest'])
                    for key, row in records['capability_route'].items())
    complete &= all(set(row.get('contract_refs', [])) <= set(row.get('considered_contract_refs', []))
                    for row in records['capability_route'].values())
    # Absence can only prove "not offered" for an unobserved target when the
    # route/offer inventory was intact and at least one routing decision exists.
    return {ref: _trace(store, run, events, records, report, ref, complete) for ref in sorted(refs)}, complete


def _integer(value):
    return value if type(value) is int and value >= 0 else None


def _number(value):
    return value if type(value) in {float, int} and 0 <= value < float('inf') else None


def _observed_usage(events, trial, records=None):
    requests = [e['data'] for e in events.values() if e['type'] in _MODEL_EVENTS]
    def not_dispatched(row):
        details = row.get('provider_details') or {}
        stop = row.get('budget_stop') or {}
        return any(proof.get('dispatch_status') == 'NOT_DISPATCHED' for proof in (details, stop))

    provider_events = [r for r in requests if not not_dispatched(r)]
    attempts = trial.get('cost', {}).get('model_calls')
    # Compare Host charges to provider-facing events. Admission can reject with
    # proof either before a Host charge (exhausted call limit) or after one.
    counts_match = type(attempts) is int and len(provider_events) <= attempts <= len(requests)
    unsettled = not counts_match
    if records is not None:
        dispatched = [r for r in records if r.get('status') != 'NOT_DISPATCHED']
        main = [r for r in dispatched if r.get('purpose', 'main') == 'main']
        # Explicitly proven admission rejects need not appear in the provider
        # ledger. All dispatched main calls must appear on both sides.
        unsettled |= len(main) != len(provider_events)
        unsettled |= any(type(r.get('settled_tokens')) is not int for r in dispatched)
        usages = [r.get('usage') or {} for r in dispatched]
    else:
        usages = [r.get('usage') or {} for r in provider_events]
    counters = {k: [] for k in _USAGE}
    for usage in usages:
        counters['input_tokens'].append(_integer(usage.get('input_tokens', usage.get('prompt_tokens'))))
        counters['output_tokens'].append(_integer(usage.get('output_tokens', usage.get('completion_tokens'))))
        details = usage.get('input_tokens_details') or usage.get('prompt_tokens_details') or {}
        counters['cache_tokens'].append(_integer(details.get('cached_tokens', usage.get(
            'prompt_cache_hit_tokens', usage.get('cached_tokens', usage.get('cache_read_input_tokens'))))))
    complete = unsettled == 0 and all(None not in counters[k] for k in _USAGE[:2])
    return {'settled': complete, **{k: sum(v) if complete and None not in v else None
                                   for k, v in counters.items()}}


def collect_trial_impact(store, run_id, report, trial, *, task, policy, records=None):
    run, events, inventory = _rows(store, run_id)
    traces, complete = collect_run_skill_trace(store, run_id, report, run_evidence=(run, events, inventory))
    return {'skill_traces': traces, 'skill_trace_inventory_complete': complete,
            'impact_grade': _grade_run(store, run, events, report),
            'impact_usage': _observed_usage(events, trial, records),
            'impact_context': {'task_digest': digest(task), 'grader_version': GRADER_VERSION,
                               'policy_digest': digest(policy)}}



def normalize_trial(trial, contract_ref, *, expected_activation=None):
    manifest = trial.get('manifest') or {}
    context = trial.get('impact_context') or {}
    trace = (trial.get('skill_traces') or {}).get(contract_ref)
    if trace is None and trial.get('skill_trace_inventory_complete') is True:
        trace = dict.fromkeys(_ACTIVATION, False)
        trace.update(bound=None, source_status='COMPLETE')
    trace = trace or {}
    status = (trial.get('impact_grade') or {}).get('status', 'UNKNOWN')
    if status not in {'PASS', 'FAIL', 'NOT_COMPLETED'}:
        status = 'UNKNOWN'
    if trial.get('state') == 'INFRASTRUCTURE_ERROR':
        status = 'INFRASTRUCTURE_ERROR'
    usage = trial.get('impact_usage') or {}
    settled = usage.get('settled') is True and all(_integer(usage.get(k)) is not None for k in _USAGE[:2])
    costs = trial.get('cost_vector') or {}
    cost = {k: _integer(usage.get(k)) if settled else None for k in _USAGE}
    cost.update({k: _number(costs.get(k)) for k in _COST if k not in _USAGE})
    if not settled:
        cost['actual_cost'] = None
    return {
        'task_id': trial.get('task_id'), 'attempt_index': trial.get('attempt_index', 0),
        'arm': trial.get('arm'), 'trial_id': trial.get('id'), 'run_id': trial.get('run_id'),
        'state_root': trial.get('state_root'), 'release': trial.get('release'),
        'manifest_release': manifest.get('release_digest'), 'cohort': trial.get('cohort'),
        'provider_kind': trial.get('provider_kind'), 'quality': status,
        'delivery_recorded': trial.get('passed') is True,
        'expected_activation': expected_activation if type(expected_activation) is bool else None,
        'trace': {**{k: trace.get(k) if type(trace.get(k)) is bool else None for k in _ACTIVATION},
                  'binding_required': trace.get('binding_required'),
                  'source_status': trace.get('source_status', 'INCOMPLETE')},
        'cost': cost, 'usage_settled': settled,
        'comparison': {k: (context if k in {'task_digest', 'grader_version', 'policy_digest'} else manifest).get(k)
                       for k in _COMPARABLE},
        'collection_error': bool(trial.get('impact_collection_error')),
    }


def impact_plan(stage, tasks, candidate, *, efficiency_metric=None):
    """Freeze only digests and expectations; keep test definitions in Host."""
    labeled = {'CORE': True, 'TRANSFER': True, 'NEGATIVE': False}
    frozen = []
    for task in tasks:
        explicit = task.get('expected_skill_activation')
        expected = labeled.get(task.get('skill_role'), explicit) if stage == 'S3' else explicit
        frozen.append({'task_id': task['id'], 'attempt_index': 0, 'task_digest': digest(task),
                       'expected_activation': expected if type(expected) is bool else None})
    return {'tasks': frozen, 'dataset_digest': digest(frozen), 'efficiency_metric': efficiency_metric,
            'baseline_release': candidate.base_release_digest, 'treatment_release': candidate.candidate_release_digest}


def _sum(values):
    return sum(values) if all(v is not None for v in values) else None


def _aggregate(rows_a, rows_b):
    result = {}
    for k in _COST:
        va, vb = _sum([r['cost'][k] for r in rows_a]), _sum([r['cost'][k] for r in rows_b])
        result[k] = {'a': va, 'b': vb, 'delta': vb-va if va is not None and vb is not None else None,
                     'delta_pct': (vb-va)/va*100 if va and vb is not None else None}
    return result


def _activation(rows, expected):
    group = [r['trace'] for r in rows if r['expected_activation'] is expected]
    counts = {'total': len(group)}
    for key in _ACTIVATION:
        counts[key] = sum(t[key] is True for t in group)
        unknown = sum(t[key] is None and (key != 'bound' or t['binding_required'] is not False) for t in group)
        counts[key + '_unknown'] = unknown
    counts['bound_not_applicable'] = sum(t['binding_required'] is False for t in group)
    for key in ('offered', 'applied'):
        counts[key + '_rate'] = counts[key] / len(group) if group and not counts[key + '_unknown'] else None
    return counts


def _public_row(row):
    if not row:
        return None
    return {k: row[k] for k in ('quality', 'delivery_recorded', 'trace', 'cost', 'usage_settled')}


def compare_skill_pairs(trials, contract_ref, *, candidate=None, phase='S2', plan=None,
                        evidence_class='MODEL_TASK_EVALUATION'):
    """Compare the exact frozen A/B membership, publishing only report fields."""
    if evidence_class == 'INTEGRATION_ONLY' or any(t.get('provider_kind') == 'scripted_test_double' for t in trials):
        evidence_class = 'INTEGRATION_ONLY'
    elif evidence_class not in {'MODEL_TASK_EVALUATION', 'LOCAL_DIAGNOSTIC'}:
        evidence_class = 'LOCAL_DIAGNOSTIC'
    all_reasons = set()
    plan = plan or {}
    expected, duplicates = {}, set()
    for item in plan.get('tasks', []):
        key = item['task_id'], item['attempt_index']
        if key in expected:
            duplicates.add(key)
        expected[key] = item
    frozen = bool(expected) and plan.get('dataset_digest') == digest(plan['tasks'])
    a_release, b_release = plan.get('baseline_release'), plan.get('treatment_release')
    if not frozen:
        all_reasons.add('NO_FROZEN_DATASET')
    if not a_release or not b_release or a_release == b_release:
        all_reasons.add('INVALID_RELEASE_PLAN')
    grouped = defaultdict(list)
    rows = []
    for trial in trials:
        task, attempt = trial.get('task_id'), trial.get('attempt_index', 0)
        if not isinstance(task, str) or not task or _integer(attempt) is None:
            all_reasons.add('INVALID_TRIAL_POSITION')
            continue
        position = task, attempt
        row = normalize_trial(trial, contract_ref, expected_activation=expected.get(position, {}).get('expected_activation'))
        rows.append(row)
        grouped[position].append(row)
    identifiers = {k: Counter(r[k] for r in rows if r[k]) for k in ('run_id', 'state_root', 'trial_id')}
    pairs, paired_a, paired_b, both_a, both_b, observed_b = [], [], [], [], [], []
    transitions = dict(fail_to_pass=0, pass_to_fail=0, incomplete_to_pass=0, pass_to_incomplete=0)
    severe = {'NO_FROZEN_DATASET', 'INVALID_RELEASE_PLAN', 'INVALID_TRIAL_POSITION'}
    has_mismatch = bool(severe & all_reasons)
    for task, attempt in [*expected, *sorted(set(grouped)-set(expected))]:
        key = task, attempt
        group = grouped.get(key, [])
        arms = {arm: [row for row in group if row['arm'] == arm] for arm in ('A', 'B')}
        a, b = (arms[arm][0] if len(arms[arm]) == 1 else None for arm in ('A', 'B'))
        if b:
            observed_b.append(b)
        reasons, mismatches = set(), set()
        if (key in duplicates or any(len(v) > 1 for v in arms.values())
                or any(r['arm'] not in arms for r in group)):
            mismatches.add('DUPLICATE_OR_INVALID_ARM')
        if key not in expected:
            mismatches.add('UNPLANNED_PAIR')
        if not a or not b:
            reasons.add('MISSING_ARM')
        for row, release in ((a, a_release), (b, b_release)):
            if row is None:
                continue
            if any(not row[k] or counts[row[k]] != 1 for k, counts in identifiers.items()):
                mismatches.add('TRIAL_NOT_ISOLATED_OR_REUSED')
            if row['release'] != release or row['manifest_release'] != release or not release:
                mismatches.add('RELEASE_MISMATCH')
            if key in expected and row['comparison']['task_digest'] != expected[key].get('task_digest'):
                mismatches.add('FROZEN_TASK_MISMATCH')
            if row['quality'] in {'UNKNOWN', 'INFRASTRUCTURE_ERROR'}:
                reasons.add('UNSCORED_OR_INFRASTRUCTURE_ERROR')
            if not row['usage_settled']:
                reasons.add('UNSETTLED_USAGE')
            if row['collection_error']:
                reasons.add('COLLECTION_ERROR')
        if a and b:
            for field in _COMPARABLE:
                left, right = a['comparison'][field], b['comparison'][field]
                if left is None or right is None:
                    reasons.add('MISSING_' + field.upper())
                elif left != right:
                    mismatches.add('MISMATCH_' + field.upper())
            if not a['cohort'] or a['cohort'] != b['cohort']:
                mismatches.add('COHORT_MISMATCH')
            if not a['provider_kind'] or a['provider_kind'] != b['provider_kind']:
                mismatches.add('PROVIDER_MISMATCH')
        has_mismatch |= bool(mismatches)
        reasons.update(mismatches)
        all_reasons.update(reasons)
        comparable = not reasons and frozen and a_release != b_release
        if comparable:
            paired_a.append(a)
            paired_b.append(b)
            qa, qb = a['quality'], b['quality']
            for left, right, label in (
                ('FAIL', 'PASS', 'fail_to_pass'), ('PASS', 'FAIL', 'pass_to_fail'),
                ('NOT_COMPLETED', 'PASS', 'incomplete_to_pass'), ('PASS', 'NOT_COMPLETED', 'pass_to_incomplete'),
            ):
                transitions[label] += (qa, qb) == (left, right)
            if qa == qb == 'PASS':
                both_a.append(a)
                both_b.append(b)
        pairs.append({'task_id': task, 'attempt_index': attempt, 'comparable': comparable,
                      'expected_activation': expected.get(key, {}).get('expected_activation'),
                      'reasons': sorted(reasons), 'a': _public_row(a), 'b': _public_row(b)})

    n = len(paired_a)
    complete = bool(n and n == len(expected) and frozen and not all_reasons)
    comparability = 'COMPLETE' if complete else 'INCOMPARABLE' if has_mismatch or not n else 'PARTIAL'
    activation, negative = _activation(observed_b, True), _activation(observed_b, False)
    # Missing B arms represent missing evidence rather than definite non-activation.
    for metrics, expected_value in ((activation, True), (negative, False)):
        missing = sum(item['expected_activation'] is expected_value for item in expected.values()) - metrics['total']
        if missing > 0:
            metrics['total'] += missing
            for name in _ACTIVATION:
                metrics[name + '_unknown'] += missing
            metrics['offered_rate'] = metrics['applied_rate'] = None
    qa = {k: sum(r['quality'] == k for r in paired_a) for k in ('PASS', 'FAIL', 'NOT_COMPLETED')}
    qb = {k: sum(r['quality'] == k for r in paired_b) for k in qa}
    cost = _aggregate(paired_a, paired_b) if n else None
    metric = plan.get('efficiency_metric')
    measured_lift = (qb['PASS']-qa['PASS'])/n*100 if complete else None
    positive_pairs = [(a, b) for a, b in zip(paired_a, paired_b)
                      if b['expected_activation'] is True and b['trace']['applied'] is True
                      and b['trace']['source_status'] == 'COMPLETE']
    positive_conversion = any(a['quality'] != 'PASS' and b['quality'] == 'PASS' for a, b in positive_pairs)
    efficiency = bool(metric in {'model_attempts', 'tool_calls', 'input_tokens', 'output_tokens'}
                      and cost and cost[metric]['delta'] is not None and cost[metric]['delta'] < 0
                      and qa['PASS'] == qb['PASS'] and any(
                          a['quality'] == b['quality'] == 'PASS' and a['cost'][metric] is not None
                          and b['cost'][metric] is not None and b['cost'][metric] < a['cost'][metric]
                          for a, b in positive_pairs))
    harm = bool(transitions['pass_to_fail'] or transitions['pass_to_incomplete']
                or any(b['expected_activation'] is False and (b['trace']['offered'] is True or b['trace']['applied'] is True)
                       for b in paired_b))
    trace_complete = all(r['trace']['source_status'] == 'COMPLETE' for r in observed_b)
    verdict = 'INCONCLUSIVE'
    if complete and evidence_class == 'MODEL_TASK_EVALUATION':
        if harm:
            verdict = 'HARMFUL_OBSERVED'
        elif positive_pairs and trace_complete:
            verdict = 'BENEFICIAL_OBSERVED' if (measured_lift > 0 and positive_conversion) or efficiency else 'NO_OBSERVED_GAIN'
    limitations = set(all_reasons)
    if not trace_complete or not observed_b:
        limitations.add('TARGET_TRACE_INCOMPLETE')
    if not positive_pairs:
        limitations.add('NO_VERIFIED_POSITIVE_APPLICATION_OBSERVED')
    if evidence_class != 'MODEL_TASK_EVALUATION':
        limitations.add('NOT_FORMAL_MODEL_BENEFIT_EVIDENCE')
    if cost and (cost['actual_cost']['a'] is None or cost['actual_cost']['b'] is None):
        limitations.add('ACTUAL_COST_UNAVAILABLE')
    return {
        'schema_version': 1, 'contract_ref': contract_ref, 'candidate': candidate, 'phase': phase,
        'evidence_class': evidence_class, 'comparability': comparability, 'verdict': verdict,
        'dataset_digest': plan.get('dataset_digest'), 'n_planned_pairs': len(expected), 'n_pairs': n,
        'pass_a': qa['PASS'], 'pass_b': qb['PASS'], 'outcomes_a': qa, 'outcomes_b': qb,
        'recorded_deliveries': {arm.lower(): sum(r['arm'] == arm and r['delivery_recorded'] for r in rows)
                                for arm in ('A', 'B')},
        'pass_rate_a': qa['PASS']/n if complete else None, 'pass_rate_b': qb['PASS']/n if complete else None,
        'skill_lift_pp': measured_lift if evidence_class == 'MODEL_TASK_EVALUATION' else None,
        'descriptive_lift_pp': measured_lift, 'transitions': transitions,
        'activation': activation, 'negative': negative,
        'predeclared_efficiency_metric': metric, 'cost': cost,
        'both_pass_cost': _aggregate(both_a, both_b) if both_a else None, 'n_both_pass': len(both_a),
        'limitations': sorted(limitations), 'pairs': pairs,
    }


def persist_skill_impact(store, impact, trials, plan):
    identity = {'schema_version': impact['schema_version'], 'contract_ref': impact['contract_ref'],
                'candidate': impact['candidate'], 'phase': impact['phase'],
                'ordered_trial_ids': [t.get('id') for t in trials],
                'baseline_release': (plan or {}).get('baseline_release'),
                'treatment_release': (plan or {}).get('treatment_release')}
    ref = digest(identity)
    content = {'identity': identity, 'plan': plan, 'report': impact}
    content_digest = digest(content)
    with store.tx() as db:
        prior = store.get('skill_impact_report', ref, db)
        if prior:
            if prior.get('content_digest') != content_digest:
                raise ValueError('SKILL_IMPACT_CONTENT_CONFLICT')
        else:
            store.put('skill_impact_report', ref, {**content, 'generated_at': now(), 'content_digest': content_digest},
                      db, immutable=True)
    return ref


def render_markdown(report):
    a, b = report['outcomes_a'], report['outcomes_b']
    shown = lambda v: '未知/不适用' if v is None else str(v)
    lift = report['skill_lift_pp']
    lines = [
        '# Skill Impact v0.1', '',
        f"- 阶段：{report['phase']}；证据：{report['evidence_class']}",
        f"- 比较范围：{report['n_pairs']}/{report['n_planned_pairs']} 对；{report['comparability']}",
        f"- 结论：{report['verdict']}；正式通过率差：{shown(round(lift, 2) if lift is not None else None)} 个百分点",
        '', '| 判定（可比对） | A | B |', '|---|---:|---:|',
        *[f'| {k} | {a[k]} | {b[k]} |' for k in ('PASS', 'FAIL', 'NOT_COMPLETED')],
        '', '配对转换：' + '，'.join(f'{k}={v}' for k, v in report['transitions'].items()),
        '', '| B 组目标 Skill | 明确适用 | 明确不适用 |', '|---|---:|---:|',
        *[f"| {k} | {report['activation'][k]}（未知 {report['activation'][k+'_unknown']}）"
          f" | {report['negative'][k]}（未知 {report['negative'][k+'_unknown']}） |" for k in _ACTIVATION],
        f"\n绑定不适用（无需绑定槽）：明确适用 {report['activation']['bound_not_applicable']}，"
        f"明确不适用 {report['negative']['bound_not_applicable']}。",
        '', '| 可比对资源（两组同一子集） | A | B | B−A |', '|---|---:|---:|---:|',
    ]
    for k, row in (report['cost'] or {}).items():
        lines.append(f"| {k} | {shown(row['a'])} | {shown(row['b'])} | {shown(row['delta'])} |")
    lines += ['', '限制：' + ('、'.join(report['limitations']) or '无额外限制。'),
              '', '此报告不参与 Skill 资格、独立验收或 Release 发布。逐任务配对和原因见 results.csv。']
    return '\n'.join(lines) + '\n'
