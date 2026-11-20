"""Deterministic DEV-only stage triage over existing facts, not a second memory DB."""
import contextlib
import json
from .util import digest, RepoError
from .governance import Lineage


class EpisodeBuilder:
    TRIGGERS = {'run.ended','delivery.committed','control.admitted','candidate.review_finished'}
    FORBIDDEN_KEYS = {'gold_patch','test_patch','FAIL_TO_PASS','PASS_TO_PASS','hidden_tests'}

    def __init__(self, store):
        self.store = store

    def _authorized(self, roots, c):
        for root in roots:
            for parent in {root,*Lineage(self.store).ancestors(root,c)}:
                if self.store.get('revocation',parent,c):
                    raise RepoError('EPISODE_SOURCE_REVOKED')
                if c.execute('SELECT 1 FROM exposures WHERE artifact=?',(parent,)).fetchone():
                    raise RepoError('EPISODE_EXPOSED_SOURCE')
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_control'").fetchone():
            row=c.execute("SELECT data FROM memory_control WHERE id='restore_fence'").fetchone()
            if row:raise RepoError('REVOCATION_SYNC_REQUIRED')

    @classmethod
    def _protected(cls, value):
        if isinstance(value,dict):
            return bool(cls.FORBIDDEN_KEYS & value.keys()) or any(cls._protected(v) for v in value.values())
        return isinstance(value,list) and any(cls._protected(v) for v in value)

    def _deny(self, c, key, run_id, end, roots, reason, failure_class='UNKNOWN'):
        self.store.put('l1_triage_audit',key,{'run_id':run_id,'through':end,
            'priority':'DENY','reason':reason,'failure_class':failure_class,'derived_from':roots},c,immutable=True)
        self.store.put('l1_episode_cursor',key,{'episode_ids':[],'through':end},c,immutable=True)
        return []

    def capture_run(self, run_id, *, through=None, c=None, pipeline=None, stage=None, enqueue=True):
        """History inspection, or materialization at one verified stage boundary."""
        if c is None:
            with self.store.tx() as conn:return self.capture_run(run_id,through=through,c=conn,pipeline=pipeline,stage=stage,enqueue=enqueue)
        if pipeline=='causal_v2' or (pipeline is None and self.store.get('operator_config','curator_evidence_pipeline',c,default={}).get('enabled')):
            original=self.capture_run(run_id,through=through,c=c,pipeline='legacy',stage=stage,enqueue=stage is None and enqueue)
            return [self.capture_causal(e,c) for e in original]
        row=c.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row:return []
        run={**dict(row),'data':json.loads(row['data'])}
        rows=c.execute('SELECT * FROM events WHERE run_id=? AND (? IS NULL OR sequence<=?) ORDER BY sequence',
                       (run_id,through,through)).fetchall()
        events=[{**dict(e),'data':json.loads(e['data'])} for e in rows]
        triggers=[e for e in events if e['type'] in self.TRIGGERS and (through is None or e['sequence']<=through)]
        if stage is not None:
            from .verified_stages import VerifiedStages
            triggers=[e for e in events if e['sequence']==through==stage['sequence']
                      and e['type']==VerifiedStages.EVENT and e['event_id']==stage['event_id']
                      and e['visibility']=='DEV' and digest(e['data'])==stage['event_digest']]
            if not triggers:raise RepoError('L1_STAGE_SOURCE_CHANGED')
        if not triggers:return []
        trigger=triggers[-1];end=trigger['sequence']
        cursor_key=digest([run_id,end])
        prior=self.store.get('l1_episode_cursor',cursor_key,c)
        if prior:
            try:self._authorized(prior['episode_ids'],c)
            except RepoError as exc:
                self.store.put('l1_episode_block',cursor_key,{'episode_ids':prior['episode_ids'],
                    'reason':exc.code,'derived_from':prior['episode_ids']},c)
                return []
            return prior['episode_ids']
        events=[e for e in events if e['sequence']<=end]
        manifest=run['data']['manifest']
        roots=[run_id,run['task_id'],manifest['workspace_baseline_digest'],
               manifest.get('knowledge_snapshot_digest')]
        roots=[r for r in roots if r]
        if stage is not None:
            roots.extend([stage['id'],stage['source_digest'],*stage['refs']])
            for pair in stage.get('local_repair_pairs',[]):roots.extend([pair['before'],pair['after']])
        denial=None
        if run['data']['visibility']!='DEV' or any(e['visibility']!='DEV' for e in events):
            denial='NON_DEV_TRACE'
        elif any(self._protected(e['data']) for e in events):
            denial='PROTECTED_EVALUATION_CONTENT'
        try:self._authorized(roots,c)
        except RepoError as exc:denial=exc.code
        reason=run['data'].get('end_reason','') if stage is None else ''
        if stage is not None:
            # Carry only the Host-linked original failed check into the repair stage.
            prior_failed={p['failed'] for p in stage.get('local_repair_pairs',[])}
            events=[e for e in events if e['sequence']>stage['after'] or
                    (e['type']=='tool.operation_finished' and e['data'].get('operation_id') in prior_failed)]
        failure_class='UNKNOWN'
        if str(reason).startswith(('BLOCKED_ENV','SANDBOX_UNAVAILABLE','BASE_COMMIT_CHECK_FAILED')):
            failure_class='ENVIRONMENT';denial='KNOWN_ENVIRONMENT_FAILURE'
        if str(reason).startswith(('ISOLATION_REQUIRED','INTERNAL_ERROR','RECOVERY_REQUIRED')):
            failure_class='HOST';denial='KNOWN_HOST_FAILURE'
        if str(reason).startswith(('USAGE_UNSETTLED','RETRY_PROOF_REQUIRED','USAGE_UNKNOWN','PROVIDER_ERROR',
                                   'PROVIDER_TIMEOUT','server_is_overloaded','API_KEY_MISSING','CHATGPT_AUTH')):
            failure_class='EVALUATION';denial='KNOWN_EVALUATION_FAILURE'
        if denial:
            return self._deny(c,cursor_key,run_id,end,roots,denial,failure_class)
        baseline=manifest['workspace_baseline_digest'];latest=baseline
        writes=[];failures=[];recoveries=[];operations=[];artifacts=set();checks={}
        correction=trigger['type']=='control.admitted' and trigger['data'].get('kind')=='STEER'
        for event in events:
            if event['type']!='tool.operation_finished':continue
            data=event['data'];body=data.get('result') or {};opid=data.get('operation_id')
            if opid:operations.append(opid)
            op=c.execute('SELECT request,result FROM operations WHERE id=? AND run_id=?',(opid,run_id)).fetchone() if opid else None
            args=json.loads(op['request'])['arguments'] if op else {}
            if op:
                raw=json.loads(op['result'])
                if raw.get('truncated') and (raw.get('result_artifact') or raw.get('artifact')):
                    # Read only a run-owned immutable result, never grading files.
                    ref=raw.get('result_artifact') or raw['artifact']
                    if not self.store.get('run_artifact',digest([run_id,ref]),c):
                        return self._deny(c,cursor_key,run_id,end,roots,'EVIDENCE_ARTIFACT_DENIED')
                    try:body=self.store.read_json(ref)
                    except (RepoError,FileNotFoundError,ValueError):
                        return self._deny(c,cursor_key,run_id,end,roots,'INVALID_SOURCE_ARTIFACT')
                    artifacts.add(ref)
            if self._protected(body):
                return self._deny(c,cursor_key,run_id,end,roots,'PROTECTED_EVALUATION_CONTENT')
            output=body.get('output_snapshot')
            if output:
                if output!=latest:writes.append(event['sequence'])
                latest=output
            elif body.get('changed_paths') and not body.get('is_error'):
                writes.append(event['sequence']);latest='changed'
            for field in ('result_artifact','artifact'):
                if body.get(field):artifacts.add(body[field])
            proof=(body.get('execution_evidence') or {}).get('data') or {}
            for field in ('stdin_ref','stdout_ref','stderr_ref'):
                if proof.get(field):artifacts.add(proof[field])
            if data.get('tool_name')=='process.run':
                # Outcomes are observed facts, not a root-cause attribution.
                key=digest(['probe',body['checked_probe']] if body.get('checked_probe') else [args.get('argv'),args.get('stdin','')])
                if body.get('exit_code') not in (None,0) and not any(body.get(k) for k in ('timeout','cancelled','running')):
                    failures.append(event['sequence']);checks[key]=event['sequence']
                elif body.get('exit_code')==0 and key in checks and not any(body.get(k) for k in ('timeout','cancelled','running')):
                    # Require known Host check evidence, not a generic zero exit.
                    from .commands import command_evidence
                    evidence=command_evidence(args.get('argv',[]),body,args.get('stdin',''),strict=True,store=self.store)
                    if evidence and evidence['verification'] and evidence['outcome']=='PASSED':
                        recoveries.append((checks.pop(key),event['sequence']))
        # Validate artifacts before publishing any derived object.
        try:
            for ref in artifacts:self.store.read_blob(ref)
            self._authorized([*roots,*artifacts,*[e['event_id'] for e in events]],c)
        except (RepoError,FileNotFoundError) as exc:
            return self._deny(c,cursor_key,run_id,end,roots,
                exc.code if isinstance(exc,RepoError) else 'EPISODE_SOURCE_MISSING')
        used=run['data']['budget_used'] if stage is None else stage['budget_used']
        trace={'run_id':run_id,'task_id':run['task_id'],'task_revision':manifest['task_spec_revision'],
               'release_digest':manifest['release_digest'],'contract_digest':manifest['contract_digest'],
               'environment_digest':manifest['environment_digest'],'visibility':'DEV','scope':manifest['scope'],
               'event_range':[events[0]['sequence'],end],
               'event_refs':[{'id':e['event_id'],'sequence':e['sequence'],'digest':digest(e)} for e in events],
               'operation_ids':operations,'artifact_refs':sorted(artifacts),
               'trace_digest':digest(events),'usage_summary':used,
               'derived_from':roots}
        trace_id=digest(trace);trace['id']=trace_id
        self.store.put('trace_manifest',trace_id,trace,c,immutable=True)
        for parent in [*roots,*[e['event_id'] for e in events],*artifacts]:
            c.execute('INSERT OR IGNORE INTO lineage VALUES(?,?)',(trace_id,parent))
        high=(correction or bool(recoveries) or bool(failures) or
              (run['state'] in {'PARTIAL','BLOCKED','FAILED'} and latest!=baseline)
              or reason in {'NO_PROGRESS','STAGNATION_AFTER_WRITE'})
        medium=not high and (len(writes)>=3 or used.get('model_calls',0)>=30 or used.get('tool_calls',0)>=80)
        priority='HIGH' if high else 'MEDIUM' if medium or stage is not None else 'LOW'
        episode_ids=[]
        if high or medium or stage is not None:
            start=min([*writes,*failures] or [events[0]['sequence']])
            selected=[e for e in events if start<=e['sequence']<=end]
            episode_id=digest([run_id,start,end,trigger['event_id']])
            episode={'episode_id':episode_id,'run_id':run_id,'task_id':run['task_id'],
                'task_revision':manifest['task_spec_revision'],'trace_manifest_ref':trace_id,
                'release_digest':manifest['release_digest'],'contract_digest':manifest['contract_digest'],
                'environment_digest':manifest['environment_digest'],'scope':manifest['scope'],'visibility':'DEV',
                'task_family':manifest.get('task_family','UNSPECIFIED'),'source_version':latest,
                'stage_type':'CORRECTION' if correction else 'RECOVERY' if recoveries else 'VERIFY' if failures else 'DEBUG',
                'event_range':[start,end],'operation_ids':operations,
                'support_refs':[e['event_id'] for e in selected],'counter_refs':[],
                'expected':'Registered task/check behavior; no inferred oracle',
                'actual':{'terminal_state':run['state'],'failed_checks':len(failures),'recoveries':len(recoveries)},
                'failure_class':failure_class,'root_cause_hypotheses':[],
                'outcome':'RESOLVED' if recoveries or run['state']=='SUCCEEDED' else 'PARTIAL' if run['state']=='PARTIAL' else 'UNKNOWN',
                'epistemic_status':'observed','status':'CANDIDATE','priority':priority,
                'cost':used,'derived_from':[trace_id,*[e['event_id'] for e in selected]]}
            if stage is not None:
                episode.update(stage_candidate_id=stage['id'],source_version=stage['source_digest'],
                    actual={**episode['actual'],'terminal_state':None,'run_state_at_check':stage['run_state'],
                            'verified_stage':stage['outcome'],'verification_kind':stage['kind']},
                    stage_type='RECOVERY' if recoveries else 'VERIFY',
                    outcome=('RESOLVED' if stage['outcome'] in {'DELIVERY_VERIFIED','NEW_CHECK_PASSED'}
                             else 'PARTIAL'))
            self.store.put('l1_episode',episode_id,episode,c,immutable=True)
            for parent in episode['derived_from']:
                c.execute('INSERT OR IGNORE INTO lineage VALUES(?,?)',(episode_id,parent))
            problem_id=digest(['episode-problem',episode_id])
            pattern_id=digest(['stage-pattern',manifest['scope'],episode['stage_type']])
            problem={'id':problem_id,'run_id':run_id,'episode_ref':episode_id,
                'sequence':end,'event_id':trigger['event_id'],'symptom':'stage_learning_signal','evidence':episode['support_refs']}
            self.store.put('problem',problem_id,problem,c,immutable=True)
            pattern=self.store.get('pattern',pattern_id,c,default={'id':pattern_id,'symptom':'stage_learning_signal','support':[],'episode_refs':[],'status':'CANDIDATE'})
            if problem_id not in pattern['support']:pattern['support'].append(problem_id)
            if episode_id not in pattern['episode_refs']:pattern['episode_refs'].append(episode_id)
            self.store.put('pattern',pattern_id,pattern,c)
            job_id=digest(['episode-curator-job',episode_id])
            # Separate kind: the existing L1 worker must not consume a Curator job.
            if enqueue:
                c.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?)',
                          (job_id,'L1_CURATE',episode_id,'QUEUED',json.dumps({'episode_id':episode_id,'max_model_calls':2,'default_model_calls':1})))
            episode_ids.append(episode_id)
        self.store.put('l1_triage_audit',cursor_key,{'run_id':run_id,'through':end,'priority':priority,
            'trace_manifest_ref':trace_id,'episode_ids':episode_ids,'derived_from':[trace_id]},c,immutable=True)
        self.store.put('l1_episode_cursor',cursor_key,{'through':end,'episode_ids':episode_ids},c,immutable=True)
        return episode_ids

    def capture_causal(self, episode_id, c=None):
        """Append a versioned causal Episode; never rewrite a legacy cursor/object."""
        if c is None:
            with self.store.tx() as conn:return self.capture_causal(episode_id,conn)
        old=self.get(episode_id,c)
        if old.get('selection_version')=='episode-v2':return episode_id
        trace=self.store.get('trace_manifest',old['trace_manifest_ref'],c)
        events=[]
        for ref in trace['event_refs']:
            row=c.execute('SELECT * FROM events WHERE event_id=?',(ref['id'],)).fetchone()
            events.append({**dict(row),'data':json.loads(row['data'])})
        from .memory.evidence_pipeline import causal_segments
        segments,refs,links=causal_segments(self.store,events,old['run_id'],c)
        # A failed task without a source-linked repair still remains inspectable.
        if not refs:
            refs=old['support_refs'];segments={'UNCLASSIFIED':refs}
        self._authorized([episode_id,*refs],c)
        selected=[e for e in events if e['event_id'] in set(refs)]
        source_digest=digest(selected)
        new_id=digest([trace['id'],'episode-v2',source_digest,links['rule']])
        value={**old,'episode_id':new_id,'legacy_episode_ref':episode_id,
            'core_event_range':old['event_range'],'event_range':[selected[0]['sequence'],selected[-1]['sequence']],
            'segments':segments,'source_event_ids':refs,'support_refs':refs,'source_digest':source_digest,
            'selection_version':'episode-v2','causal_links':links,
            'verification_incomplete':old['outcome']!='RESOLVED' if old.get('stage_candidate_id')
                else not bool(old['actual']['recoveries'] or old['outcome']=='RESOLVED'),
            'derived_from':[episode_id,trace['id'],*refs]}
        value['episode_digest']=digest(value)
        self.store.put('l1_episode',new_id,value,c,immutable=True)
        for ref in value['derived_from']:c.execute('INSERT OR IGNORE INTO lineage VALUES(?,?)',(new_id,ref))
        job_id=digest(['episode-curator-job',new_id])
        c.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?)',(job_id,'L1_CURATE',new_id,'QUEUED',
            json.dumps({'episode_id':new_id,'max_model_calls':3,'default_model_calls':1,'pipeline':'causal_v2'})))
        return new_id

    def get(self, episode_id, c=None):
        if c is None:
            with self.store.tx() as conn:return self.get(episode_id,conn)
        episode=self.store.get('l1_episode',episode_id,c)
        if not episode:raise RepoError('EPISODE_NOT_FOUND')
        if episode.get('selection_version')=='episode-v2' and digest({k:v for k,v in episode.items() if k!='episode_digest'})!=episode.get('episode_digest'):
            raise RepoError('EPISODE_SOURCE_CHANGED')
        self._authorized([episode_id],c)
        trace=self.store.get('trace_manifest',episode['trace_manifest_ref'],c)
        if not trace or digest({k:v for k,v in trace.items() if k!='id'})!=trace['id']:
            raise RepoError('TRACE_MANIFEST_CORRUPT')
        events=[]
        for ref in trace['event_refs']:
            row=c.execute("SELECT * FROM events WHERE event_id=? AND run_id=? AND visibility='DEV'",(ref['id'],episode['run_id'])).fetchone()
            if not row:raise RepoError('EPISODE_SOURCE_MISSING')
            event={**dict(row),'data':json.loads(row['data'])}
            if digest(event)!=ref['digest']:raise RepoError('EPISODE_SOURCE_CHANGED')
            events.append(event)
        for ref in trace['artifact_refs']:
            try:self.store.read_blob(ref)
            except FileNotFoundError:raise RepoError('EPISODE_SOURCE_MISSING') from None
        if digest(events)!=trace['trace_digest']:raise RepoError('EPISODE_SOURCE_CHANGED')
        return episode

    def evidence(self, episode_id):
        with self.store.tx() as c:
            episode=self.get(episode_id,c)
            events=[]
            for ref in episode['support_refs']:
                row=c.execute("SELECT * FROM events WHERE event_id=? AND visibility='DEV'",(ref,)).fetchone()
                events.append({**dict(row),'data':json.loads(row['data'])})
        return {'episode':episode,'events':events}
