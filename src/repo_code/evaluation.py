"""Real clean-workspace task trials and progressive gates. Mock models are labelled."""
from __future__ import annotations
import asyncio
import json
import tempfile
import time
import shutil
import sqlite3
import contextlib
from pathlib import Path
from .models import Contract, Budget, Candidate
from .acceptance import freeze_contract
from .providers import ScriptedProvider,is_test_double
from .service import RepoCode
from .governance import Lineage
from .util import RepoError, uid, digest, atomic_write, now
from .workspace import relative_path

INHERIT_MEMORY=object()


class TrialRunner:
    def __init__(self,service,provider_factory,isolated_states=False,trial_root=None,*,review_contract_tasks=False,legacy_window=True,context_ceiling=64_000):
        self.service,self.provider_factory=service,provider_factory
        self.isolated_states,self.trial_root=isolated_states,Path(trial_root) if trial_root else None
        self.review_contract_tasks,self.legacy_window=review_contract_tasks,legacy_window
        if type(context_ceiling) is not int or not 6_000 <= context_ceiling <= 262_500:
            raise RepoError("INVALID_TRIAL_CONTEXT_CEILING")
        self.context_ceiling = context_ceiling
    async def run(self,task,release,cohort,hidden=False,memory_profile=INHERIT_MEMORY):
        start=time.monotonic();s=self.service;run_id=None
        # Only public inputs are passed to provider construction. Oracle stays in Host.
        provider=self.provider_factory({k:task[k] for k in ("id","goal","files") if k in task})
        memory=getattr(s,'memory',None)
        selected=memory.profile if memory_profile is INHERIT_MEMORY and memory else None if memory_profile is INHERIT_MEMORY else memory_profile
        trial_profile=selected.model_copy(update={'learning_enabled':False}) if selected is not None else None
        state_root=s.store.root
        if self.isolated_states:
            # Clone only the idle frozen Host/template. No previous trial's
            # messages or writable knowledge is ever used as the next input.
            from .maintenance import Maintenance
            Maintenance(s.store)._idle()
            if self.trial_root is None:raise RepoError('TRIAL_ROOT_REQUIRED')
            self.trial_root.mkdir(parents=True,exist_ok=True)
            state_root=Path(tempfile.mkdtemp(prefix='trial-',dir=self.trial_root))
            with contextlib.closing(s.store.connect()) as origin,contextlib.closing(sqlite3.connect(state_root/'runtime.db')) as target:
                origin.backup(target)
                target.execute("DELETE FROM objects WHERE kind IN ('trial_dev','trial_private','evaluation_dev','evaluation_private')")
                target.commit()
            shutil.copy2(s.store.root/'host.key',state_root/'host.key')
            shutil.copytree(s.store.artifact_root,state_root/'artifacts')
        app=RepoCode(state_root,provider,sandbox=s.sandbox.mode,image=s.sandbox.image,
                     memory_profile=trial_profile,memory_encoder=memory.encoder if trial_profile and memory else None)
        app.sandbox.kit=s.sandbox.kit
        app.agent.review_contract_tasks = self.review_contract_tasks
        app.agent.context_ceiling = self.context_ceiling
        try:
            with tempfile.TemporaryDirectory(prefix="trial-source-",dir=s.store.root) as temp:
                root=Path(temp)
                for path,text in task.get("files",{}).items():atomic_write(root/relative_path(path),text.encode())
                contract=Contract.model_validate(task["contract"])
                if contract.status!="FROZEN":contract=freeze_contract(contract,"trusted-suite-curator")
                budget=Budget.model_validate(task.get("budget",{"model_calls":8,"tool_calls":30,"wall_seconds":120}))
                if trial_profile:
                    scope='eval:'+cohort
                    knowledge=app.memory.pin(scope,visibility='TRUSTED_ONLY')
                    from .providers import model_configuration
                    run=app.runtime.create(task['goal'],root,release,contract,budget,scope=scope,model_config=model_configuration(provider),
                                           visibility='TRUSTED_ONLY' if hidden else 'DEV',knowledge_pin=knowledge)
                    app.broker.authorize_run(run['id'],True,True,True)
                else:
                    knowledge=app.runtime.knowledge.pin("eval:"+cohort,include_user=False)
                    run=app.new_task(task["goal"],root,contract,budget,True,True,"TRUSTED_ONLY" if hidden else "DEV",release,knowledge)
                    if self.isolated_states and self.legacy_window:
                        from .context import ContextEngine
                        class FixedLegacyWindow(ContextEngine):
                            def build(self,run_id,tools,policy=None,*args,**kw):
                                return super().build(run_id,tools,{**(policy or {}),'max_tokens':15500,'reserve_tokens':4096},*args,**kw)
                        app.agent.context=FixedLegacyWindow(app.store,app.runtime,app.agent.context.request_serializer,app.agent.context.output_reserve,app.agent.context.input_scale)
                run_id=run['id']
                skill_context=task.get('capability_context')
                if skill_context:
                    if not self.isolated_states:raise RepoError('SKILL_ISOLATED_STATES_REQUIRED')
                    with app.store.tx() as c:
                        current=app.runtime.run(run_id,c)
                        current['data']['manifest']['scope']=skill_context['scope']
                        current['data']['manifest']['task_family']=skill_context['task_family']
                        app.runtime.update(c,current,data=current['data'])
                        payload={'run_id':run_id,'release':release,'task_digest':digest(task),
                            'scope':skill_context['scope'],'family':skill_context['task_family'],
                            'role':task.get('skill_role','SUPPORT'),'environment':app.sandbox.environment_digest}
                        app.store.put('capability_trial',run_id,{'payload':payload,'signature':app.store.sign(payload)},c,immutable=True)
                        app.store.put('operator_config','capability_router',{'enabled':True},c)
                if self.isolated_states:
                    probe="from pathlib import Path;p=Path('/workspace');list(p.iterdir());f=p/'._host_preflight';f.write_text('ok');assert f.read_text()=='ok';print('PREFLIGHT_OK')"
                    preflight=await app.sandbox.run(run['id'],app.runtime.workspace(run).root,['python','-c',probe],timeout=20)
                    if preflight['exit_code']!=0 or preflight['stdout'].strip()!='PREFLIGHT_OK':raise RepoError('INFRA_ERROR','Workspace preflight failed before any model call')
                report=await app.execute(run_id)
                passed=report["state"]=="SUCCEEDED" and report["delivery"] is not None
                output={"id":uid("trial-"),"task_id":task["id"],"run_id":run_id,"release":release,"cohort":cohort,"hidden":hidden,"passed":passed,"state":report["state"],"manifest":report["manifest"],"evidence_ids":report["delivery"]["evidence_ids"] if passed else [],"cost":report["budget_used"],"usage":report["usage"],"wall_seconds":time.monotonic()-start,"provider_kind":"scripted_test_double" if is_test_double(provider) else type(provider).__name__}
                ev=[e for e in app.store.values('evidence') if e.get('run_id')==run_id]
                output['executed_check_ids']=sorted({e['check_id'] for e in ev if e.get('executed_ids') and not e.get('skipped_ids')})
                output['passed_check_ids']=sorted({e['check_id'] for e in ev if e['status']=='PASSED' and e.get('executed_ids') and not e.get('skipped_ids')})
                receipts=[app.store.get('application_receipt',k) for k in report.get('application_receipts',[])]
                output['capability_applied']=any(r and r['applied'] for r in receipts)
                output['capability_offered']=any(o['run_id']==run_id and o['offered'] for o in app.store.values('capability_offer'))
                output['application_receipts']=report.get('application_receipts',[])
                if 'candidate_review' in report:output['candidate_review']=report['candidate_review']
                if 'memory_cost' in report:output['memory_cost']=report['memory_cost']
        except Exception as exc:
            if run_id:
                app.sandbox.stop_run(run_id)
                app.runtime.end(run_id,'BLOCKED',getattr(exc,'code',type(exc).__name__))
            partial = app.agent.report(run_id) if run_id else {}
            output={"id":uid("trial-"),"task_id":task["id"],"run_id":run_id,"release":release,"cohort":cohort,"hidden":hidden,"passed":False,"state":"INFRASTRUCTURE_ERROR","error_type":type(exc).__name__,"error":getattr(exc,"code",str(exc))[:200],"cost":partial.get("budget_used",{}),"usage":partial.get("usage",{"usage_available":False}),"wall_seconds":time.monotonic()-start,"provider_kind":"scripted_test_double" if is_test_double(provider) else type(provider).__name__}
        app.sandbox.stop_run(run_id) if run_id else None
        from .cost_metrics import trial_cost
        output['cost_vector']=trial_cost(output,getattr(provider,'records',None))
        app.close()
        output['state_root']=str(state_root)
        s.store.put("trial_private" if hidden else "trial_dev",output["id"],output,immutable=True)
        return output

class SuiteEvaluator:
    def __init__(self,service,vault,provider_factory):
        self.service,self.store,self.releases,self.vault=service,service.store,service.releases,vault
        self.cap=vault.evaluator_capability();self.runner=TrialRunner(service,provider_factory)
    async def paired(self,candidate,key,hidden=False,gate="DEV",require_gain=False):
        cohort=self.vault.open(key,self.cap if hidden else None)
        if hidden:self.vault.claim(candidate.id,key,gate)
        trials=[];f2p=[];p2f=[];baseline_ok=0;candidate_ok=0
        for task in cohort["tasks"]:
            old=await self.runner.run(task,candidate.base_release_digest,key,hidden)
            new=await self.runner.run(task,candidate.candidate_release_digest,key,hidden)
            trials.extend([old,new]);baseline_ok+=old["passed"];candidate_ok+=new["passed"]
            if not old["passed"] and new["passed"]:f2p.append(task["id"])
            if old["passed"] and not new["passed"]:p2f.append(task["id"])
        # All attempts, including failures, contribute to budget reports.
        old_cost=sum(x["cost"].get("model_calls",0)+x["cost"].get("tool_calls",0) for x in trials[::2])
        new_cost=sum(x["cost"].get("model_calls",0)+x["cost"].get("tool_calls",0) for x in trials[1::2])
        infra=any(x["state"]=="INFRASTRUCTURE_ERROR" for x in trials)
        from .cost_metrics import aggregate_cost, compare_cost
        vectors={'baseline':aggregate_cost(trials[::2]),'candidate':aggregate_cost(trials[1::2])}
        comparison=compare_cost(vectors['baseline'],vectors['candidate'],self.releases.governor.policy['max_cost_ratio'])
        quality_passed=not infra and candidate_ok>0 and not p2f and candidate_ok>=baseline_ok
        integration=cohort.get('synthetic_integration') or any(x['provider_kind']=='scripted_test_double' for x in trials)
        passed=quality_passed and ((bool(f2p) or comparison['improved']) if require_gain else True) and comparison['bounded'] and (comparison['cost_settled'] or integration)
        evidence_class="INTEGRATION_ONLY" if cohort.get("synthetic_integration") or any(x["provider_kind"]=="scripted_test_double" for x in trials) else "MODEL_TASK_EVALUATION"
        report={"evidence_class":evidence_class,"passed":bool(passed),"cohort":key,"trials":[x["id"] for x in trials],"fail_to_pass":f2p,"pass_to_fail":p2f,"baseline_passed":baseline_ok,"candidate_passed":candidate_ok,"tasks":len(cohort["tasks"]),"cost":{"baseline":old_cost,"candidate":new_cost},"provider_kinds":sorted({x["provider_kind"] for x in trials})}
        report.update(quality_passed=quality_passed,cost_vectors=vectors,cost_comparison=comparison,
                      legacy_cost_proxy_only=True,statistics_version=3)
        report_key=self.store.json_blob(report)
        self.store.put("evaluation_private" if hidden else "evaluation_dev",report_key,report,immutable=True)
        if hidden:self.vault.notice(candidate.id,key,passed,"QUALITY" if not passed else "PASSED")
        else:self.vault.mark_exposure(candidate.id,key)
        return report
    async def safety(self,candidate):
        from .regression import CandidateRegression
        vector=self.releases.governor.check(self.releases.components(candidate.candidate_release_digest),candidate.operation,candidate.growth_justification)
        if not self.service.sandbox.isolated:raise RepoError("ISOLATION_REQUIRED")
        report=await CandidateRegression(self.service).run(candidate)
        report["complexity"]=vector
        return report
    async def qualify(self,candidate_id,dev,promotion,final):
        candidate=Candidate.model_validate(self.store.get("candidate",candidate_id))
        roles=[self.store.get("cohort",x)["role"] for x in (dev,promotion,final)]
        if roles!=["dev","promotion","final"] or len({dev,promotion,final})!=3:raise RepoError("COHORT_ROLE_MISMATCH")
        self.releases.governor.check(self.releases.components(candidate.candidate_release_digest),candidate.operation,candidate.growth_justification)
        self.releases.record_gate(candidate,"G0",{"passed":True,"scope":"schema, allowed components, AST, immutable build"})
        dev_report=await self.paired(candidate,dev,require_gain=False)
        self.releases.record_gate(candidate,"G1",dev_report);self.releases.record_gate(candidate,"G2",dev_report)
        if not dev_report["passed"]:return self._reject(candidate,"DEV_REGRESSION")
        freeze={"release":candidate.candidate_release_digest,"model":{"adapter":type(self.service.provider).__name__,"id":getattr(self.service.provider,"model","scripted")},"protocol":"paired-all-attempts-v1","cohorts":[dev,promotion,final],"gate":self.releases.governor.digest}
        self.store.put("candidate_freeze",candidate.id,freeze,immutable=True)
        self.store.put("candidate_state",candidate.id,{"state":"FROZEN"})
        promotion_report=await self.paired(candidate,promotion,True,"G3",require_gain=self.releases.governor.policy["require_net_gain"])
        self.releases.record_gate(candidate,"G3",promotion_report)
        if not promotion_report["passed"]:return self._reject(candidate,"HOLDOUT_REJECT")
        safety=await self.safety(candidate);self.releases.record_gate(candidate,"G4",safety)
        if not safety["passed"]:return self._reject(candidate,"SECURITY_OR_COMPLEXITY")
        final_report=await self.paired(candidate,final,True,"G4b")
        self.releases.record_gate(candidate,"G4b",final_report)
        if not final_report["passed"]:return self._reject(candidate,"FINAL_REJECT")
        self.store.put("candidate_state",candidate.id,{"state":"AWAITING_APPROVAL"})
        return {"candidate":candidate.id,"status":"PASS","next":"operator approval then whole-release CAS"}
    def _reject(self,candidate,reason):
        self.store.put("candidate_state",candidate.id,{"state":"REJECTED","reason":reason})
        return {"candidate":candidate.id,"status":"REJECT","reason":reason}
