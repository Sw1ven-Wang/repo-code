"""Cross-tool observation coverage supplements, never replaces, exact guards.

New code pages, changed failures, edits and justified process retries are useful
evidence. Reformatting an already answered inspection or adding `ls` is not.
Only repeated batches without new evidence stop; a lack of writes alone never
does. The stop is PARTIAL and cannot produce a verified delivery.
"""
from __future__ import annotations
import json
import re
from .commands import command_segments, inspection_command, verification_command, verification_summary
from .util import digest
from .work_status import full_result

WARN_BATCHES = 6
STOP_BATCHES = 10


def extend_interval(coverage, key, start, end):
    if end <= start: return False
    spans = coverage.setdefault(key, [])
    merged = sorted([*spans, [start, end]])
    union = []
    for a, b in merged:
        if union and a <= union[-1][1]: union[-1][1] = max(union[-1][1], b)
        else: union.append([a, b])
    old_size = sum(b-a for a, b in spans)
    # Coverage may be fragmented; cap metadata conservatively by forgetting
    # older ranges, which can only allow more work, never reject a new page.
    coverage[key] = union[-64:]
    return sum(b-a for a, b in union) > old_size


class TaskProgress:
    def __init__(self, store, runtime): self.store, self.runtime = store, runtime

    def batch(self, run_id, turn_id, pairs):
        run = self.runtime.run(run_id)
        state = {'source': self.runtime.workspace(run).snapshot(), 'control': run['control_epoch'],
                 'writer': run['writer_epoch'], 'environment': self.runtime.sandbox.environment_digest}
        previous = self.store.get('task_progress', run_id, default={})
        if previous.get('turn_id') == turn_id: return previous
        same = previous.get('state') == state
        seen = set(previous.get('seen', [])) if same else set()
        coverage = previous.get('coverage', {}) if same else {}
        retries = previous.get('retry_reasons', {}) if same else {}
        gained = False
        reading = []

        def observe_range(key,start,end):
            novel = extend_interval(coverage,key,start,end)
            if end > start:
                reading.append(novel)
            return novel

        def claim(value):
            key = digest(value)
            novel = key not in seen
            seen.add(key)
            return novel

        for req, receipt in pairs:
            body = full_result(self.store, run_id, receipt.get('result') or {})
            args, name = req.arguments, req.tool_name
            if body.get('executed') is False:
                if body.get('code') == 'INSPECTION_NO_PROGRESS' and name in {'read','artifact.read','history.expand'}:
                    reading.append(False)
                continue
            if name == 'read' and body.get('version'):
                start = args.get('start', 1)
                end = min(start + args.get('lines', 200), body.get('total_lines', start + args.get('lines', 200)) + 1)
                gained |= observe_range(digest(['file', args['path'], body['version']]),start,end)
            elif name == 'artifact.read':
                ref = body.get('source_artifact') or args['artifact']
                owner = self.store.get('run_artifact', digest([run_id, ref]), default={})
                target = 'folded-history-index' if owner.get('kind') == 'history_index' else ref
                start = body.get('offset', args.get('offset', 0))
                end = start + body.get('returned_bytes', len(str(body.get('content', '')).encode()))
                gained |= observe_range(digest(['artifact', target]),start,end)
            elif name == 'history.expand':
                # Resolve nested recovery outputs to an underlying immutable
                # artifact when present, instead of counting wrapper IDs.
                value = body; target = args['reference']
                for _ in range(4):
                    if value.get('source_artifact'):
                        target = value['source_artifact']; break
                    try: child = json.loads(value.get('content', ''))
                    except (ValueError, TypeError): break
                    if not isinstance(child, dict): break
                    value = child
                start = args.get('start', 0)
                gained |= observe_range(digest(['history', target]),start,start + len(body.get('content', '')))
            elif name in {'search', 'history.search', 'glob', 'memory.search', 'skill.search'}:
                if name == 'glob': evidence = ['paths', body.get('paths', [])]
                else: evidence = [name, args.get('query'), args.get('glob'), body.get('matches', [])]
                gained |= claim(evidence)
            elif name == 'process.run':
                argv = args.get('argv', [])
                if body.get('running') or body.get('cancelled') or body.get('timeout'):
                    gained |= claim(['incomplete', argv, body.get('code'), body.get('stderr')]); continue
                parts = command_segments(argv)
                file_read = False
                # Common alternate readers share coverage with read(). Unknown
                # commands retain normal output identity, not a purity claim.
                for words in parts:
                    path = words[-1] if len(words) > 1 else None
                    if words[0] not in {'cat', 'sed', 'head', 'tail'} or not path or path.startswith('-'): continue
                    try: value = self.runtime.workspace(run).read(path)
                    except Exception: continue
                    total = len(value['content'].splitlines()); start, end = 1, total + 1
                    if words[0] == 'sed':
                        match = re.search(r'^(\d+)(?:,(\d+|\$))?p$', words[-2])
                        if not match: continue
                        start = int(match[1]); end = total + 1 if match[2] == '$' else int(match[2] or match[1]) + 1
                    elif words[0] == 'head':
                        if words[1:2] != ['-n'] or len(words) != 4 or not words[2].isdigit(): continue
                        end = min(total + 1, int(words[2]) + 1)
                    elif words[0] == 'tail': continue  # do not invent tail offsets
                    gained |= observe_range(digest(['file', path, value['version']]),start,min(end,total+1))
                    file_read = True
                if file_read: continue
                if parts and all(w[0] in {'ls', 'pwd'} for w in parts):
                    gained |= claim(['environment-listing']); continue
                from .commands import command_evidence
                check = command_evidence(argv,body,args.get('stdin',''),strict=True,store=self.store,run=run)
                if check and check['verification'] and check['outcome'] == 'PASSED':
                    evidence = ['validation', parts, args.get('stdin', ''), verification_summary(body)]
                else:
                    evidence = ['process', parts or argv, args.get('stdin', ''), body.get('exit_code'),
                                body.get('stdout'), body.get('stderr'), body.get('code')]
                gained |= claim(evidence)
                # Keep two explicit same-state recovery opportunities across
                # timeout/presentation variations, not two per exact signature.
                if args.get('retry_reason'):
                    key=digest([parts or argv,args.get('stdin','')]);reasons=retries.setdefault(key,[])
                    if args['retry_reason'] not in reasons and len(reasons)<2:
                        reasons.append(args['retry_reason']);gained=True
            elif name in {'create', 'edit', 'patch', 'delete', 'move'}:
                # Real effects already reset the source/writer fence. Rejected
                # edits should expose a new diagnosis, not count as a write.
                gained |= claim([name, body.get('code'), body.get('versions'), body.get('changed_paths')])
            elif name == 'workspace.diff':
                gained |= claim(['diff-metadata',body.get('baseline'),body.get('files')])
                start=body.get('offset',args.get('offset',0))
                gained |= extend_interval(coverage,digest(['diff',body.get('baseline'),args.get('path')]),
                                          start,start+len(body.get('diff','')))
            elif name == 'plan.update':
                # A first plan is useful. Reworded model proposals are not
                # additional Host evidence and cannot reset stagnation forever.
                gained |= claim(['plan-present'])
            else:
                gained |= claim([name, body.get('code'), body.get('content'), body.get('matches')])
        streak = 0 if gained or not same else previous.get('stagnant_batches', 0) + 1
        operations = (previous.get('operations_in_state',0) if same else 0) + len(pairs)
        before_first_write = run['writer_epoch'] == 0
        window = ([*previous.get('investigation_window',[])] if same else [])
        window = [*window,[len(reading),reading.count(False),bool(gained)]][-12:]
        repeated = sum(batch[1] for batch in window)
        investigate = before_first_write and (operations >= 36 or (operations >= 24 and repeated >= 3))

        def low_progress(batches):
            reads = sum(batch[0] for batch in batches)
            return reads >= 5 and sum(batch[1] for batch in batches)*4 >= reads*3 and sum(batch[2] for batch in batches) <= 1

        persistent = (before_first_write and operations >= 36 and len(window) == 12 and not gained
                      and low_progress(window[:6]) and low_progress(window[6:]))
        view = {'turn_id': turn_id, 'state': state, 'seen': sorted(seen)[-1024:], 'coverage': dict(list(coverage.items())[-256:]),
                'operations_in_state': operations, 'investigation_window': window,
                'repeated_recent_reads': repeated, 'recent_reads': sum(batch[0] for batch in window),
                'investigation_warning': investigate, 'before_first_write': before_first_write,
                'persistent_investigation': persistent,
                'retry_reasons':dict(list(retries.items())[-128:]),
                'new_evidence': bool(gained), 'stagnant_batches': streak, 'warning': streak >= WARN_BATCHES,
                'stop': streak >= STOP_BATCHES or persistent}
        self.store.put('task_progress', run_id, view)
        return view
