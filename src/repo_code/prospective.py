"""Host-owned prospective certification queue. No tasks ship with the package.

Collect future sealed cohorts before evaluating; ordinary reuse feedback is not
certification evidence. Existing SuiteEvaluator owns every Release gate.
"""
from .models import Candidate, Budget
from .providers import model_configuration
from .util import RepoError, digest, now


class ProspectiveCertification:
    ROLES=('dev','promotion','final')

    def __init__(self,service,evaluator):
        self.service=service;self.store=service.store;self.evaluator=evaluator
        if evaluator.service is not service:raise RepoError('CERTIFICATION_HOST_MISMATCH')

    def _identity(self,candidate_id):
        from pathlib import Path
        candidate=Candidate.model_validate(self.store.get('candidate',candidate_id))
        self.service.releases.get(candidate.candidate_release_digest)
        package=Path(__file__).parent
        return {'candidate':candidate.model_dump(mode='json'),
                'model':model_configuration(self.service.provider),
                'host_build':digest({p.relative_to(package).as_posix():digest(p.read_bytes()) for p in sorted(package.rglob('*.py'))}),
                'memory_profile':self.service.memory.profile_digest if self.service.memory else None,
                'environment':self.service.sandbox.environment_digest,
                'policy':self.service.releases.governor.digest,
                'runner':{'context_ceiling':self.evaluator.runner.context_ceiling,
                          'legacy_window':self.evaluator.runner.legacy_window,
                          'review_contract_tasks':self.evaluator.runner.review_contract_tasks}}

    def freeze(self,candidate_id,*,budget,counts=None):
        counts=counts if counts is not None else {'dev':2,'promotion':3,'final':2}
        if (set(counts)!=set(self.ROLES) or
                any(type(v) is not int or not 1<=v<=16 for v in counts.values())):
            raise RepoError('CERTIFICATION_COUNTS_INVALID')
        identity=self._identity(candidate_id)
        if Budget.model_validate(budget).model_calls is None:raise RepoError('CERTIFICATION_FINITE_BUDGET_REQUIRED')
        policy={'identity':identity,'counts':counts,'budget':Budget.model_validate(budget).model_dump(mode='json'),
                'protocol':'prospective-sealed-cohorts-v1'}
        key=digest([candidate_id,'prospective-v1'])
        with self.store.tx() as c:
            old=self.store.get('certification_plan',key,c)
            if old:
                if old['policy']!=policy:raise RepoError('CERTIFICATION_ALREADY_FROZEN')
                return key
            if self.store.get('candidate_freeze',candidate_id,c) or self.store.get('gate',digest([candidate_id,'G0']),c):raise RepoError('CERTIFICATION_ALREADY_EVALUATED')
            self.store.put('certification_plan',key,{'policy':policy,'frozen_at':now(),
                'signature':self.store.sign(policy)},c,immutable=True)
            self.store.put('certification_state',key,{'status':'WAITING_EVIDENCE','cohorts':[],
                'counts':dict.fromkeys(self.ROLES,0)},c)
        return key

    def _plan(self,key,c=None):
        row=self.store.get('certification_plan',key,c)
        if not row or not self.store.verify(row['policy'],row['signature']):raise RepoError('CERTIFICATION_PLAN_INVALID')
        if self._identity(row['policy']['identity']['candidate']['id'])!=row['policy']['identity']:
            raise RepoError('CERTIFICATION_CONFIGURATION_CHANGED')
        if self.service.releases.active()['digest']!=row['policy']['identity']['candidate']['base_release_digest']:
            raise RepoError('CERTIFICATION_BASE_CHANGED')
        return row

    def nominate(self,key,cohort):
        """Trusted evaluator seals future tasks BEFORE any candidate evaluation.

        Metadata alone cannot establish independence: source groups must be
        assigned by the trusted evaluator. Vault independently checks exposure.
        """
        plan=self._plan(key)
        meta=self.store.get('cohort',cohort)
        if not meta or meta.get('provenance_version')!=2 or meta['role'] not in self.ROLES:
            raise RepoError('CERTIFICATION_PROVENANCE_REQUIRED')
        data=self.evaluator.vault.open(cohort,self.evaluator.cap)
        if any(Budget.model_validate(t.get('budget',{})).model_dump(mode='json')!=plan['policy']['budget'] for t in data['tasks']):
            raise RepoError('CERTIFICATION_TASK_BUDGET_MISMATCH')
        role=meta['role'];n=len(data['tasks'])
        with self.store.tx() as c:
            state=self.store.get('certification_state',key,c)
            if cohort in state['cohorts']:return self.status(key,c)
            if state['status']!='WAITING_EVIDENCE':raise RepoError('CERTIFICATION_COLLECTION_CLOSED')
            if state['counts'][role]+n>plan['policy']['counts'][role]:raise RepoError('CERTIFICATION_QUOTA_EXCEEDED')
            for previous in state['cohorts']:
                other=self.store.get('cohort',previous,c)
                if meta['applicability_family']!=other['applicability_family']:raise RepoError('CERTIFICATION_APPLICABILITY_MISMATCH')
                if bool(meta['synthetic_integration'])!=bool(other['synthetic_integration']):raise RepoError('CERTIFICATION_EVIDENCE_CLASS_MISMATCH')
                if set(meta['task_fingerprints'])&set(other['task_fingerprints']):raise RepoError('CERTIFICATION_DUPLICATE_TASK')
                if role!=other['role'] and set(meta['contamination_groups'])&set(other['contamination_groups']):
                    raise RepoError('CERTIFICATION_SOURCE_OVERLAP')
            state['cohorts'].append(cohort);state['counts'][role]+=n
            if state['counts']==plan['policy']['counts']:state['status']='READY_FOR_GATES'
            self.store.put('certification_state',key,state,c)
            self.store.put('certification_nomination',digest([key,cohort]),
                {'plan':key,'cohort':cohort,'at':now(),'meta_digest':digest(meta)},c,immutable=True)
            return self.status(key,c)

    def observe(self,key,run_id):
        """Persist an actual local outcome; intentionally never advance quotas."""
        self._plan(key)
        run=self.service.runtime.run(run_id)
        from .models import TERMINAL
        if run['state'] not in TERMINAL:raise RepoError('CERTIFICATION_OBSERVATION_NOT_TERMINAL')
        value={'plan':key,'run_id':run_id,'state':run['state'],
               'manifest':digest(run['data']['manifest']),'evidence_class':'OBSERVATIONAL_ONLY',
               'counts_for_gates':False}
        self.store.put('certification_observation',digest([key,run_id]),value,immutable=True)
        return value

    def status(self,key,c=None):
        plan=self.store.get('certification_plan',key,c)
        state=self.store.get('certification_state',key,c)
        if not plan or not state:raise RepoError('CERTIFICATION_NOT_FOUND')
        return {**state,'missing':{r:plan['policy']['counts'][r]-state['counts'][r] for r in self.ROLES},
                'published':False,'normal_tasks_blocked':False}

    async def evaluate(self,key):
        from .util import execution_lock
        # One owner across processes; crashes retain RUNNING, never replay calls.
        with execution_lock(self.store.root/'executor_locks'/('cert-'+key[7:]+'.lock')):
            plan=self._plan(key)
            state=self.status(key)
            if state['status'] in {'COMPLETED','REJECTED','INTERRUPTED'}:return state
            if state['status']=='RUNNING':
                state.update(status='INTERRUPTED',reason='Previous evaluator stopped; preserve all attempts, do not rerun automatically')
                self.store.put('certification_state',key,state)
                return self.status(key)
            if state['status']!='READY_FOR_GATES':return state
            candidate=plan['policy']['identity']['candidate']
            skills=self.service.releases.components(candidate['candidate_release_digest'])['skills_index'].get('skills',[])
            if any(isinstance(s,dict) and s.get('experience_id') for s in skills):
                qualification=self.store.get('skill_qualification',candidate['id'])
                if not qualification or not self.store.verify(qualification['payload'],qualification['signature']):
                    return {**state,'next':'WAITING_SKILL_QUALIFICATION','reason':'Existing S1/S2/S3 validation required before Release gates'}
                p=qualification['payload']
                if (p['candidate']!=candidate['id'] or p['release']!=candidate['candidate_release_digest']
                        or p['evidence_class']!='MODEL_TASK_EVALUATION'):
                    raise RepoError('CERTIFICATION_SKILL_BINDING')
                for stage in ('s1','s2','s3'):
                    report=self.store.get('skill_'+stage,digest([p['contract_ref'],candidate['id']]))
                    if not report or not report['passed'] or digest(report)!=p[stage]:raise RepoError('CERTIFICATION_SKILL_STALE')
            runner=self.evaluator.runner
            if not runner.isolated_states or runner.trial_root is None:raise RepoError('CERTIFICATION_ISOLATED_STATES_REQUIRED')
            groups={r:[] for r in self.ROLES}
            for cohort in state['cohorts']:
                nomination=self.store.get('certification_nomination',digest([key,cohort]))
                if nomination['meta_digest']!=digest(self.store.get('cohort',cohort)):raise RepoError('CERTIFICATION_COHORT_CHANGED')
                data=self.evaluator.vault.open(cohort,self.evaluator.cap)
                groups[data['role']].append(data)
            merged={}
            for role,cohorts in groups.items():
                tasks=[t for co in cohorts for t in co['tasks']]
                if len({t['id'] for t in tasks})!=len(tasks):raise RepoError('CERTIFICATION_DUPLICATE_TASK_ID')
                merged[role]=self.evaluator.vault.register(key+'-'+role,role,tasks,
                    synthetic=any(co['synthetic_integration'] for co in cohorts),
                    applicability_family=cohorts[0]['applicability_family'],
                    contamination_groups=sorted({g for co in cohorts for g in co['contamination_groups']}))
            state.update(status='RUNNING',started=now(),merged_cohorts=merged)
            self.store.put('certification_state',key,state)
            factory=runner.provider_factory
            def pinned(public):
                provider=factory(public)
                if model_configuration(provider)!=plan['policy']['identity']['model']:
                    raise RepoError('CERTIFICATION_TRIAL_MODEL_CHANGED')
                return provider
            runner.provider_factory=pinned
            try:
                result=await self.evaluator.qualify(plan['policy']['identity']['candidate']['id'],
                    merged['dev'],merged['promotion'],merged['final'])
                state.update(status='COMPLETED' if result['status']=='PASS' else 'REJECTED',result=result)
            except BaseException as exc:
                state.update(status='INTERRUPTED',reason=getattr(exc,'code',type(exc).__name__))
                self.store.put('certification_state',key,state)
                raise
            finally:runner.provider_factory=factory
            self.store.put('certification_state',key,state)
            return self.status(key)
