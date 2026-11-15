"""Small deterministic work ledger shared by v1 and Memory v2.

Only Host observations are reported. A passing local command never implies
acceptance, completion of the user's request, or permission to stop early.
"""
from __future__ import annotations
import contextlib
import json
from .commands import command_evidence, command_segments
from .util import canonical, digest, redact_data


def operation_rows(store, run_id):
    with contextlib.closing(store.connect()) as c:
        rows = c.execute("SELECT rowid AS seq,id,request,result,approved_epoch,state FROM operations WHERE run_id=? AND result IS NOT NULL ORDER BY rowid", (run_id,)).fetchall()
    return [dict(row) for row in rows]


def full_result(store, run_id, result):
    if result.get('truncated') and (result.get('result_artifact') or result.get('artifact')):
        ref = result.get('result_artifact') or result['artifact']
        if store.get('run_artifact', digest([run_id, ref])):
            try:
                body = store.read_json(ref)
                if isinstance(body, dict): return body
            except (ValueError, UnicodeError): pass
    return result


def compact_check(entry):
    keys = ('operation','argv_summary','exit_code','result_summary','source_digest','evidence_kind','scope','outcome','artifact')
    return {key:entry[key] for key in keys if entry.get(key) is not None}


def work_status(store, runtime, run, *, snapshot=None, files=None, review_after=None, required_probes=(), repair_checks=None):
    from .memory.working_state import semantic_text
    current = snapshot if snapshot is not None else runtime.workspace(run).snapshot()
    baseline_digest = run['data']['manifest']['workspace_baseline_digest']
    baseline = store.read_json(baseline_digest)['files']
    if files is None:
        files = store.read_json(current)['files']
    changes = sorted(p for p in baseline.keys() | files.keys() if baseline.get(p) != files.get(p))
    write = None; verification = None; last_command = None; readonly = 0
    probes = {}
    checks = {}
    earlier_checks = set()
    replayable = {}
    checked_probes = {}
    failed_write = None
    current_write = None
    changed_by_write = set()
    reverted_write = None
    attempted_check_failure = None
    baseline_check_failure = None
    baseline_reproduction = None
    check_failures = {}
    rows = operation_rows(store, run['id'])
    for row in rows:
        request = json.loads(row['request']); args = request['arguments']; tool = request['tool_name']
        body = full_result(store, run['id'], json.loads(row['result']))
        if body.get('executed') is False: continue
        changed = body.get('changed_paths')
        process_write = (tool in {'process.run', 'process.status'} and body.get('relevant_state') and body.get('output_snapshot')
                         and body['relevant_state'] != body['output_snapshot'])
        if (tool in {'create', 'edit', 'patch', 'delete', 'move'} and changed and not body.get('is_error')) or process_write:
            write = {'operation': row['id'], 'operation_sequence': row['seq'], 'tool': tool,
                     'paths': changed[:12] if changed else [], 'source_digest': body.get('output_snapshot'),
                     'versions': dict(list(body.get('versions', {}).items())[:3])}
            readonly = 0
        else: readonly += 1
        same_epoch = row['approved_epoch'] == run['control_epoch']
        if review_after is not None and same_epoch:
            if write and write['operation'] == row['id']:
                current_write = write
                failed_write = None
                versions = body.get('versions', {})
                restored = [p for p, version in versions.items()
                            if p in changed_by_write and version == baseline.get(p, {}).get('blob')]
                if restored or (process_write and body['output_snapshot'] == baseline_digest):
                    reverted_write = {**write, 'paths': restored[:12] or write['paths']}
                changed_by_write.update(p for p, version in versions.items()
                                        if version != baseline.get(p, {}).get('blob'))
            if body.get('code') == 'WORKSPACE_WRITE_NO_EFFECT' or (
                    body.get('code') == 'AMBIGUOUS_OR_MISSING_MATCH'
                    and body.get('file_version') == files.get(body.get('path'), {}).get('blob')):
                failed_write = {'operation': row['id'], 'code': body['code']}
                if body.get('path'):
                    failed_write.update(path=body['path'], match_count=body.get('match_count'),
                                        read_windows=body.get('read_windows', []))
        if tool == 'process.run' and isinstance(body.get('exit_code'), int):
            last_command = {'operation': row['id'], 'argv': semantic_text(str(args.get('argv', [])))[:240], 'exit_code': body['exit_code']}
            argv = args.get('argv',[])
            evidence = command_evidence(argv,body,args.get('stdin',''),strict=True,store=store,run=run,
                                        operation_id=row['id'],approved_epoch=row['approved_epoch'])
            if evidence is None:
                continue
            observed = body.get('output_snapshot') or body.get('relevant_state')
            validation = {'operation': row['id'], 'operation_sequence': row['seq'],
                          'argv_summary': semantic_text(str(argv))[:240], 'exit_code': body['exit_code'],
                          'result_summary': redact_data(evidence['summary'])[:180], 'source_digest': observed,
                          'evidence_kind': evidence['evidence_kind'], 'scope': evidence['scope'], 'outcome': evidence['outcome'],
                          'validity': 'CURRENT' if observed == current and body.get('environment_digest') == runtime.sandbox.environment_digest else 'HISTORICAL',
                          'same_control_epoch': same_epoch,
                          'complete': not any(body.get(k) for k in ('timeout', 'cancelled', 'running')),
                          'artifact': body.get('result_artifact') or body.get('artifact')}
            if (body.get('observed_probe') == row['id'] and validation['same_control_epoch']
                    and validation['complete'] and body.get('environment_digest') == runtime.sandbox.environment_digest):
                stdout = str(body.get('stdout', ''))
                replayable[row['id']] = {'operation': row['id'], 'probe': validation['argv_summary'],
                                         'stdout_excerpt': redact_data(stdout[:360]),
                                         'print_arity': body.get('probe_print_arity', [1]),
                                         'key': digest([argv, args.get('stdin', '')])}
                if review_after is not None and row['seq'] > review_after and observed == baseline_digest:
                    baseline_reproduction = compact_check(validation)
            if (review_after is not None and same_epoch and validation['complete'] and observed
                    and validation['outcome'] == 'FAILED'
                    and body.get('environment_digest') == runtime.sandbox.environment_digest):
                if observed == baseline_digest:
                    baseline_check_failure = compact_check(validation)
                    if (row['seq'] > review_after and validation['evidence_kind'] == 'ASSERTION_CHECK'
                            and 'AssertionError' in str(body.get('stderr') or '')[-1200:]):
                        baseline_reproduction = baseline_check_failure
                else:
                    attempted_check_failure = compact_check(validation)
            if not evidence['verification']:
                if validation['validity'] == 'CURRENT' and validation['same_control_epoch'] and validation['complete']:
                    segments = command_segments(argv)
                    key = tuple(segments[-1][:2])
                    probes.pop(key,None)
                    probes[key] = validation
                continue
            ref = body.get('checked_probe')
            key = digest(['linked_probe', ref] if ref else [argv, args.get('stdin', '')])
            if (review_after is not None and not ref and validation['same_control_epoch']
                    and validation['complete'] and observed and observed != baseline_digest
                    and body.get('environment_digest') == runtime.sandbox.environment_digest
                    and validation['outcome'] == 'FAILED'):
                detail = compact_check(validation)
                if not args.get('stdin') and len(canonical(argv)) <= 1200:
                    safe_argv = redact_data(argv)
                    if safe_argv == argv:
                        detail['argv'] = safe_argv
                check_failures.pop(key, None)
                check_failures[key] = detail
            if validation['validity'] == 'CURRENT' and validation['same_control_epoch']:
                if body.get('checked_probe') and validation['complete']:
                    checked_probes[body['checked_probe']] = validation['outcome']
                checks.pop(key,None)
                checks[key] = validation
                if review_after is not None and row['seq'] <= review_after:
                    earlier_checks.add(key)
            # An older-source check does not evict a check of the current candidate.
            if verification is None or validation['validity'] == 'CURRENT' or verification['validity'] != 'CURRENT':
                verification = validation
    plan = store.get('plan', run['id'], default={})
    turn = run['data'].get('active_turn', {})
    steps = plan.get('steps', []) if not turn or plan.get('turn_id') == turn.get('turn_id') else []
    # Retain bounded metadata even when optional context observations are removed.
    review = {}
    if review_after is not None:
        selected = {ref: replayable[ref] for ref in required_probes if ref in replayable}
        keys = {p['key'] for p in selected.values()}
        for ref, probe in reversed(list(replayable.items())):
            if len(selected) >= 2:
                break
            if probe['key'] not in keys:
                selected[ref] = probe
                keys.add(probe['key'])
        repairs = dict(repair_checks or {})
        for key in reversed(check_failures):
            if len(repairs) >= 4:
                break
            if key not in checks or not checks[key]['complete'] or checks[key]['outcome'] != 'PASSED':
                repairs.setdefault(key, check_failures[key])
        repaired = {key: checks[key]['operation'] for key in repairs
                    if key in checks and checks[key]['complete'] and checks[key]['outcome'] == 'PASSED'}
        pending_repairs = {key: check_failures.get(key, detail) for key, detail in repairs.items() if key not in repaired}
        review = {'review_evidence': {
            'failed_write': failed_write or (current_write if not changes else None),
            'reverted_write': reverted_write if not changes else None,
            'attempted_check_failure': attempted_check_failure,
            'baseline_check_failure': baseline_check_failure,
            'baseline_reproduction': baseline_reproduction,
            'repair_checks': repairs,
            'pending_repairs': pending_repairs,
            'repaired_checks': list(repaired.values()),
            'required_probes': list(selected),
            'pending_probes': [{**{k: p[k] for k in ('operation', 'probe', 'stdout_excerpt', 'print_arity')},
                                'outcome': checked_probes.get(ref)}
                               for ref, p in selected.items() if checked_probes.get(ref) != 'PASSED'],
            'after_operation': rows[-1]['seq'] if rows else 0,
            'new_passing_checks': [x['operation'] for key, x in checks.items()
                                   if key not in earlier_checks and x['operation_sequence'] > review_after
                                   and x['complete'] and x['outcome'] == 'PASSED'][-4:],
            'failing_checks': [x['operation'] for x in checks.values()
                               if x['complete'] and x['outcome'] == 'FAILED'][-4:],
        }}
    return {'current_turn_id': turn.get('turn_id'), 'source_digest': current,
            'changed_path_count': len(changes), 'changed_paths': changes[:20], 'paths_truncated': len(changes) > 20,
            'last_write': write, 'verification': verification,
            'behavior_probes': [compact_check(x) for x in list(probes.values())[-2:]],
            'other_current_checks': [compact_check(x) for x in list(checks.values())[-2:] if x != verification],
            'last_process': ({**last_command, 'argv': str(last_command['argv'])[:240]} if last_command else None),
            'readonly_operations_since_write': readonly if write else None,
            'proposed_next_steps': [str(s)[:180] for s in steps[:3]],
            'delivery': 'A no-tool final response can hand off this candidate and its local evidence. State remaining limitations; local checks are not an external verdict.',
            **review}
