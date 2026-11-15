"""Host-owned, disposable projection records over append-only source evidence.

Blob fsync precedes the SQLite commit. A crash can leave an unreferenced blob or
PREPARED record, but never a committed card without verified original evidence.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import time

from .live_compact import LiveCompactProfile, indexed_groups, plan_cut, source_archive
from .util import Faults, RepoError, canonical, digest, uid


def configuration(mode='legacy', profile=None):
    if mode not in {'legacy', 'live-experimental'} or (mode == 'legacy' and profile is not None):
        raise RepoError('INVALID_CONTEXT_COMPACTION')
    if mode == 'legacy':
        return None
    profile = LiveCompactProfile.model_validate(profile or {})
    return {'mode': mode, 'profile': profile.model_dump(), 'profile_digest': digest(profile)}


class LiveProjection:
    def __init__(self, store, runtime, faults=None):
        self.store, self.runtime = store, runtime
        self.faults = faults or Faults()

    def snapshot(self, run_id, c=None):
        """Read actual Host row IDs, not reconstructible array positions."""
        own = c is None; c = c or self.store.connect()
        try:
            return [{'id': str(r['id']), 'operation_id': r['operation_id'],
                     'operation_state': r['operation_state'], 'data': json.loads(r['data'])}
                    for r in c.execute('''SELECT m.id,m.operation_id,m.data,o.state AS operation_state
                        FROM messages m LEFT JOIN operations o ON m.operation_id=o.id AND m.run_id=o.run_id
                        WHERE m.run_id=? ORDER BY m.id''', (run_id,))]
        finally:
            if own: c.close()

    def fence(self, run_id, c=None):
        own = c is None; c = c or self.store.connect()
        try:
            run = self.runtime.run(run_id, c)
            revocations = [tuple(r) for r in c.execute("SELECT id,data FROM objects WHERE kind='revocation' ORDER BY id")]
            memory_control = []
            summary_heads = []
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_control'").fetchone():
                memory_control = [tuple(r) for r in c.execute("SELECT id,data FROM memory_control WHERE id IN ('revocation','restore_fence') ORDER BY id")]
                summary_heads = [tuple(r) for r in c.execute('SELECT id,data FROM memory_control WHERE id LIKE ? OR id=? ORDER BY id',
                    ('summary:'+run_id+'%', 'summary_segments:'+run_id))]
            operations = [tuple(r) for r in c.execute('SELECT id,state,result FROM operations WHERE run_id=? ORDER BY id', (run_id,))]
            return {'control_epoch': run['control_epoch'], 'writer_epoch': run['writer_epoch'],
                    'revision': run['revision'], 'state': run['state'],
                    'authorization': digest(run['data'].get('authorization', {})),
                    'manifest': digest(run['data']['manifest']),
                    'revocations': digest([revocations, memory_control]),
                    'summary_heads': digest(summary_heads),
                    'messages': digest(self.snapshot(run_id, c)), 'operations': digest(operations)}
        finally:
            if own: c.close()

    def _reference(self, run_id, ref, c=None):
        owned = self.store.get('run_artifact', digest([run_id, ref]), c)
        if not owned or owned.get('run_id') != run_id or owned.get('artifact') != ref:
            raise RepoError('LIVE_COMPACT_SOURCE_DENIED')
        from .governance import Lineage
        if any(self.store.get('revocation', root, c) for root in {ref, *Lineage(self.store).ancestors(ref, c)}):
            raise RepoError('LIVE_COMPACT_SOURCE_REVOKED')
        return self.store.read_blob(ref)

    def _verify_sources(self, run_id, records, c=None):
        verified = []
        for row in records:
            row = dict(row); row['references_verified'] = True
            if row['data'].get('role') == 'tool':
                try:
                    if row.get('operation_state')!='SUCCEEDED' or not row.get('operation_id'):
                        raise ValueError('tool receipt is not settled successfully')
                    body = json.loads(row['data'].get('content', ''))
                    if not isinstance(body, dict):
                        raise ValueError('unstructured result')
                    for key in ('artifact', 'result_artifact', 'source_artifact'):
                        ref = body.get(key)
                        if ref is not None:
                            raw = self._reference(run_id, ref, c)
                            if body.get('truncated') and key == 'artifact':
                                row['full_result'] = json.loads(raw)
                                if isinstance(row['full_result'],dict):
                                    for nested in ('artifact','result_artifact','source_artifact'):
                                        ref=row['full_result'].get(nested)
                                        if ref is not None:self._reference(run_id,ref,c)
                except (RepoError, OSError, ValueError, TypeError):
                    row['references_verified'] = False
            verified.append(row)
        return verified

    def _chain(self, run_id, c=None):
        head = self.store.get('live_compact_head', run_id, c)
        chain = []; seen = set(); current = head
        while current:
            if current['id'] in seen:
                raise RepoError('LIVE_COMPACT_CHECKPOINT_CORRUPT')
            seen.add(current['id'])
            record = self.store.get('live_compact_cut', current['id'], c)
            if (not record or record['cut_state'] != 'COMMITTED' or record['run_id'] != run_id
                    or record['id'] != current['id'] or record['group_end'] != current['group_end']):
                raise RepoError('LIVE_COMPACT_CHECKPOINT_CORRUPT')
            chain.append(record)
            previous = record['previous_checkpoint']
            if previous:
                parent = self.store.get('live_compact_cut', previous, c)
                if not parent or parent['group_end'] >= record['group_start']:
                    raise RepoError('LIVE_COMPACT_CHECKPOINT_CORRUPT')
                current = {'id': previous, 'group_end': parent['group_end']}
            else:
                current = None
        return head, list(reversed(chain))

    def apply(self, run_id, records, slots, profile_digest):
        head, chain = self._chain(run_id)
        indexed = indexed_groups(records)
        view = list(slots)
        for cut in chain:
            start, end = cut['group_start'], cut['group_end']
            if cut['profile_digest'] != profile_digest or not 0 <= start <= end < len(indexed):
                raise RepoError('LIVE_COMPACT_CHECKPOINT_CORRUPT')
            source = [r for _,_,rows in indexed[start:end+1] for r in rows]
            if ([r['id'] for r in source] != cut['source_message_ids']
                    or digest([r['data'] for r in source]) != cut['raw_digest']):
                raise RepoError('LIVE_COMPACT_SOURCE_CHANGED')
            archive = json.loads(self._reference(run_id, cut['archive_ref']))
            if archive != source_archive(source):
                raise RepoError('LIVE_COMPACT_SOURCE_CHANGED')
            if not all(r['references_verified'] for r in self._verify_sources(run_id, source)):
                raise RepoError('LIVE_COMPACT_SOURCE_MISSING')
            card = json.loads(self._reference(run_id, cut['card_digest']))
            if digest(card) != cut['card_digest']:
                raise RepoError('LIVE_COMPACT_CARD_CORRUPT')
            view[start] = [card]
            for position in range(start+1, end+1):view[position] = []
        return view, head, chain

    def recover(self, run_id):
        # Only auxiliary records are aborted; raw evidence and committed heads
        # are untouched. A later plan may retry the same deterministic proposal.
        with self.store.tx() as c:
            rows = c.execute("SELECT id,data FROM objects WHERE kind='live_compact_cut'").fetchall()
            for row in rows:
                value = json.loads(row['data'])
                if value['run_id'] == run_id and value['cut_state'] == 'PREPARED':
                    value['cut_state'] = 'ABORTED'
                    self.store.put('live_compact_cut', row['id'], value, c)

    def commit(self, run_id, records, proposal, expected_fence):
        raw_source = [r for r in records if r['id'] in proposal.source_message_ids]
        ident = digest([run_id, proposal.source_message_ids, proposal.raw_digest,
                        proposal.profile_digest, proposal.previous_checkpoint])
        record = {**asdict(proposal), 'source_message_ids': list(proposal.source_message_ids),
                  'id': ident, 'run_id': run_id, 'control_epoch': expected_fence['control_epoch'],
                  'writer_epoch': expected_fence['writer_epoch'], 'cut_state': 'PREPARED'}
        record.pop('card')
        with self.store.tx() as c:
            previous = self.store.get('live_compact_cut', ident, c)
            if previous and previous['cut_state'] == 'COMMITTED':
                return previous
            if self.fence(run_id, c) != expected_fence:
                raise RepoError('LIVE_COMPACT_STALE')
            self.store.put('live_compact_cut', ident, record, c)
        if not all(r['references_verified'] for r in self._verify_sources(run_id, raw_source)):
            raise RepoError('LIVE_COMPACT_SOURCE_MISSING')
        archive_ref = self.store.json_blob(source_archive(raw_source))
        if archive_ref != proposal.archive_ref:
            raise RepoError('LIVE_COMPACT_SOURCE_CHANGED')
        self.faults.hit('live_original_confirmed')
        card_ref = self.store.json_blob(proposal.card)
        if card_ref != proposal.card_digest:
            raise RepoError('LIVE_COMPACT_CARD_CORRUPT')
        self.faults.hit('live_card_written')
        self.faults.hit('live_before_commit')
        with self.store.tx() as c:
            head, _ = self._chain(run_id, c)
            already=self.store.get('live_compact_cut',ident,c)
            if already and already['cut_state']=='COMMITTED':
                return already
            if ((head or {}).get('id') != proposal.previous_checkpoint
                    or (head and proposal.group_start <= head['group_end'])
                    or self.fence(run_id, c) != expected_fence):
                raise RepoError('LIVE_COMPACT_STALE')
            current = self.snapshot(run_id, c)
            source = [r for r in current if r['id'] in proposal.source_message_ids]
            if (source_archive(source) != source_archive(raw_source)
                    or digest([r['data'] for r in source]) != proposal.raw_digest
                    or not all(r['references_verified'] for r in self._verify_sources(run_id, source, c))):
                raise RepoError('LIVE_COMPACT_SOURCE_CHANGED')
            # Files are already durable. Validate bytes again before visibility.
            self.store.read_blob(archive_ref); self.store.read_blob(card_ref)
            for ref, kind in ((archive_ref, 'live_compact_source'), (card_ref, 'live_compact_card')):
                self.store.put('run_artifact', digest([run_id, ref]),
                    {'run_id': run_id, 'artifact': ref, 'kind': kind}, c, immutable=True)
            record['cut_state'] = 'COMMITTED'
            self.store.put('live_compact_cut', ident, record, c)
            self.store.put('live_compact_head', run_id, {'id': ident, 'group_end': proposal.group_end}, c)
        self.faults.hit('live_after_commit')
        return record

    def project(self, run_id, slots, *, system, tools, serialize, available_history,
                scale=1.0, prefix_messages=(), maintenance=True, fits_limit=None, after_group=-1):
        run = self.runtime.run(run_id)
        cfg = run['data']['manifest'].get('context_compaction')
        if not cfg:
            return slots, {'reason': 'LEGACY', 'cuts': [], 'head': None}
        profile = LiveCompactProfile.model_validate(cfg['profile'])
        if digest(profile) != cfg['profile_digest']:
            raise RepoError('RUN_CONTEXT_COMPACTION_CHANGED')
        started = time.monotonic()
        expected_fence = self.fence(run_id)
        records = self.snapshot(run_id)
        if self.fence(run_id) != expected_fence:
            raise RepoError('LIVE_COMPACT_STALE')
        view, head, chain = self.apply(run_id, records, slots, cfg['profile_digest'])
        maintenance_fence = {k:v for k,v in expected_fence.items() if k != 'revision'}
        stamp = digest([maintenance_fence, head, after_group])
        last = self.store.get('live_compact_maintenance', run_id)
        reason = 'UNCHANGED_SNAPSHOT'; count = 0
        if maintenance and (not last or last['stamp'] != stamp):
            self.recover(run_id)
            safe = (run['state'] == 'RUNNING' and not run['cancel_requested']
                    and not self.runtime.unresolved(run_id)
                    and not self.store.get('pending_turn', run_id, default={}).get('requests'))
            # A live background lease is not a completed tool observation.
            safe = safe and not any(x.get('run_id') == run_id and x.get('state') != 'STOPPED'
                                    for x in self.store.values('lease'))
            if safe:
                self.faults.hit('live_before_card')
                try:
                    recent = [r for _,_,source in indexed_groups(records)[-profile.max_scan_groups:] for r in source]
                except (RepoError, ValueError, KeyError, TypeError):
                    recent = []
                verified = {r['id']:r for r in self._verify_sources(run_id, recent)}
                pressure=fits_limit is not None and not fits_limit([*prefix_messages,*[m for g in view for m in g]])
                for _ in range(profile.max_cuts):
                    result = plan_cut([verified.get(r['id'],r) for r in records], view, system=system,
                        tools=tools, serialize=serialize, profile=profile, available_history=available_history,
                        head=head, scale=scale, prefix_messages=prefix_messages, after_group=after_group,oldest_first=pressure)
                    reason = result.reason
                    if not result.proposal:break
                    if fits_limit is not None:
                        # Do not freeze an already oversized prefix. Semantic
                        # fallback may still summarize these original sources.
                        end=result.proposal.group_end;start=result.proposal.group_start
                        minimum=[*prefix_messages,*[m for g in view[:start] for m in g],
                                 result.proposal.card,*[m for g in view[max(end+1,len(view)-profile.recent_groups):] for m in g]]
                        if not fits_limit(minimum):
                            reason='PREFIX_REQUIRES_SEMANTIC_FALLBACK';break
                    try:
                        self.commit(run_id, records, result.proposal, expected_fence)
                        view, head, chain = self.apply(run_id, records, slots, cfg['profile_digest'])
                        count += 1
                    except (RepoError, OSError):
                        view, head, chain = self.apply(run_id, records, slots, cfg['profile_digest'])
                        reason = 'COMMIT_DEFERRED';break
                    if fits_limit is None or fits_limit([*prefix_messages, *[m for g in view for m in g]]):break
            else:
                reason = 'HOST_NOT_SETTLED'
            self.store.put('live_compact_maintenance', run_id,
                           {'stamp': digest([maintenance_fence, head, after_group]), 'reason': reason})
        if self.fence(run_id) != expected_fence:
            raise RepoError('LIVE_COMPACT_STALE')
        return view, {'reason': reason, 'cuts': [r['id'] for r in chain], 'head': head,
                      'group_positions': [r['group_start'] for r in chain], 'cut_count': count,
                      'savings_estimate': sum(r['savings_estimate'] for r in chain),
                      'history_rows': len(records), 'history_groups': len(slots),
                      'candidate_scan_slots': min(len(slots),profile.max_scan_groups),
                      'seconds': time.monotonic() - started, 'fence': expected_fence}

    def assert_current(self, run_id, token):
        # Host budget reservation may increment revision between preparation and
        # dispatch. All source/control/authorization/manifest fences still match.
        current=self.fence(run_id)
        if token and {k:v for k,v in token.get('fence',{}).items() if k!='revision'} != {k:v for k,v in current.items() if k!='revision'}:
            raise RepoError('LIVE_COMPACT_STALE')

    def record_request(self, run_id, request, usage=None, projection=None):
        """Measure complete native-prefix bytes separately from actual usage."""
        raw = canonical(request)
        previous = self.store.get('live_compact_request', run_id)
        common = None
        if previous:
            prior = self.store.read_blob(previous['request_ref'])
            common = next((i for i, (a, b) in enumerate(zip(prior, raw)) if a != b), min(len(prior), len(raw)))
        ref = self.store.blob(raw)
        data = {'id': uid('live-request-'), 'run_id': run_id, 'request_ref': ref,
                'sequence': (previous or {}).get('sequence',0)+1,
                'previous_attempt_id': (previous or {}).get('id'),
                'bytes': len(raw), 'common_prefix_bytes': common,
                'usage': usage, 'estimate_only': True, 'cache_hit_inferred': False,
                'status': 'PREPARED', 'projection': {k:v for k,v in (projection or {}).items() if k!='fence'}}
        self.store.put('live_compact_request_attempt', data['id'], data)
        self.store.put('live_compact_request', run_id, data)
        return data

    def settle_request(self, attempt, usage, seconds, *, dispatched=True, failed=False):
        data = {**attempt, 'usage': usage, 'seconds': seconds,
                'status': 'NOT_DISPATCHED' if not dispatched else 'FAILED' if failed else 'COMPLETED',
                'usage_status': 'REPORTED' if usage else 'USAGE_UNKNOWN' if dispatched else 'NOT_DISPATCHED'}
        with self.store.tx() as c:
            self.store.put('live_compact_request_attempt', data['id'], data, c)
            latest = self.store.get('live_compact_request', data['run_id'], c)
            if latest and latest.get('id') == data['id']:
                self.store.put('live_compact_request', data['run_id'], data, c)
        return data
