#!/usr/bin/env python3
"""Render Skill Impact from a trusted Host snapshot without opening Host for writes."""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
import io
import json
from pathlib import Path
import sqlite3

from repo_code.skill_impact import compare_skill_pairs, render_markdown
from repo_code.util import atomic_write, digest


def _get(db, kind, ref):
    row = db.execute('SELECT data FROM objects WHERE kind=? AND id=?', (kind, ref)).fetchone()
    return json.loads(row[0]) if row else None


def _stage(db, phase, candidate):
    rows = db.execute("SELECT data FROM objects WHERE kind=? AND json_extract(data,'$.candidate')=?",
                      ('skill_' + phase, candidate))
    reports = [json.loads(r[0]) for r in rows]
    if len(reports) != 1:
        raise ValueError('No unique S2/S3 report for this candidate and phase.')
    return reports[0]


def read_impact(state, candidate, phase):
    path = (Path(state) / 'runtime.db').resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only = ON')
        db.execute('BEGIN')
        report = _stage(db, phase, candidate)
        ref = report.get('impact_ref')
        sealed = {k: v for k, v in report.items() if k != 'impact_ref'}
        attached = _get(db, 'skill_impact_stage', digest(sealed))
        if attached:
            ref = attached['impact_ref']
        impact = _get(db, 'skill_impact_report', ref) if ref else None
        if ref and (not impact or impact.get('content_digest') != digest(
                {k: impact[k] for k in ('identity', 'plan', 'report')}) or digest(impact['identity']) != ref):
            raise ValueError('Skill Impact record integrity check failed.')
        trials = []
        for saved in report.get('trials', []):
            base = (_get(db, 'trial_private' if saved.get('hidden') else 'trial_dev', saved['id'])
                    if saved.get('id') else None)
            if base and any(saved.get(k) != base.get(k) for k in ('id', 'run_id', 'task_id', 'release', 'state')):
                raise ValueError('The trial reference disagrees with the stage report.')
            trials.append({**(base or {}), **saved})
        if impact:
            identity = impact['identity']
            if (identity['ordered_trial_ids'] != [t.get('id') for t in trials]
                    or identity['contract_ref'] != report['contract_ref']
                    or identity['phase'] != phase.upper() or identity['candidate'] != candidate):
                raise ValueError('Skill Impact does not belong to this S2/S3 report.')
        plan = impact['plan'] if impact else None
        expected = compare_skill_pairs(trials, report['contract_ref'], candidate=candidate, phase=phase.upper(),
                                       plan=plan, evidence_class=report.get('evidence_class', 'LOCAL_DIAGNOSTIC'))
        if impact and expected != impact['report']:
            raise ValueError('Skill Impact record does not reproduce from its trial sources.')
    return expected


def _cell(value):
    if type(value) is bool:
        return str(value).lower()
    # Do not let task identifiers become formulas when opened in a spreadsheet.
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def write_outputs(out, report):
    out = Path(out)
    columns = ('quality', 'delivery_recorded', 'usage_settled', 'source_status', 'binding_required',
               'retrieved', 'offered', 'bound', 'applied', 'verified', 'model_attempts', 'tool_calls',
               'input_tokens', 'output_tokens', 'cache_tokens', 'wall_seconds', 'actual_cost')
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(['task_id', 'attempt_index', 'expected_activation', 'comparable', 'reasons',
                     *(f'{arm}_{key}' for arm in ('a', 'b') for key in columns)])
    for pair in report['pairs']:
        rows = [pair[k] for k in ('task_id', 'attempt_index', 'expected_activation', 'comparable')]
        rows.append(';'.join(pair['reasons']))
        for arm in ('a', 'b'):
            row = pair[arm] or {}
            merged = {**row, **row.get('trace', {}), **row.get('cost', {})}
            rows.extend(merged.get(k) for k in columns)
        writer.writerow(map(_cell, rows))
    summary = {k: v for k, v in report.items() if k != 'pairs'}
    for name, content in {
        'results.csv': buffer.getvalue(),
        'summary.json': json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + '\n',
        'report.md': render_markdown(report),
    }.items():
        atomic_write(out / name, content.encode('utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    report = sub.add_parser('report', help='Generate an offline report from an existing S2/S3 result.')
    report.add_argument('--state', type=Path, required=True)
    report.add_argument('--candidate-id', required=True)
    report.add_argument('--phase', choices=['s2', 's3'], required=True)
    report.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    # Protect the Host state from accidental export into its own directory.
    if args.out.resolve().is_relative_to(args.state.resolve()):
        parser.error('--out must be outside --state')
    try:
        write_outputs(args.out, read_impact(args.state, args.candidate_id, args.phase))
    except (OSError, sqlite3.Error, ValueError, KeyError) as e:
        parser.exit(1, f'Skill Impact report unavailable: {e}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
