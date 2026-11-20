"""Bounded, problem-driven L1. Diagnostic hypotheses are validated by real trials."""
from __future__ import annotations
import json
from .models import Candidate
from .util import RepoError, digest, uid, now
from .governance import COMPONENTS, Lineage
from .observation import Observer
from .evolved import EvolvedWorker

class EvolutionEngine:
    def __init__(self,service,provider,evaluator=None,*,optimizer_release=None):
        self.service,self.store,self.releases,self.provider,self.evaluator=service,service.store,service.releases,provider,evaluator
        # Coding base and optimizer implementation are different inputs. In
        # Meta-Eval this is pinned explicitly and NEVER inferred from coding_base.
        self.optimizer_release = optimizer_release
        if optimizer_release is not None:
            self.releases.get(optimizer_release)

    def optimizer_binding(self, coding_base=None):
        release_digest = self.optimizer_release or coding_base or self.releases.active()["digest"]
        release = self.releases.get(release_digest)
        return {"optimizer_release_digest": release_digest,
                "optimizer_component_digest": release.components["l1_optimizer"]}

    async def _optimizer_call(self, operation, payload, coding_base=None):
        binding = self.optimizer_binding(coding_base)
        component = self.store.read_json(binding["optimizer_component_digest"])
        record = {"id": uid("optimizer-call-"), **binding, "operation": operation,
                  "input_digest": self.store.json_blob(payload), "state": "STARTED"}
        self.store.put("optimizer_invocation", record["id"], record)
        try:
            out = await EvolvedWorker(self.store, self.service.sandbox).converse(
                component, {"operation": operation, **payload}, self.provider,
                run_id=record["id"])
            record.update(state="COMPLETED", output_digest=self.store.json_blob(out))
            return out
        except BaseException as exc:
            record.update(state="FAILED", error=getattr(exc,"code",type(exc).__name__))
            raise
        finally:
            self.store.put("optimizer_invocation", record["id"], record)
    def evidence(self,pattern_id):
        p=self.store.get("pattern",pattern_id)
        if not p:raise RepoError("PATTERN_NOT_FOUND")
        problems=[self.store.get("problem",x) for x in p["support"]]
        events=[]
        for problem in problems:
            if problem.get('episode_ref'):
                from .episodes import EpisodeBuilder
                events += EpisodeBuilder(self.store).evidence(problem['episode_ref'])['events']
            else:
                events += self.store.events(problem["run_id"],max(0,problem["sequence"]-8),limit=12)
        exact=any(x.get('episode_ref') for x in problems)
        return {"pattern":p,"problems":problems,"events":list({e['event_id']:e for e in events}.values()) if exact else events[-50:],
                "evidence_mode":"EXACT_EPISODE_REFERENCES" if exact else "LEGACY_BOUNDED_WINDOW"}
    async def diagnose(self,pattern_id,base=None):
        evidence=self.evidence(pattern_id);allowed_ids={x["event_id"] for x in evidence["events"]}
        prompt="Diagnose ONLY this observed failure path. Do not enumerate unrelated components. Return JSON {conclusion: testable|inconclusive|environment|trusted_safety, hypothesis: string, targets: [component_id], evidence_ids:[event_id], negative_probes:[string]}. Code errors alone do not prove harness defects. Allowed targets: memory_policy,context_policy,retry_policy,planning_policy,tools_schema,skills_index,delegation_policy. Cite actual event IDs."
        binding=self.optimizer_binding(base)
        policy=await self._optimizer_call("diagnose", {"evidence":evidence}, base)
        if "diagnosis" in policy:
            report=policy["diagnosis"];usage={"source":"isolated_algorithm"}
        else:
            prompt+="\nOptimizer guidance: "+str(policy.get("diagnosis_instructions",""))[:4000]
            reply=await self.provider.complete(prompt,[{"role":"user","content":json.dumps(evidence,ensure_ascii=False)}],[])
            try:report=json.loads(reply.text)
            except ValueError:raise RepoError("INVALID_DIAGNOSIS") from None
            usage=reply.usage
        if report.get("conclusion") not in {"testable","inconclusive","environment","trusted_safety"}:raise RepoError("INVALID_DIAGNOSIS")
        if report["conclusion"]=="testable":
            targets = report.get("targets", [])
            if (not isinstance(targets, list) or not targets or len(targets) > 3
                    or not set(targets) <= COMPONENTS - {"l1_optimizer", "l2_optimizer"}
                    or not report.get("evidence_ids") or not set(report["evidence_ids"]) <= allowed_ids
                    or not isinstance(report.get("hypothesis"), str) or not report["hypothesis"].strip()
                    or not isinstance(report.get("negative_probes"), list) or not report["negative_probes"]
                    or not all(isinstance(x,str) and x.strip() for x in report["negative_probes"])):
                raise RepoError("UNSUPPORTED_DIAGNOSIS")
        report.update(id=uid("diagnosis-"),pattern_id=pattern_id,usage=usage,**binding)
        self.store.put("diagnosis",report["id"],report,immutable=True);return report
    async def propose(self,diagnosis,max_candidates=3,base=None,parents=None):
        if diagnosis["conclusion"]!="testable":return []
        limit=min(max(1,max_candidates),self.releases.governor.policy["max_candidates"])
        base=base or self.releases.active()["digest"];components=self.releases.components(base)
        prompt="Generate targeted replacement candidates for this evidence-supported defect. Prefer TUNE/REPLACE/SIMPLIFY/MERGE/REMOVE. Return JSON {candidates:[{operation,changes:{component_id:{kind:config|python,...}},hypothesis,growth_justification}]}. Python components expose handle(payload)->JSON dict in a sandbox, no credentials/files/tools. Only the supplied targets can change; no policy budgets, judges, safety or publisher changes. No claims of improvement without trials."
        payload={"diagnosis":diagnosis,"baseline":{k:components[k] for k in diagnosis["targets"]},"candidate_limit":limit}
        binding=self.optimizer_binding(base)
        policy=await self._optimizer_call("propose", {"input":payload}, base)
        if "candidates" in policy:specs=policy["candidates"];usage={"source":"isolated_algorithm"}
        else:
            prompt+="\nSearch guidance: "+str(policy.get("proposal_instructions",""))[:4000]
            reply=await self.provider.complete(prompt,[{"role":"user","content":json.dumps(payload,ensure_ascii=False)}],[])
            try:specs=json.loads(reply.text)["candidates"]
            except (ValueError,KeyError,TypeError):raise RepoError("INVALID_CANDIDATES") from None
            usage=reply.usage
        if not isinstance(specs,list):raise RepoError("INVALID_CANDIDATES")
        seen=set();out=[]
        for spec in specs[:limit]:
            changes=spec.get("changes",{})
            if not changes or not set(changes)<=set(diagnosis["targets"]):raise RepoError("CANDIDATE_SCOPE_DENIED")
            key=digest(changes)
            if key in seen:continue
            seen.add(key)
            candidate=self.releases.candidate(diagnosis["pattern_id"],changes,spec["hypothesis"],[*diagnosis["evidence_ids"],diagnosis["id"],binding["optimizer_release_digest"]],spec.get("operation","REPLACE"),parents=parents,justification=spec.get("growth_justification",""),base=base)
            out.append(candidate)
        self.store.put("generation",uid("gen-"),{"diagnosis":diagnosis["id"],"candidates":[c.id for c in out],"usage":usage,**binding})
        return out
    async def search(self,pattern_id,dev_suite,max_candidates=3,max_rounds=2,base=None,strategy=None):
        import asyncio
        from .providers import MeteredProvider
        if self.evaluator is None:raise RepoError("EVALUATOR_REQUIRED")
        ledger={"calls":0,"limit":256,"usage":[]}
        original_provider=self.provider;original_factory=self.evaluator.runner.provider_factory
        self.provider=MeteredProvider(original_provider,ledger)
        self.evaluator.runner.provider_factory=lambda public:MeteredProvider(original_factory(public),ledger)
        try:
            async with asyncio.timeout(1800):
                result=await self._search(pattern_id,dev_suite,max_candidates,max_rounds,base,strategy)
            result.update(self.optimizer_binding(base))
            result["all_model_calls"]=ledger["calls"]
            return result
        finally:
            self.provider=original_provider;self.evaluator.runner.provider_factory=original_factory
            self.store.put("evolution_budget",uid("budget-"),ledger,immutable=True)

    async def _search(self,pattern_id,dev_suite,max_candidates=3,max_rounds=2,base=None,strategy=None):
        if self.evaluator is None:raise RepoError("EVALUATOR_REQUIRED")
        cfg=await self._optimizer_call("search_config", {"pattern_id":pattern_id}, base)
        max_candidates=min(max_candidates,max(1,int(cfg.get("max_candidates",max_candidates))))
        max_rounds=min(max_rounds,max(1,int(cfg.get("max_rounds",max_rounds))))
        start=now();diagnosis=await self.diagnose(pattern_id,base);diagnosis["search_strategy"]=str(strategy or cfg.get("order","evidence_first"))[:1000];all_candidates=[];results=[];best=None;calls=1
        if diagnosis["conclusion"]!="testable":return {"status":diagnosis["conclusion"].upper(),"candidates":[],"model_calls":calls}
        budget=min(max_candidates,5)
        for _ in range(min(max_rounds,3)):
            remaining=budget-len(all_candidates)
            if remaining<=0:break
            candidates=await self.propose(diagnosis,remaining,base=base,parents=[c.id for c in all_candidates]);calls+=1
            if not candidates:break
            for candidate in candidates:
                all_candidates.append(candidate)
                report=await self.evaluator.paired(candidate,dev_suite)
                results.append({"candidate":candidate.id,"report":report})
                self.store.put("candidate_state",candidate.id,{"state":"DEV_EVALUATED","report":self.store.json_blob(report)})
                if report["passed"] and (best is None or report["candidate_passed"]>best["report"]["candidate_passed"] or report["candidate_passed"]==best["report"]["candidate_passed"] and report["cost"]["candidate"]<best["report"]["cost"]["candidate"]):best=results[-1]
            if best:break
            diagnosis={**diagnosis,"previous_dev_failures":results[-3:]}
        output={"status":"DEV_CANDIDATE_SELECTED" if best else "NO_NET_GAIN","selected":best["candidate"] if best else None,"candidates":[c.id for c in all_candidates],"reports":results,"model_calls":calls,"wall_seconds":now()-start}
        self.store.put("experiment",uid("experiment-"),output)
        return output
    async def curate_episode(self,episode_id):
        """Reuse Consolidator for v1/v2 without switching the runtime Memory default."""
        memory=getattr(self.service,'memory',None)
        if memory is not None:
            consolidator=memory.consolidator
        else:
            from .memory.repository import MemoryRepository
            from .memory.consolidation import Consolidator
            from .memory.models import MemoryProfile
            consolidator=Consolidator(MemoryRepository(self.store),self.service.runtime,MemoryProfile())
        return await consolidator.curate_episode(episode_id,self.provider)

    async def extract_skill(self,run_ids,scope="project"):
        """Compatibility entry, now returns structured Lesson/Skill candidate receipts.

        The old raw-trace-to-knowledge draft route is retired. No activation.
        """
        from .verified_stages import VerifiedStages
        stages=VerifiedStages(self.service.runtime)
        run_ids=list(dict.fromkeys(run_ids))
        wanted=set(run_ids)
        candidates=[stages.payload(row) for row in self.store.values(stages.KIND)
                    if row['payload']['run_id'] in wanted]
        reports=[]
        for run_id in run_ids:
            run=self.service.runtime.run(run_id)
            if run['data']['visibility']!='DEV':raise RepoError('HIDDEN_DATA_DENIED')
            actual=run['data']['manifest']['scope']
            if scope!='project' and scope!=actual:raise RepoError('MEMORY_SOURCE_SCOPE')
        for stage in sorted(candidates,key=lambda s:s['sequence']):
            for episode_id in stages.materialize(stage['id']):
                reports.append(await self.curate_episode(episode_id))
        return {'contract':'episode-curation-r3','reports':reports,'release_activated':False}
