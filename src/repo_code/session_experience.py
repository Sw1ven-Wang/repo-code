"""Host-owned, expiring advisory reuse. Never a Release activation authority."""
from .util import RepoError, digest, now, uid


class SessionExperiences:
    def __init__(self,runtime):self.runtime=runtime;self.store=runtime.store

    def create_collection(self,scope,name):
        """Durable multi-workspace learning, explicitly scoped by the trusted Host.

        Use the same state-root for successive tasks. This grants neither model
        calls nor publication; existing Episode/Curator and admission gates apply.
        """
        if any(not isinstance(v,str) or not v.strip() or len(v)>512 for v in (scope,name)):
            raise RepoError('COLLECTION_SCOPE_OR_NAME')
        key='collection-'+digest([scope,name])[7:]
        with self.store.tx() as c:
            if not self.store.get('experience_session',key,c):
                self.store.put('experience_session',key,{'id':key,'scope':scope,'name':name,
                    'kind':'MULTITASK_COLLECTION','expires':None,'bound_tasks':0,
                    'status':'OPEN','generation':0},c,immutable=True)
        self.get(key) # Re-creation cannot reopen a closed authorization.
        return key

    def accumulation(self,key):
        """Inspect native cross-task evidence, not a new raw-trace learning DB.

        Capture is idempotent and also recovers an interrupted terminal hook.
        Raw tool errors are observations, not inferred learning opportunities.
        No candidate is auto-admitted and no model is called here.
        """
        from .episodes import EpisodeBuilder
        from .models import TERMINAL
        session=self.get(key)
        if session.get('kind')!='MULTITASK_COLLECTION':raise RepoError('NOT_MULTITASK_COLLECTION')
        runs=[];episodes={};filtered=[];error_count=0
        builder=EpisodeBuilder(self.store)
        for binding in self.store.values('session_run'):
            p=binding['payload']
            if p.get('session')!=key or not self.store.verify(p,binding['signature']):continue
            run=self.runtime.run(p['run_id']);m=run['data']['manifest']
            if run['data']['visibility']!='DEV' or p['scope']!=session['scope'] or m['scope']!=session['scope']:continue
            runs.append(run)
            if run['state'] in TERMINAL:
                builder.capture_run(run['id'])
            for episode in self.store.values('l1_episode'):
                if episode['run_id']!=run['id']:continue
                try:episodes[episode['episode_id']]=builder.get(episode['episode_id'])
                except RepoError as exc:filtered.append({'episode_id':episode['episode_id'],'reason':exc.code})
            # Only count run-owned tool outcomes; never read private graders.
            import json
            with self.store.connect() as c:
                for row in c.execute('SELECT result FROM operations WHERE run_id=? AND result IS NOT NULL',(run['id'],)):
                    if json.loads(row[0]).get('is_error'):error_count+=1
        members={r['id'] for r in runs};patterns=[]
        for pattern in self.store.values('pattern'):
            support=[]
            for pid in pattern.get('support',[]):
                problem=self.store.get('problem',pid)
                if (problem and problem['run_id'] in members
                        and problem.get('episode_ref') in episodes):support.append(problem)
            if support:
                patterns.append({'pattern_id':pattern['id'],'problem_ids':[p['id'] for p in support],
                    'episode_ids':sorted({p['episode_ref'] for p in support}),
                    'distinct_tasks':len({episodes[p['episode_ref']]['task_id'] for p in support}),
                    'classification':'NATIVE_STAGE_GROUP_NOT_PROVEN_COMMON_ROOT_CAUSE'})
        value={'collection_id':key,'scope':session['scope'],'run_ids':sorted(members),
            'distinct_tasks':len({r['task_id'] for r in runs}),
            'raw_tool_error_observations':error_count,'episodes':sorted(episodes),
            'patterns':patterns,'filtered':filtered,'automatic_activation':False}
        self.store.put('multitask_learning_index',key,value)
        return value

    def create_project(self,scope,workspace,*,max_tasks=32):
        """Persistent, explicitly bound local reuse; never a permanent Release."""
        from pathlib import Path
        root=Path(workspace).resolve(strict=True)
        if not root.is_dir() or not isinstance(scope,str) or not scope.strip() or type(max_tasks) is not int or not 1<=max_tasks<=128:
            raise RepoError('PROJECT_EXPERIENCE_POLICY_INVALID')
        key='project-'+digest([scope,str(root)])[7:]
        with self.store.tx() as c:
            old=self.store.get('experience_session',key,c)
            if old:
                if old['max_tasks']!=max_tasks:raise RepoError('PROJECT_EXPERIENCE_POLICY_CHANGED')
                return key # Never reset a closed or exhausted authorization.
            self.store.put('experience_session',key,{'id':key,'scope':scope,'expires':None,
                'kind':'PROJECT_CONDITIONAL','workspace':str(root),'max_tasks':max_tasks,
                'bound_tasks':0,'status':'OPEN','generation':0},c,immutable=True)
        return key

    def create(self,scope,*,ttl_seconds=86400):
        if not isinstance(scope,str) or not scope or not 0<ttl_seconds<=604800:
            raise RepoError('SESSION_SCOPE_OR_TTL')
        key=uid('session-')
        self.store.put('experience_session',key,{'id':key,'scope':scope,'expires':now()+ttl_seconds,
            'status':'OPEN','generation':0},immutable=True)
        return key

    def get(self,key,c=None):
        row=self.store.get('experience_session',key,c)
        if not row or row['status']!='OPEN' or (row['expires'] is not None and row['expires']<=now()):raise RepoError('SESSION_CLOSED_OR_EXPIRED')
        return row

    def bind(self,key,run_id,task_family):
        """Trusted embedding API; cannot be called by model tools or attach mid-run."""
        with self.store.tx() as c:
            session=self.get(key,c);run=self.runtime.run(run_id,c)
            if run['data']['visibility']!='DEV' or run['data']['manifest']['scope']!=session['scope']:
                raise RepoError('SESSION_SCOPE_DENIED')
            if not task_family or task_family in {'UNKNOWN','UNSPECIFIED'}:raise RepoError('SESSION_FAMILY_REQUIRED')
            if any(run['data'].get('budget_used',{}).get(k,0) for k in ('model_calls','tool_calls')):raise RepoError('SESSION_RUN_ALREADY_STARTED')
            if self.store.get('session_run',run_id,c):raise RepoError('SESSION_ALREADY_BOUND')
            if session.get('kind')=='PROJECT_CONDITIONAL':
                from pathlib import Path
                if Path(run['data']['inherited_source']).resolve()!=Path(session['workspace']):raise RepoError('PROJECT_WORKSPACE_MISMATCH')
                if session['bound_tasks']>=session['max_tasks']:raise RepoError('PROJECT_REVIEW_REQUIRED')
                self.store.put('experience_session',key,{**session,'bound_tasks':session['bound_tasks']+1},c)
            elif session.get('kind')=='MULTITASK_COLLECTION':
                self.store.put('experience_session',key,{**session,'bound_tasks':session['bound_tasks']+1},c)
            run['data']['manifest']['task_family']=task_family
            self.runtime.update(c,run,data=run['data'])
            payload={'session':key,'run_id':run_id,'scope':session['scope'],'family':task_family,
                'release':run['data']['manifest']['release_digest'],'environment':self.runtime.sandbox.environment_digest}
            self.store.put('session_run',run_id,{'payload':payload,'signature':self.store.sign(payload)},c,immutable=True)

    def admit(self,key,contract_ref,candidate_id):
        """Local support validation suffices for session reuse, not global release.

        S1 and actual-model S2 must have executed at this Host environment. No
        bundled dataset is required: the caller supplies its local support tasks.
        """
        with self.store.tx() as c:
            session=self.get(key,c)
            ref=digest([contract_ref,candidate_id])
            s1=self.store.get('skill_s1',ref,c);s2=self.store.get('skill_s2',ref,c)
            contract=self.store.get('capability_contract',contract_ref,c)
            if (not s1 or not s2 or not s1['passed'] or not s2['passed']
                    or s1.get('environment')!=self.runtime.sandbox.environment_digest
                    or s2.get('evidence_class')!='MODEL_TASK_EVALUATION'):
                raise RepoError('SESSION_LOCAL_VALIDATION_REQUIRED')
            if not contract or contract['scope']!=session['scope']:raise RepoError('SESSION_SCOPE_DENIED')
            from .models import Candidate
            candidate=Candidate.model_validate(self.store.get('candidate',candidate_id,c))
            release=self.store.get('release',candidate.candidate_release_digest,c)
            cfg=self.store.read_json(release['components']['skills_index'])
            spec=next(s for s in cfg['skills'] if s.get('experience_id')==contract['skill_id'])
            if spec['revision']!=contract['revision']:raise RepoError('SESSION_REVISION_MISMATCH')
            grant_id=digest([key,contract_ref,candidate_id])
            existing=self.store.get('session_grant',grant_id,c)
            if existing:return grant_id
            count=c.execute("SELECT count(*) FROM objects WHERE kind='session_grant' AND json_extract(data,'$.payload.session')=?",(key,)).fetchone()[0]
            if count>=16:raise RepoError('SESSION_GROWTH_LIMIT')
            payload={'session':key,'contract_ref':contract_ref,'candidate':candidate_id,'spec':spec,
                'environment':self.runtime.sandbox.environment_digest,'expires':session['expires'],
                's1_digest':digest(s1),'s2_digest':digest(s2),'authority':'PROJECT_CONDITIONAL_ADVISORY' if session.get('kind')=='PROJECT_CONDITIONAL' else 'SESSION_ADVISORY_ONLY'}
            self.store.put('session_grant',grant_id,{'payload':payload,'signature':self.store.sign(payload)},c,immutable=True)
            self.store.put('session_grant_state',grant_id,{'status':'ACTIVE'},c)
            self.store.put('experience_session',key,{**session,'generation':session['generation']+1},c)
            return grant_id

    def revoke(self,grant_id,reason):
        if not isinstance(reason,str) or not reason.strip():raise RepoError('SESSION_REASON_REQUIRED')
        with self.store.tx() as c:
            grant=self.store.get('session_grant',grant_id,c)
            if not grant:raise RepoError('SESSION_GRANT_MISSING')
            if (self.store.get('session_grant_state',grant_id,c) or {}).get('status')=='REVOKED':return
            key=grant['payload']['session'];session=self.store.get('experience_session',key,c)
            self.store.put('session_grant_state',grant_id,{'status':'REVOKED','reason':reason,'at':now()},c)
            self.store.put('experience_session',key,{**session,'generation':session['generation']+1},c)

    def close(self,key):
        with self.store.tx() as c:
            row=self.store.get('experience_session',key,c)
            if not row:raise RepoError('SESSION_MISSING')
            self.store.put('experience_session',key,{**row,'status':'CLOSED','generation':row['generation']+1},c)

    def entries(self,run_id,c=None):
        if c is None:
            from .memory.repository import MemoryRepository
            MemoryRepository(self.store)
            with self.store.tx() as db:return self.entries(run_id,db)
        binding=self.store.get('session_run',run_id,c)
        if not binding or not self.store.verify(binding['payload'],binding['signature']):return {}
        p=binding['payload'];run=self.runtime.run(run_id,c);m=run['data']['manifest']
        try:session=self.get(p['session'],c)
        except RepoError:return {}
        if (p['run_id']!=run_id or p['scope']!=m['scope'] or p['scope']!=session['scope']
                or p['family']!=m.get('task_family') or p['release']!=m['release_digest']
                or p['environment']!=self.runtime.sandbox.environment_digest or run['data']['visibility']!='DEV'):return {}
        entries={}
        for row in c.execute("SELECT id,data FROM objects WHERE kind='session_grant' ORDER BY rowid"):
            import json
            gid,raw=row;g=json.loads(raw);payload=g['payload']
            if payload['session']!=session['id'] or not self.store.verify(payload,g['signature']):continue
            if (self.store.get('session_grant_state',gid,c) or {}).get('status')!='ACTIVE':continue
            if (payload['expires'] is not None and payload['expires']<=now()) or payload['environment']!=p['environment']:continue
            ref=digest([payload['contract_ref'],payload['candidate']])
            if any(digest(self.store.get('skill_'+stage,ref,c))!=payload[stage+'_digest'] for stage in ['s1','s2']):continue
            spec=payload['spec']
            from .memory.repository import MemoryRepository
            repo=MemoryRepository(self.store)
            try:repo.ensure_dev([payload['contract_ref'],spec['revision_digest']],c)
            except RepoError:continue
            record=self.store.get('experience_revision',spec['revision_digest'],c)
            claim=repo.get('memory_versions',record['claim_ref'],c) if record else None
            if not claim or claim['validity'] in {'STALE','CONTRADICTED'} or claim['lifecycle']=='REVOKED':continue
            if c.execute("SELECT 1 FROM objects WHERE kind='experience_conflict' AND json_extract(data,'$.entity_id')=?",(spec['experience_id'],)).fetchone():continue
            entries[spec['experience_id']]={**spec,'session_grant_id':gid,'session_id':session['id'],'reuse_authority':payload['authority']}
        return entries

    def feedback(self,run_id,report):
        """Delivery outcome is application feedback, never proof of causation."""
        binding=self.store.get('session_run',run_id)
        if not binding:return
        for rid in report.get('application_receipts',[]):
            receipt=self.store.get('application_receipt',rid)
            if not receipt:continue
            route=self.store.get('capability_route',receipt['route_id']) or {}
            gid=route.get('session_grants',{}).get(receipt['skill_id'])
            if not gid:continue
            key=digest([run_id,gid,rid])
            value={'run_id':run_id,'grant_id':gid,'receipt':rid,'applied':receipt['applied'],
                'state':report['state'],'causal_benefit':'UNKNOWN','usage':report.get('usage')}
            self.store.put('session_feedback',key,value,immutable=True)
            if receipt['applied'] and report['state'] not in {'SUCCEEDED','WAITING_USER'}:
                self.revoke(gid,'Applied session procedure followed by unsuccessful delivery; requires review')
