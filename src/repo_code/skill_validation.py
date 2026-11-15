"""Skill S1/S2/S3 qualification layered above, never replacing, Release gates."""
import json
from pydantic import BaseModel,ConfigDict,Field,ValidationError
from typing import Literal
from .models import Contract,Candidate
from .capabilities import CapabilityContract
from .acceptance import validate_contract
from .util import RepoError,digest

class ProbeSpec(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    probe_id:str=Field(min_length=1)
    kind:Literal['POSITIVE','NEGATIVE','BOUNDARY']
    task_id:str
    check_id:str
    expected_behavior:str=Field(min_length=1)
    required:bool=True
    source_refs:list[str]=Field(min_length=1)
    environment_profile:str

class SkillValidator:
    def __init__(self,service,runner):
        self.service=service;self.store=service.store;self.runner=runner

    def register(self,spec,task):
        spec=ProbeSpec.model_validate(spec)
        contract=Contract.model_validate(task['contract']);validate_contract(contract,True)
        checks={c.id:c for c in contract.checks}
        if spec.task_id!=task['id'] or spec.check_id not in contract.required_checks:raise RepoError('PROBE_TASK_BINDING')
        check=checks[spec.check_id]
        if not check.active or check.kind in {'human','diagnostic'}:raise RepoError('PROBE_NOT_EXECUTABLE')
        from .memory.repository import MemoryRepository
        repo=MemoryRepository(self.store)
        value={'spec':spec.model_dump(),'task_digest':digest(task),'contract_digest':contract.digest,
            'check_digest':check.digest,'environment_profile':spec.environment_profile}
        with self.store.tx() as c:
            repo.ensure_dev(spec.source_refs,c)
            for ref in spec.source_refs:
                exists=c.execute('SELECT 1 FROM objects WHERE id=?',(ref,)).fetchone() or c.execute("SELECT 1 FROM events WHERE event_id=? AND visibility='DEV'",(ref,)).fetchone()
                if not exists:raise RepoError('PROBE_SOURCE_MISSING')
            self.store.put('skill_probe',spec.probe_id,value,c,immutable=True)
            repo.derive(spec.probe_id,spec.source_refs,c)
        return value

    def s1(self,contract_ref,candidate_id,tasks):
        from .memory.repository import MemoryRepository
        repo=MemoryRepository(self.store);reasons=[];semantic_reviews={}
        candidate=None
        try:
            contract=CapabilityContract.model_validate(self.store.get('capability_contract',contract_ref))
            candidate=Candidate.model_validate(self.store.get('candidate',candidate_id))
            from .memory.consolidation import Consolidator
            from .memory.models import MemoryProfile
            from .capabilities import CapabilityRouter
            owner=Consolidator(repo,self.service.runtime,MemoryProfile(mode='lexical_only'))
            canonical_ref,_=CapabilityRouter(owner).contract(contract.skill_id,contract.revision,
                contract.step_tools,contract.step_rules,contract.required_probe_ids,contract.binding_slots)
            if canonical_ref!=contract_ref:raise RepoError('SKILL_CONTRACT_SOURCE_BINDING')
            experience=CapabilityRouter(owner).experiences.get(contract.skill_id,contract.revision)
            for support in experience['support']:
                lesson=self.store.get('lesson',support['lesson_id'])
                if contract.binding_slots and lesson.get('semantic_review'):
                    portable=lesson.get('portability',{})
                    if portable.get('binding_slots')!=contract.binding_slots or portable.get('step_rules')!=contract.step_rules:
                        raise RepoError('SKILL_PORTABLE_REVIEW_BINDING')
                if lesson.get('semantic_review'):
                    review=self.store.get('claim_semantic_review',lesson['id'])
                    if not review or not self.store.verify(review.get('payload',{}),review.get('signature','')):raise RepoError('SKILL_SEMANTIC_REVIEW_REQUIRED')
                    payload=review['payload']
                    if payload.get('lesson_id')!=lesson['id'] or payload.get('lesson_digest')!=digest(lesson) or payload.get('decision')!='APPROVED':raise RepoError('SKILL_SEMANTIC_REVIEW_REQUIRED')
                    semantic_reviews[lesson['id']]=digest(review)
            repo.ensure_dev([contract_ref,*contract.source_episode_refs])
            if not contract.required_probe_ids:raise RepoError('SKILL_PROBES_REQUIRED')
            if any(f in {'UNKNOWN','UNSPECIFIED',''} for f in contract.task_families):raise RepoError('SKILL_FAMILY_UNKNOWN')
            if not candidate.targets or not set(candidate.targets)<={'skills_index','planning_policy'}:raise RepoError('SKILL_TARGET_DENIED')
            old=self.service.releases.get(candidate.base_release_digest);new=self.service.releases.get(candidate.candidate_release_digest)
            changed={k for k in old.components if old.components[k]!=new.components[k]}
            if changed!=set(candidate.targets) or not changed<={'skills_index','planning_policy'}:raise RepoError('SKILL_RELEASE_BINDING')
            cfg=self.store.read_json(new.components['skills_index'])
            entry=next((e for e in cfg.get('skills',[]) if isinstance(e,dict) and e.get('experience_id')==contract.skill_id),None)
            if not entry or entry.get('revision')!=contract.revision or entry.get('scope')!=contract.scope or entry.get('required_probe_ids')!=contract.required_probe_ids:raise RepoError('SKILL_CONTRACT_RELEASE_BINDING')
            version=self.store.get('experience_version',digest([contract.skill_id,contract.revision]))
            if not version or entry.get('revision_digest')!=version['digest'] or entry.get('step_rules',[])!=contract.step_rules:raise RepoError('SKILL_CONTRACT_RELEASE_BINDING')
            if entry.get('binding_slots',{})!=contract.binding_slots:raise RepoError('SKILL_CONTRACT_RELEASE_BINDING')
            if len([e for e in cfg.get('skills',[]) if isinstance(e,dict) and e.get('experience_id')==contract.skill_id])!=1:raise RepoError('SKILL_DUPLICATE_ENTRY')
            old_cfg=self.store.read_json(old.components['skills_index'])
            retained=lambda v:[e for e in v.get('skills',[]) if not isinstance(e,dict) or e.get('experience_id')!=contract.skill_id]
            if retained(old_cfg)!=retained(cfg):raise RepoError('SKILL_MULTIPLE_CHANGES')
            self.service.releases.governor.check(self.service.releases.components(candidate.candidate_release_digest),candidate.operation,candidate.growth_justification)
            kinds=set()
            for pid in contract.required_probe_ids:
                row=self.store.get('skill_probe',pid)
                if not row:raise RepoError('SKILL_PROBE_UNREGISTERED')
                spec=ProbeSpec.model_validate(row['spec']);task=tasks.get(spec.task_id)
                if not spec.required:raise RepoError('SKILL_REQUIRED_PROBE_OPTIONAL')
                if task is None or digest(task)!=row['task_digest']:raise RepoError('SKILL_PROBE_SOURCE_CHANGED')
                if spec.environment_profile!=self.service.sandbox.environment_digest:raise RepoError('SKILL_PROBE_ENVIRONMENT')
                repo.ensure_dev([pid]);kinds.add(spec.kind)
            if not {'POSITIVE','NEGATIVE','BOUNDARY'}<=kinds:raise RepoError('SKILL_PROBE_COVERAGE')
            if not self.runner.isolated_states:raise RepoError('SKILL_ISOLATED_STATES_REQUIRED')
        except RepoError as exc:reasons.append(exc.code)
        except (ValidationError,TypeError,KeyError):reasons.append('SKILL_SCHEMA_INVALID')
        report={'stage':'S1','passed':not reasons,'reasons':reasons,'contract_ref':contract_ref,
            'candidate':candidate_id,'release':candidate.candidate_release_digest if candidate else None,'environment':self.service.sandbox.environment_digest,
            'semantic_review_digests':semantic_reviews}
        self.store.put('skill_s1',digest([contract_ref,candidate_id]),report)
        return report

    async def s2(self,contract_ref,candidate_id,tasks,*,efficiency_metric=None):
        if efficiency_metric not in {None,'model_attempts','tool_calls','input_tokens','output_tokens'}:raise RepoError('SKILL_EFFICIENCY_METRIC')
        report=self.s1(contract_ref,candidate_id,tasks)
        if not report['passed']:return {**report,'stage':'S2','not_run':True}
        contract=CapabilityContract.model_validate(self.store.get('capability_contract',contract_ref))
        candidate=Candidate.model_validate(self.store.get('candidate',candidate_id))
        ids=sorted({self.store.get('skill_probe',pid)['spec']['task_id'] for pid in contract.required_probe_ids})
        trials=[];probe_reports=[]
        for task_id in ids:
            task=tasks[task_id]
            for arm,release in [('A',candidate.base_release_digest),('B',candidate.candidate_release_digest)]:
                trial=await self.runner.run(task,release,'skill-support:'+candidate.id,memory_profile=None)
                trials.append({**trial,'arm':arm})
                expected=[pid for pid in contract.required_probe_ids if self.store.get('skill_probe',pid)['spec']['task_id']==task_id]
                executed=[pid for pid in expected if self.store.get('skill_probe',pid)['spec']['check_id'] in trial.get('executed_check_ids',[])]
                passed=[pid for pid in executed if self.store.get('skill_probe',pid)['spec']['check_id'] in trial.get('passed_check_ids',[])]
                probe_reports.append({'arm':arm,'task_id':task_id,'expected':expected,'executed':executed,
                    'skipped':sorted(set(expected)-set(executed)),'passed':passed})
        roots=[t['state_root'] for t in trials]
        isolated=len(roots)==len(set(roots))
        comparable=['budget','model_config_digest','environment_digest','contract_digest','workspace_baseline_digest']
        balanced=all(all(trials[i].get('manifest',{}).get(k)==trials[i+1].get('manifest',{}).get(k) for k in comparable) for i in range(0,len(trials),2))
        isolated=isolated and balanced
        evidence=all(not p['skipped'] and p['passed']==p['expected'] for p in probe_reports if p['arm']=='B')
        infra=any(t['state']=='INFRASTRUCTURE_ERROR' for t in trials)
        application=all(t.get('capability_applied',False) for t in trials if t['arm']=='B')
        quality=not infra and isolated and evidence and application and all(t['passed'] for t in trials if t['arm']=='B')
        report={'stage':'S2','passed':quality,'isolated':isolated,'probes':probe_reports,'trials':trials,
            'candidate':candidate_id,'contract_ref':contract_ref,'skill_applied':application,
            'support_implementation_digests':[digest(tasks[i]['files']) for i in ids],
            'support_layout_digests':[digest(sorted(tasks[i]['files'])) for i in ids],
            'predeclared_efficiency_metric':efficiency_metric,
            'evidence_class':'INTEGRATION_ONLY' if any(t['provider_kind']=='scripted_test_double' for t in trials) else 'MODEL_TASK_EVALUATION'}
        self.store.put('skill_s2',digest([contract_ref,candidate_id]),report)
        self.store.put('candidate_state',candidate_id,{'state':'INTEGRATION_ONLY' if report['evidence_class']=='INTEGRATION_ONLY' else 'LOCAL_VALIDATED' if quality else 'SUPPORT_REPLAY_REJECTED','skill_report':digest(report)})
        return report

    async def s3(self,contract_ref,candidate_id,tasks):
        prior=self.store.get('skill_s2',digest([contract_ref,candidate_id]))
        if not prior or not prior['passed']:raise RepoError('SKILL_S2_REQUIRED')
        roles=[t.get('skill_role') for t in tasks]
        if not 3<=roles.count('CORE')<=4 or roles.count('TRANSFER')!=1 or roles.count('NEGATIVE')!=1 or any(r not in {'CORE','TRANSFER','NEGATIVE'} for r in roles):raise RepoError('SKILL_S3_COVERAGE')
        # Renaming a task or rewording its goal cannot create independent evidence.
        identities=[digest(t['files']) for t in tasks]
        if len(set(identities))!=len(identities):raise RepoError('SKILL_REPEATED_TASK')
        contract=CapabilityContract.model_validate(self.store.get('capability_contract',contract_ref))
        if set(identities)&set(prior.get('support_implementation_digests',[])):raise RepoError('SKILL_SUPPORT_TASK_REUSED')
        if contract.binding_slots:
            layouts={digest(sorted(t['files'])) for t in tasks if t['skill_role']=='TRANSFER'}
            if not layouts-set(prior.get('support_layout_digests',[])):
                raise RepoError('SKILL_PORTABLE_TRANSFER_LAYOUT_REQUIRED')
        for task in tasks:
            family=task.get('capability_context',{}).get('task_family')
            if not family or (task['skill_role']=='TRANSFER' and family not in contract.task_families) or (task['skill_role']=='NEGATIVE' and family in contract.task_families):raise RepoError('SKILL_S3_FAMILY_BINDING')
        candidate=Candidate.model_validate(self.store.get('candidate',candidate_id));trials=[]
        for task in tasks:
            pair=[]
            for arm,release in [('A',candidate.base_release_digest),('B',candidate.candidate_release_digest)]:
                trial=await self.runner.run(task,release,'skill-transfer:'+candidate.id,memory_profile=None)
                pair.append({**trial,'arm':arm,'skill_role':task['skill_role']})
            trials.extend(pair)
        from .cost_metrics import aggregate_cost,compare_cost
        old_cost,new_cost=aggregate_cost(trials[::2]),aggregate_cost(trials[1::2])
        cost=compare_cost(old_cost,new_cost,self.service.releases.governor.policy['max_cost_ratio'])
        regressions=[trials[i]['task_id'] for i in range(0,len(trials),2) if trials[i]['passed'] and not trials[i+1]['passed']]
        misfires=[t['task_id'] for t in trials if t['arm']=='B' and t['skill_role']=='NEGATIVE' and t.get('capability_offered',False)]
        transfer=all(t['passed'] and t.get('capability_applied',False) for t in trials if t['arm']=='B' and t['skill_role']=='TRANSFER')
        roots=[t['state_root'] for t in trials]+[t['state_root'] for t in prior['trials']]
        balanced=all(all(trials[i].get('manifest',{}).get(k)==trials[i+1].get('manifest',{}).get(k) for k in ['budget','model_config_digest','environment_digest','contract_digest','workspace_baseline_digest']) for i in range(0,len(trials),2))
        execution_valid=len(roots)==len(set(roots)) and balanced and all(t['state']!='INFRASTRUCTURE_ERROR' for t in trials)
        core_passed=all(t['passed'] for t in trials if t['arm']=='B' and t['skill_role']=='CORE')
        report={'stage':'S3','passed':execution_valid and core_passed and not regressions and not misfires and transfer and cost['bounded'] and cost['cost_settled'],
            'execution_valid':execution_valid,'core_passed':core_passed,
            'pass_to_fail':regressions,'misfires':misfires,'transfer_passed':transfer,'cost_comparison':cost,'trials':trials,
            'candidate':candidate_id,'contract_ref':contract_ref,'release_activated':False}
        gains=[trials[i]['task_id'] for i in range(0,len(trials),2) if not trials[i]['passed'] and trials[i+1]['passed']]
        metric=prior.get('predeclared_efficiency_metric')
        efficiency=bool(metric and cost['improved'] and old_cost.get(metric) is not None and new_cost.get(metric) is not None and new_cost[metric]<old_cost[metric])
        report.update(fail_to_pass=gains,predeclared_efficiency_metric=metric,measured_benefit=bool(gains or efficiency))
        report['evidence_class']='INTEGRATION_ONLY' if prior['evidence_class']=='INTEGRATION_ONLY' or any(t['provider_kind']=='scripted_test_double' for t in trials) else 'MODEL_TASK_EVALUATION'
        if report['evidence_class']=='MODEL_TASK_EVALUATION' and not report['measured_benefit']:
            report['passed']=False
            report['reason']='SKILL_NO_MEASURED_BENEFIT'
        self.store.put('skill_s3',digest([contract_ref,candidate_id]),report)
        self.store.put('candidate_state',candidate_id,{'state':'INTEGRATION_ONLY' if report['evidence_class']=='INTEGRATION_ONLY' else 'CHAMPION_CANDIDATE' if report['passed'] else 'LOCAL_VALIDATED','skill_report':digest(report)})
        if report['passed'] and report['evidence_class']=='MODEL_TASK_EVALUATION':
            payload={'candidate':candidate_id,'release':candidate.candidate_release_digest,'contract_ref':contract_ref,
                's1':digest(self.store.get('skill_s1',digest([contract_ref,candidate_id]))),
                's2':digest(prior),'s3':digest(report),'evidence_class':report['evidence_class']}
            self.store.put('skill_qualification',candidate_id,{'payload':payload,'signature':self.store.sign(payload)},immutable=True)
        return report
