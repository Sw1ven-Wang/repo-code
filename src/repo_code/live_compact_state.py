"""Host-owned, disposable projection records over append-only source evidence.

Blob fsync precedes the SQLite commit. A crash can leave an unreferenced blob or
PREPARED record, but never a committed card without verified original evidence.
"""
from __future__ import annotations
from contextlib import closing

from dataclasses import asdict
import json
import time

from .live_compact import LiveCompactProfile, indexed_groups, plan_cut, source_archive, card_messages, render_card
from .util import Faults, RepoError, canonical, digest, uid


def configuration(mode='legacy', profile=None):
    if mode not in {'legacy', 'live-experimental', 'live-experimental-v2', 'live-additive'} or (mode == 'legacy' and profile is not None):
        raise RepoError('INVALID_CONTEXT_COMPACTION')
    if mode == 'legacy':
        return None
    if mode == 'live-experimental-v2':
        from .projection_epoch import EpochProfile
        profile=EpochProfile.model_validate(profile or {})
        return {'mode':mode,'profile':profile.model_dump(),'profile_digest':digest(profile)}
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
        if c is None:
            # One fresh read connection per validation, not one per ancestor.
            # Every ownership/revocation check below still executes; nothing is
            # cached across requests or dispatch fences.
            with closing(self.store.connect()) as connection:
                return self._reference(run_id,ref,connection)
        owned = self.store.get('run_artifact', digest([run_id, ref]), c)
        if not owned or owned.get('run_id') != run_id or owned.get('artifact') != ref:
            raise RepoError('LIVE_COMPACT_SOURCE_DENIED')
        from .governance import Lineage
        if any(self.store.get('revocation', root, c) for root in {ref, *Lineage(self.store).ancestors(ref, c)}):
            raise RepoError('LIVE_COMPACT_SOURCE_REVOKED')
        return self.store.read_blob(ref)

    def _verify_sources(self, run_id, records, c=None):
        if c is None:
            with closing(self.store.connect()) as connection:
                return self._verify_sources(run_id,records,connection)
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
            cfg=self.runtime.run(run_id)['data']['manifest']['context_compaction']
            profile=LiveCompactProfile.model_validate(cfg['profile'])
            if profile.preserve_reasoning or card.get('kind') == 'live_compact_tool_projection':
                verified=self._verify_sources(run_id, source)
                expected=render_card([r['data'] for r in source], verified, cut['archive_ref'], profile)
                if not profile.preserve_reasoning or card != expected:
                    raise RepoError('LIVE_COMPACT_CARD_CORRUPT')
            view[start] = card_messages(card)
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
        profile=LiveCompactProfile.model_validate(self.runtime.run(run_id)['data']['manifest']['context_compaction']['profile'])
        if profile.preserve_reasoning or proposal.card.get('kind') == 'live_compact_tool_projection':
            expected=render_card([r['data'] for r in raw_source], self._verify_sources(run_id,raw_source),proposal.archive_ref,profile)
            if not profile.preserve_reasoning or proposal.card != expected:
                raise RepoError('LIVE_COMPACT_CARD_CORRUPT')
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
                key = digest([run_id, ref])
                owned = self.store.get('run_artifact', key, c)
                if owned:
                    # Legacy may already own these exact raw-source bytes.
                    # Keep its immutable registration; ownership is shared,
                    # while each checkpoint independently verifies its format.
                    if owned.get('run_id') != run_id or owned.get('artifact') != ref:
                        raise RepoError('LIVE_COMPACT_SOURCE_DENIED')
                else:
                    self.store.put('run_artifact', key,
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
        if cfg['mode']=='live-experimental-v2':
            from .projection_epoch import EpochProjection
            return EpochProjection(self.store,self.runtime,self.faults).project(run_id,slots,system=system,tools=tools,
                serialize=serialize,available_history=available_history,scale=scale,prefix_messages=prefix_messages,
                maintenance=maintenance,fits_limit=fits_limit)
        profile = LiveCompactProfile.model_validate(cfg['profile'])
        incremental = cfg['mode'] == 'live-additive'
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
        reason = 'UNCHANGED_SNAPSHOT'; count = 0; scanned_windows = []
        scan_next = (last or {}).get('scan_next', 0) if incremental else None
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
                try: indexed = indexed_groups(records)
                except (RepoError, ValueError, KeyError, TypeError): indexed = []
                pressure=fits_limit is not None and not fits_limit([*prefix_messages,*[m for g in view for m in g]])
                completed_before=None
                if run['data']['manifest'].get('conversation_mode'):
                    active=run['data'].get('active_turn',{}).get('text')
                    completed_before=next((i for i,g,_ in reversed(indexed_groups(records))
                        if len(g)==1 and g[0].get('role')=='user' and g[0].get('content')==active),0)
                for _ in range(profile.max_cuts):
                    scan_start = None
                    if incremental:
                        floor = max(after_group, (head or {}).get('group_end', -1)) + 1
                        end = min(len(indexed), completed_before if completed_before is not None else len(indexed))
                        scan_start = max(floor, scan_next)
                        if scan_start >= end: scan_start = floor
                        scan_next = min(end, scan_start + profile.max_scan_groups)
                        scanned_windows.append([scan_start, scan_next])
                        candidates = indexed[scan_start:scan_next]
                    else: candidates = indexed[-profile.max_scan_groups:]
                    recent = [r for _,_,source in candidates for r in source]
                    verified = {r['id']:r for r in self._verify_sources(run_id, recent)}
                    result = plan_cut([verified.get(r['id'],r) for r in records], view, system=system,
                        tools=tools, serialize=serialize, profile=profile, available_history=available_history,
                        head=head, scale=scale, prefix_messages=prefix_messages, after_group=after_group,oldest_first=incremental or pressure,
                        completed_before_group=completed_before, scan_start=scan_start,
                        trigger_on_history=incremental)
                    reason = result.reason
                    if not result.proposal:
                        # Skip a bounded window of ineligible old groups; do
                        # not let them permanently hide later eligible history.
                        if incremental and reason == 'NO_ELIGIBLE_SPAN' and scan_next < end:
                            continue
                        if incremental and reason == 'NO_ELIGIBLE_SPAN': scan_next = floor
                        break
                    if fits_limit is not None and not incremental:
                        # Do not freeze an already oversized prefix. Semantic
                        # fallback may still summarize these original sources.
                        end=result.proposal.group_end;start=result.proposal.group_start
                        minimum=[*prefix_messages,*[m for g in view[:start] for m in g],
                                 *card_messages(result.proposal.card),*[m for g in view[max(end+1,len(view)-profile.recent_groups):] for m in g]]
                        if not fits_limit(minimum):
                            reason='PREFIX_REQUIRES_SEMANTIC_FALLBACK';break
                    # Additive checkpoints retain verified raw sources and do
                    # not pin the prefix against Legacy folding. A positive,
                    # safe incremental cut need not reach low water by itself;
                    # ContextEngine still admits the final serialized request.
                    try:
                        self.commit(run_id, records, result.proposal, expected_fence)
                        view, head, chain = self.apply(run_id, records, slots, cfg['profile_digest'])
                        count += 1
                        if incremental: scan_next = head['group_end'] + 1
                    except (RepoError, OSError):
                        view, head, chain = self.apply(run_id, records, slots, cfg['profile_digest'])
                        reason = 'COMMIT_DEFERRED';break
                    if fits_limit is None or fits_limit([*prefix_messages, *[m for g in view for m in g]]):break
            else:
                reason = 'HOST_NOT_SETTLED'
            self.store.put('live_compact_maintenance', run_id,
                           {'stamp': digest([maintenance_fence, head, after_group]), 'reason': reason,
                            **({'scan_next': scan_next} if incremental else {})})
        if self.fence(run_id) != expected_fence:
            raise RepoError('LIVE_COMPACT_STALE')
        return view, {'reason': reason, 'cuts': [r['id'] for r in chain], 'head': head,
                      'group_positions': [r['group_start'] for r in chain], 'cut_count': count,
                      'savings_estimate': sum(r['savings_estimate'] for r in chain),
                      'history_rows': len(records), 'history_groups': len(slots),
                      'candidate_scan_slots': min(len(slots),profile.max_scan_groups),
                      **({'candidate_scan_windows': scanned_windows, 'scan_next': scan_next} if incremental else {}),
                      'seconds': time.monotonic() - started, 'fence': expected_fence}

    def assert_current(self, run_id, token):
        if token and token.get('version')==3:
            return self.assert_request_view(run_id,token)
        if token and token.get('version')==2:
            from .projection_epoch import EpochProjection
            return EpochProjection(self.store,self.runtime).assert_current(run_id,token)
        # Host budget reservation may increment revision between preparation and
        # dispatch. All source/control/authorization/manifest fences still match.
        current=self.fence(run_id)
        if token and {k:v for k,v in token.get('fence',{}).items() if k!='revision'} != {k:v for k,v in current.items() if k!='revision'}:
            raise RepoError('LIVE_COMPACT_STALE')

    def source_access(self, run_id, records, c):
        """Use raw lineage for views, archives and dispatch, including restores."""
        from .governance import Lineage
        for row in records:
            roots={run_id,'message:'+row['id'],digest(row['data'])}
            if row['data']['role']=='tool':
                try:body=json.loads(row['data'].get('content',''))
                except (TypeError,ValueError):body={}
                if isinstance(body,dict):
                    roots.update(body[k] for k in ('artifact','result_artifact','source_artifact','output_snapshot') if isinstance(body.get(k),str))
            for root in roots:
                if any(self.store.get('revocation',r,c) for r in {root,*Lineage(self.store).ancestors(root,c)}):
                    raise RepoError('LIVE_COMPACT_SOURCE_REVOKED')
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_control'").fetchone():
            if c.execute("SELECT 1 FROM memory_control WHERE id='restore_fence'").fetchone():
                raise RepoError('REVOCATION_SYNC_REQUIRED')

    def verify_folded_archive(self,run_id,records,folded_ids,summary_ref,c):
        if not summary_ref:
            if folded_ids:raise RepoError('REQUEST_PROJECTION_COVERAGE')
            return
        index=json.loads(self._reference(run_id,summary_ref,c));by_id={r['id']:r for r in records};restored=[]
        if index.get('kind')!='history_index' or not index.get('data_not_authority'):raise RepoError('REQUEST_PROJECTION_CORRUPT')
        for entry in index['groups']:
            group=json.loads(self._reference(run_id,entry['group_artifact'],c))
            try:rows=[by_id[i] for i in group['source_message_ids']]
            except (KeyError,TypeError):raise RepoError('REQUEST_PROJECTION_COVERAGE') from None
            if not group.get('data_not_authority') or digest(rows)!=group.get('source_digest'):
                raise RepoError('REQUEST_PROJECTION_CORRUPT')
            if json.loads(self._reference(run_id,group['raw_source_ref'],c))!=source_archive(rows):
                raise RepoError('REQUEST_PROJECTION_CORRUPT')
            # A raw row may contain only a bounded original observation. Its
            # owned recovery artifacts must remain readable as well; retaining
            # a dangling reference is not a restorable source chain.
            for row in rows:
                if row['data']['role']!='tool':continue
                try:result=json.loads(row['data'].get('content',''))
                except (TypeError,ValueError):result={}
                if isinstance(result,dict):
                    for field in ('artifact','result_artifact','source_artifact','output_snapshot'):
                        ref=result.get(field)
                        if isinstance(ref,str) and self.store.get('run_artifact',digest([run_id,ref]),c):
                            self._reference(run_id,ref,c)
            restored.extend(group['source_message_ids'])
        if sorted(restored)!=sorted(folded_ids):raise RepoError('REQUEST_PROJECTION_COVERAGE')

    def commit_request_view(self,run_id,records,request,folded_ids,summary_ref,live_trace,expected_fence):
        """Atomically publish a final request view, never rewrite a Live Head.

        This is a derived snapshot using the existing Store, not another context
        engine. Blobs precede one SQLite CAS. No PREPARED pointer is visible.
        Every build starts from raw sources, not the previous view or summary.
        """
        from .context import groups
        raw_groups=indexed_groups(records)
        covered=[i for _,_,rows in raw_groups for i in [r['id'] for r in rows]]
        if len(set(folded_ids))!=len(folded_ids) or not set(folded_ids)<=set(covered):
            raise RepoError('REQUEST_PROJECTION_COVERAGE')
        folded_set=set(folded_ids)
        if any(bool(folded_set & {r['id'] for r in rows}) and not {r['id'] for r in rows}<=folded_set for _,_,rows in raw_groups):
            raise RepoError('INCOMPLETE_TOOL_GROUP')
        self.faults.hit('request_view_before_source')
        source_ref=self.store.json_blob(source_archive(records))
        request_ref=self.store.json_blob(request)
        previous=self.store.get('active_request_projection',run_id)
        if previous:
            prior=self.store.get('context_request_projection',previous['id'])
            if (not prior or digest(prior['body'])!=prior['id'] or not self.store.verify(prior['body'],prior['signature'])
                    or previous!={'id':prior['id'],'generation':prior['body']['generation']} or prior['body']['run_id']!=run_id):
                raise RepoError('REQUEST_PROJECTION_CORRUPT')
        else:
            with closing(self.store.connect()) as c:
                if c.execute("SELECT 1 FROM objects WHERE kind='context_request_projection' AND json_extract(data,'$.body.run_id')=? LIMIT 1",(run_id,)).fetchone():
                    raise RepoError('REQUEST_PROJECTION_CORRUPT')
        body={'version':3,'run_id':run_id,'source_ref':source_ref,'source_digest':digest(records),
              'request_ref':request_ref,'request_digest':digest(request),'folded_source_ids':folded_ids,
              'summary_ref':summary_ref,'live_trace':live_trace,'fence':expected_fence,
              'parent':(previous or {}).get('id'),'generation':(previous or {}).get('generation',0)+1,
              'trust':'DERIVED_HISTORY_NOT_NEW_FACTS'}
        ident=digest(body)
        self.faults.hit('request_view_before_cas')
        with self.store.tx() as c:
            if self.fence(run_id,c)!=expected_fence or self.snapshot(run_id,c)!=records:
                raise RepoError('REQUEST_PROJECTION_STALE')
            if self.store.get('active_request_projection',run_id,c)!=previous:
                raise RepoError('REQUEST_PROJECTION_STALE')
            self.source_access(run_id,records,c)
            self.verify_folded_archive(run_id,records,folded_ids,summary_ref,c)
            refs={source_ref,request_ref}
            if summary_ref:
                index=json.loads(self._reference(run_id,summary_ref,c));refs.add(summary_ref)
                refs.update(g['group_artifact'] for g in index['groups'])
            roots={run_id}
            for row in records:
                roots.update(('message:'+row['id'],digest(row['data'])))
                if row['data']['role']=='tool':
                    try:result=json.loads(row['data'].get('content',''))
                    except (TypeError,ValueError):result={}
                    if isinstance(result,dict):roots.update(result[k] for k in ('artifact','result_artifact','source_artifact','output_snapshot') if isinstance(result.get(k),str))
            for ref in refs:
                self.store.read_blob(ref)
                self.store.put('run_artifact',digest([run_id,ref]),{'run_id':run_id,'artifact':ref,
                    'kind':(self.store.get('run_artifact',digest([run_id,ref]),c) or {}).get('kind','request_projection')},c)
            c.executemany('INSERT OR IGNORE INTO lineage(child,parent) VALUES(?,?)',[(source_ref,r) for r in roots if r!=source_ref])
            # Group archives already inherit their exact raw-group roots from
            # folded_archive(). Linking them to every later full snapshot would
            # grow their ancestry on every rebase and turn replay quadratic.
            c.executemany('INSERT OR IGNORE INTO lineage(child,parent) VALUES(?,?)',
                [(ref,source_ref) for ref in {request_ref,summary_ref} if ref and ref!=source_ref])
            self.store.put('context_request_projection',ident,{'id':ident,'body':body,'signature':self.store.sign(body)},c,immutable=True)
            self.store.put('active_request_projection',run_id,{'id':ident,'generation':body['generation']},c)
        self.faults.hit('request_view_after_cas')
        return {'version':3,'request_projection_id':ident,'generation':body['generation'],
                'request_digest':body['request_digest'],'fence':expected_fence}

    def assert_request_view(self,run_id,token):
        from .context import groups
        record=self.store.get('context_request_projection',token['request_projection_id'])
        if not record or digest(record['body'])!=record['id'] or not self.store.verify(record['body'],record['signature']):
            raise RepoError('REQUEST_PROJECTION_CORRUPT')
        body=record['body']
        if body['run_id']!=run_id or token['request_digest']!=body['request_digest'] or token['fence']!=body['fence']:
            raise RepoError('REQUEST_PROJECTION_CORRUPT')
        if self.store.get('active_request_projection',run_id)!={'id':record['id'],'generation':body['generation']}:
            raise RepoError('REQUEST_PROJECTION_STALE')
        with closing(self.store.connect()) as c:
            current=self.fence(run_id,c)
            if {k:v for k,v in current.items() if k!='revision'}!={k:v for k,v in body['fence'].items() if k!='revision'}:
                raise RepoError('REQUEST_PROJECTION_STALE')
            records=self.snapshot(run_id,c);self.source_access(run_id,records,c)
            if digest(records)!=body['source_digest'] or json.loads(self._reference(run_id,body['source_ref'],c))!=source_archive(records):
                raise RepoError('REQUEST_PROJECTION_CORRUPT')
            if digest(json.loads(self._reference(run_id,body['request_ref'],c)))!=body['request_digest']:
                raise RepoError('REQUEST_PROJECTION_CORRUPT')
            if body['summary_ref']:
                self.verify_folded_archive(run_id,records,body['folded_source_ids'],body['summary_ref'],c)
        if body['live_trace']:
            trace=body['live_trace']
            if self._chain(run_id)[0]!=trace.get('head'):raise RepoError('REQUEST_PROJECTION_STALE')
            cfg=self.runtime.run(run_id)['data']['manifest']['context_compaction']
            self.apply(run_id,records,groups([r['data'] for r in records]),cfg['profile_digest'])
            self.assert_current(run_id,trace)
        return body

    def record_request(self, run_id, request, usage=None, projection=None):
        """Measure complete native-prefix bytes separately from actual usage."""
        if projection and projection.get('version')==3:
            self.assert_request_view(run_id,projection)
            if digest(request)!=projection['request_digest']:raise RepoError('REQUEST_PROJECTION_MISMATCH')
        raw = canonical(request)
        previous = self.store.get('live_compact_request', run_id)
        common = None
        if previous:
            prior = self.store.read_blob(previous['request_ref'])
            common = next((i for i, (a, b) in enumerate(zip(prior, raw)) if a != b), min(len(prior), len(raw)))
        ref = self.store.blob(raw)
        data = {'id': uid('live-request-'), 'run_id': run_id, 'request_ref': ref,
                'serialized_request_digest':digest(request),
                'projection_epoch_id':(projection or {}).get('projection_epoch_id'),
                'epoch_generation':(projection or {}).get('epoch_generation'),
                'sequence': (previous or {}).get('sequence',0)+1,
                'previous_attempt_id': (previous or {}).get('id'),
                'bytes': len(raw), 'common_prefix_bytes': common,
                'usage': usage, 'estimate_only': True, 'cache_hit_inferred': False,
                'status': 'PREPARED', 'projection': {k:v for k,v in (projection or {}).items() if k!='fence'}}
        self.store.put('live_compact_request_attempt', data['id'], data)
        self.store.put('live_compact_request', run_id, data)
        return data

    def settle_request(self, attempt, usage, seconds, *, dispatched=True, failed=False):
        usage=usage or {}
        details=usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
        cache_read=next((v for v in (usage.get('cache_read_tokens'),usage.get('cache_read_input_tokens'),
            usage.get('cached_tokens'),details.get('cached_tokens')) if type(v) is int and v>=0),None)
        cache_write=next((v for v in (usage.get('cache_write_tokens'),usage.get('cache_creation_input_tokens'))
            if type(v) is int and v>=0),None)
        data = {**attempt, 'usage': usage, 'seconds': seconds,
                'actual_cache_read_tokens':cache_read,'actual_cache_write_tokens':cache_write,
                'status': 'NOT_DISPATCHED' if not dispatched else 'FAILED' if failed else 'COMPLETED',
                'usage_status': 'REPORTED' if usage else 'USAGE_UNKNOWN' if dispatched else 'NOT_DISPATCHED'}
        with self.store.tx() as c:
            self.store.put('live_compact_request_attempt', data['id'], data, c)
            latest = self.store.get('live_compact_request', data['run_id'], c)
            if latest and latest.get('id') == data['id']:
                self.store.put('live_compact_request', data['run_id'], data, c)
        return data
