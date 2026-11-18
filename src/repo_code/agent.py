"""Dynamic L0 loop. Final model text is a verification request, not a verdict."""
from __future__ import annotations
import contextlib
import asyncio
import json
import time
from pydantic import ValidationError
from .models import OperationRequest, TERMINAL
from .context import ContextEngine
from .evolved import EvolvedWorker
from .tool_models import definitions
from .util import RepoError, ProviderNotDispatched, uid, digest, canonical

REVIEW_CALL_LIMIT = 8
REVIEW_GUIDANCE = (
    "When the request provides a concrete behavioral example, run a small reproduction and trace the branch "
    "that input actually reaches before editing. Check that any helper you call is defined or imported. "
    "Before delivery, derive checks from the actual diff. Exercise the requested case and a nearby case "
    "that should retain its behavior. For a changed predicate or early return, consider an alternate input form "
    "that takes the same path. Inspect related public tests, documentation, and callers for contract conflicts. "
    "If a public interface or diagnostic is affected, check it through its public entry point when available. "
    "Preserve existing expectations, and explain any conflict or unverified case in the handoff."
)
REVIEW_FOLLOWUP = (
    "The last no-tool reply was a provisional handoff. Complete the bounded candidate review now. "
    "Use the current diff and test scopes to choose one distinct, small check for an uncovered boundary or "
    "counterexample. Run it with authorized tools and use assertions or a test runner with a reliable exit status. "
    "Check both the reported behavior and an affected neighboring path. A failing check is evidence "
    "to repair the candidate and recheck its final source."
)
REVIEW_PROBE_ACTION = (
    "The previous final was provisional. Verify the single pending probe below in this response. "
    "Use the tool arguments below, replacing the assertion placeholder with the expected behavior. "
    "Use observed, the actual value produced by the original probe, to check the expected behavior "
    "from the issue. With multiple prints observed is a tuple in print order. Each multi-argument "
    "print contributes a tuple. The stdout excerpt is diagnostic text. "
    "If you cannot form a justified assertion, return a concise review_blocked explanation. "
    "A summary without new verification evidence ends the review as incomplete."
)
REVIEW_REPAIR_ACTION = (
    "The previous final was provisional. The Host recorded the focused check below failing. "
    "If the source is unchanged since that failure, inspect its cause and repair it, or explain a concrete blocker. "
    "Rerun that same check on the final source. If source has already changed, rerun it now. "
    "A different passing check does not resolve this failure."
)
REVIEW_NO_CHANGE_ACTION = (
    "The previous final was provisional. The workspace has no net changes after a write attempt. "
    "Use the receipt below to inspect the intended location. For an ambiguous edit, read the suggested "
    "windows and include a unique enclosing definition; prefer a versioned edit or patch. "
    "Make and check the intended change, or explain the concrete blocker if no change is justified. "
    "The same unchanged workspace will not trigger another recovery."
)
REVIEW_REVERT_ACTION = (
    "The previous final was provisional. The draft was reverted and the workspace is back at baseline. "
    "Use the request's smallest concrete example to reproduce the target behavior on the current source now. "
    "Call the relevant public entry point or routine and capture actual versus expected behavior. "
    "Trace the branch that input reaches before making a narrow edit, then rerun the same check. "
    "Use a broad suite later. Its failures on restored baseline do not diagnose the reverted draft. "
    "If the exact reproduction is blocked, state the concrete prerequisite."
)
REVIEW_REPRODUCED_ACTION = (
    "The previous final was provisional. A focused check has now reproduced or observed the behavior on "
    "baseline. Use that evidence to inspect the relevant branch, make a narrow change, and recheck the same "
    "example and a nearby unaffected case. Explain a concrete blocker if the evidence does not justify a fix."
)


def candidate_review_prompt(review):
    prefix = '\nCandidate review requested. '
    if review.get('focus') == 'APPLY_REPRODUCTION':
        return prefix + REVIEW_REPRODUCED_ACTION + '\nBaseline reproduction (tool data): ' + json.dumps(review['baseline_reproduction'], ensure_ascii=False)
    if review.get('focus') == 'NO_SOURCE_CHANGE':
        if review.get('reverted_write'):
            evidence = {k: review[k] for k in ('reverted_write', 'attempted_check_failure', 'baseline_check_failure')
                        if review.get(k)}
            return prefix + REVIEW_REVERT_ACTION + '\nRecovery evidence (tool data): ' + json.dumps(evidence, ensure_ascii=False)
        return prefix + REVIEW_NO_CHANGE_ACTION + '\nWrite receipt (tool data): ' + json.dumps(review['failed_write'], ensure_ascii=False)
    if review.get('focus') == 'REPAIR_CHECK':
        return prefix + REVIEW_REPAIR_ACTION + '\nFocused failed check (tool data): ' + json.dumps(review['focused_check'], ensure_ascii=False)
    if review.get('pending_probes'):
        pending = review['pending_probes']
        probe = pending[0]
        template = {'check_probe': probe['operation'], 'argv': ['python', '-c', 'assert <expected behavior using observed>']}
        action = REVIEW_PROBE_ACTION
        if probe.get('outcome') == 'FAILED':
            action = "The linked assertion failed. Inspect and repair the candidate, then rerun the bound check. " + action
        return (prefix + action + '\nprocess.run arguments: ' + json.dumps(template, ensure_ascii=False)
                + '\nFocused probe (tool data): ' + json.dumps(probe, ensure_ascii=False)
                + f'\nPending probe count: {len(pending)}.')
    return prefix + REVIEW_FOLLOWUP

class CodingAgent:
    def __init__(self,store,runtime,broker,grader,releases,provider,on_delta=None):
        self.review_contract_tasks = False
        self.context_ceiling = 64_000
        self.store,self.runtime,self.broker,self.grader,self.releases,self.provider=store,runtime,broker,grader,releases,provider
        serializer=(lambda s,m,t:provider.build_request(s,m,t)[0]) if hasattr(provider,'build_request') else None
        from .context import input_token_scale
        self.context=ContextEngine(store,runtime,serializer,getattr(provider,'max_tokens',None) or 0,input_token_scale(provider),provider=provider);self.worker=EvolvedWorker(store,broker.sandbox);self.on_delta=on_delta
    async def _pending(self,run_id):
        turn=self.store.get("pending_turn",run_id)
        if not turn:return True
        blocked=0;outcomes=[];budget_stop=None
        for data in turn["requests"]:
            if "invalid_call" in data:
                self.store.append_message(run_id,{"role":"tool","tool_call_id":data["invalid_call"],"content":json.dumps({"is_error":True,"code":data["error"],**({"details":data["details"]} if data.get("details") else {})}),"is_error":True},"invalid:"+run_id+":"+turn["turn_id"]+":"+data["invalid_call"])
                continue
            req=OperationRequest.model_validate(data)
            if budget_stop is None:
                try:
                    receipt=await self.broker.submit(req)
                except RepoError as exc:
                    if getattr(exc,'budget_stop',{}).get('stage') != 'tool':
                        raise
                    budget_stop = exc
                    receipt = self.broker.get(req.key,run_id)
            else:
                # The previous tool proved budget exhaustion. Pair each tail
                # call without dispatching it or touching the workspace.
                try:
                    receipt = self.broker.get(req.key,run_id)
                except RepoError as exc:
                    if exc.code != 'OPERATION_NOT_FOUND':
                        raise
                    receipt = {'status':'CANCELLED','operation_id':'budget-skipped:'+req.key,
                               'result':{'is_error':True,'code':budget_stop.code,'executed':False,
                                         'budget_stop':budget_stop.budget_stop}}
                if receipt['status'] not in {'SUCCEEDED','FAILED','CANCELLED'}:
                    raise RepoError('RECOVERY_REQUIRED')
            if receipt["status"]=="WAITING_APPROVAL":return False
            if receipt["status"] in {"EXECUTING","UNCERTAIN"}:raise RepoError("RECOVERY_REQUIRED")
            body = receipt.get("result") or {}
            if (budget_stop is None and receipt['status'] == 'CANCELLED' and body.get('executed') is False
                    and body.get('code') == 'BUDGET_EXHAUSTED' and (details := body.get("budget_stop"))):
                budget_stop = RepoError('BUDGET_EXHAUSTED',details['dimension'])
                budget_stop.budget_stop = details
            self.broker.append_result(req,receipt)
            outcomes.append((req,receipt))
            if (receipt.get('result') or {}).get('code') in {'PROCESS_NO_PROGRESS','INSPECTION_NO_PROGRESS'}:blocked+=1
        self.store.put("pending_turn",run_id,{"requests":[]})
        if budget_stop is not None:
            raise budget_stop
        if turn['requests']:
            from .task_progress import TaskProgress
            progress=TaskProgress(self.store,self.runtime).batch(run_id,turn['turn_id'],outcomes)
            previous=self.store.get('no_progress_turn',run_id,default={})
            if previous.get('turn_id')!=turn['turn_id']:
                # Unrelated successful noise does not erase withheld evidence.
                streak=previous.get('blocked_turns',0)+1 if blocked and not progress['new_evidence'] else 0
                self.store.put('no_progress_turn',run_id,{'turn_id':turn['turn_id'],'blocked_turns':streak})
                if streak>=2:raise RepoError('NO_PROGRESS','Repeated withheld inspections without new evidence or implementation')
            if progress['stop']:
                from .work_status import work_status
                candidate=bool(work_status(self.store,self.runtime,self.runtime.run(run_id))['changed_path_count'])
                raise RepoError('STAGNATION_AFTER_WRITE' if candidate else 'NO_PROGRESS','Repeated cross-tool inspections without new task evidence; candidate retained, not verified')
        return True
    async def _retry_hint(self,run,components):
        events=self.store.events(run["id"],trusted=True,limit=10000)
        failures=[x for x in events if x["type"]=="tool.operation_finished" and x["data"]["tool_name"]=="process.run"]
        if not failures:return
        last=failures[-1]["data"];result=last["result"]
        if not result.get("is_error"):return
        signature=digest([last["action_digest"],result.get("relevant_state"),result.get("exit_code"),result.get("stderr")])
        repeats=0
        for x in reversed(failures):
            d=x["data"];r=d["result"]
            if digest([d["action_digest"],r.get("relevant_state"),r.get("exit_code"),r.get("stderr")])!=signature:break
            repeats+=1
        cfg=await self.worker.invoke(components["retry_policy"],{"operation":"retry","repeated_failures":repeats,"last_error":result.get("stderr","")[:2000]},run["id"])
        threshold=max(1,min(int(cfg.get("max_identical_failures",3)),10))
        enabled=cfg.get("require_new_evidence",False)
        if enabled and repeats>=threshold:
            key=digest([run["id"],signature,repeats])
            if not self.store.get("hint",key):
                self.store.put("hint",key,{"run_id":run["id"]})
                self.store.append_message(run["id"],{"role":"user","content":"Runtime recovery hint: repeated deterministic failure with unchanged observed state. Obtain new evidence or change the relevant state before retrying; do not blindly repeat. Transient failures require an explicit reason."})

    def _candidate_review(self, run, review, *, limit_reached=False):
        """Continue only for a concrete recovery action with fresh Host evidence."""
        from .work_status import work_status

        ledger = work_status(self.store, self.runtime, run, review_after=review['after_operation'] if review else 0,
                             required_probes=review.get('required_probes', ()) if review else (),
                             repair_checks=review.get('repair_checks') if review else None)
        evidence = ledger['review_evidence']
        has_changes = bool(ledger['changed_path_count'])
        failed_write = evidence['failed_write']
        if not review and not (has_changes or failed_write):
            return False, None
        used = run['data']['budget_used']
        budget = self.runtime.budget_limits(run)
        room = all(budget[key] is None or budget[key] - used.get(key, 0) >= needed
                   for key, needed in [('model_calls', 2), ('tool_calls', 1)])
        pending = evidence['pending_probes']
        pending_repairs = evidence['pending_repairs']
        source = ledger['source_digest']
        focused_check = None
        focus = None
        fingerprint = None
        if not has_changes and failed_write:
            focus = ('APPLY_REPRODUCTION' if review and review.get('focus') in {'NO_SOURCE_CHANGE', 'APPLY_REPRODUCTION'}
                     and evidence['reverted_write'] and evidence['baseline_reproduction'] else 'NO_SOURCE_CHANGE')
            fingerprint = digest([focus, source])
        elif has_changes:
            if pending_repairs:
                focus = 'REPAIR_CHECK'
                key, focused_check = next(iter(pending_repairs.items()))
                fingerprint = digest([focus, source, key, focused_check['source_digest'] == source])
            elif pending:
                focus = 'VERIFY_PROBE'
                probe = pending[0]
                fingerprint = digest([focus, source, probe['operation'], probe['outcome']])
            elif not review or (review.get('focus') in {'NO_SOURCE_CHANGE', 'APPLY_REPRODUCTION'}
                                and not evidence['new_passing_checks']):
                focus = 'BOUNDARY_CHECK'
                fingerprint = digest([focus, source])
        seen = review.get('seen_evidence', []) if review else []
        if focus and room and not limit_reached and fingerprint not in seen:
            status = 'REQUESTED'
        elif not review and not room:
            status = 'INSUFFICIENT_BUDGET'
        elif not has_changes:
            status = 'NO_SOURCE_CHANGE'
        elif pending_repairs or evidence['failing_checks']:
            status = 'CHECK_FAILED' if evidence['failing_checks'] else 'RECHECK_PENDING'
        elif pending:
            status = 'NO_NEW_EVIDENCE' if fingerprint in seen and not limit_reached else 'PROBE_ASSERTIONS_PENDING'
        elif evidence['new_passing_checks'] or evidence['repaired_checks']:
            status = 'NEW_CHECK_PASSED'
        else:
            status = 'NO_NEW_PASSING_CHECK'
        state = {**(review or {'control_epoch': run['control_epoch'], 'after_operation': evidence['after_operation'],
                              'model_calls_at_request': used['model_calls']}),
                 'status': status, 'source_digest': source, 'focus': focus, 'failed_write': failed_write,
                 'reverted_write': evidence['reverted_write'], 'attempted_check_failure': evidence['attempted_check_failure'],
                 'baseline_check_failure': evidence['baseline_check_failure'], 'baseline_reproduction': evidence['baseline_reproduction'],
                 'focused_check': focused_check, 'repair_checks': evidence['repair_checks'],
                 'repaired_checks': evidence['repaired_checks'], 'new_passing_checks': evidence['new_passing_checks'],
                 'failing_checks': evidence['failing_checks'], 'limit_reached': limit_reached,
                 'required_probes': evidence['required_probes'], 'pending_probes': pending,
                 'seen_evidence': (seen + [fingerprint])[-REVIEW_CALL_LIMIT:] if status == 'REQUESTED' else seen}
        with self.store.tx() as c:
            current = self.runtime.run(run['id'], c)
            if (current['state'] != 'RUNNING'  or current['control_epoch'] != run['control_epoch']
                    or current['writer_epoch'] != run['writer_epoch']):
                raise RepoError('STALE_CONTROL')
            self.store.put('candidate_review', run['id'], state, c)
            event = 'candidate.review_requested' if state['status'] == 'REQUESTED' else 'candidate.review_finished'
            self.store.emit(c, run['id'], event, state, run['data']['visibility'])
        if state['status'] == 'REQUESTED':
            return True, None
        if limit_reached and state['status'] != 'NEW_CHECK_PASSED':
            return False, 'CANDIDATE_REVIEW_LIMIT'
        if state['status'] != 'NEW_CHECK_PASSED':
            return False, 'CANDIDATE_REVIEW_INCOMPLETE'
        return False, None

    async def execute(self,run_id):
        run=self.runtime.run(run_id)
        if run["state"] in TERMINAL or run["state"]=='WAITING_USER':return self.report(run_id)
        if run["state"] in {"CONTROL_PENDING","NEEDS_RECONCILIATION","SUSPENDING_FOR_SUCCESSOR"}:return self.report(run_id)
        if run["state"]=="WAITING_APPROVAL":return self.report(run_id)
        components=self.releases.components(run["data"]["manifest"]["release_digest"])
        try:
            while True:
                run=self.runtime.run(run_id)
                self.runtime.assert_executable(run)
                if not await self._pending(run_id):return self.report(run_id)
                run=self.runtime.run(run_id)
                review = self.store.get('candidate_review', run_id)
                if review and (review['control_epoch'] != run['control_epoch'] or review['status'] != 'REQUESTED'):
                    review = None
                if review and run['data']['budget_used']['model_calls'] - review['model_calls_at_request'] >= REVIEW_CALL_LIMIT:
                    _, reason = self._candidate_review(run, review, limit_reached=True)
                    self.runtime.end(run_id, 'PARTIAL', reason or 'CANDIDATE_READY_EXTERNAL_EVALUATION: bounded review complete')
                    return self.report(run_id)
                self.runtime.check_wall(run_id,"prepare")
                await self._retry_hint(run,components)
                config=await self.worker.invoke(components["context_policy"],{"operation":"context_config","run_id":run_id},run_id)
                # Policy may tighten, never exceed the Host maximum context allocation.
                config["max_tokens"]=min(max(int(config.get("max_tokens",24000)),6000),self.context_ceiling)
                # A frozen, signed Host benchmark grant may select a larger
                # context window equally for both arms. It grants no tool or
                # acceptance authority and cannot be changed after dispatch.
                from .benchmark import allocation as benchmark_allocation
                allocation=benchmark_allocation(self.runtime,run)
                if allocation and allocation.get('context_tokens') is not None:
                    config['max_tokens']=allocation['context_tokens']
                config["reserve_tokens"]=min(max(int(config.get("reserve_tokens",4096)),1024),8192)
                tools=definitions(self.broker.available_tools(run))
                if run['data']['manifest'].get('memory_schema_version')!=2:
                    tools=[t for t in tools if t['name'] not in {'memory.read','memory.explain','history.search','history.expand','memory.propose'}]
                elif not self.memory.profile.learning_enabled:tools=[t for t in tools if t['name']!='memory.propose']
                presentation=await self.worker.invoke(components["tools_schema"],{"operation":"tool_descriptions","tool_names":[t["name"] for t in tools]},run_id)
                suffix=str(presentation.get("description_suffix",""))[:4000]
                for tool in tools:
                    tool["description"]=str(presentation.get("descriptions",{}).get(tool["name"],tool["description"]))[:4000]+suffix
                current_goal=self.runtime.current_goal(run)
                memory_cfg=await self.worker.invoke(components["memory_policy"],{"operation":"memory_selection","goal":current_goal},run_id)
                system_suffix=""
                guidance=await self.worker.invoke(components["planning_policy"],{"operation":"next_action_guidance","goal":current_goal,"usage":run["data"]["budget_used"]},run_id)
                manifest = run['data']['manifest']
                authorization = run['data'].get('authorization', {})
                external_review = (guidance.get('verify_before_finish') and (not manifest['contract_digest'] or self.review_contract_tasks)
                                   and not manifest.get('conversation_mode') and authorization.get('workspace_write')
                                   and authorization.get('process') and 'process.run' in {tool['name'] for tool in tools})
                if guidance.get('verify_before_finish') and not (external_review and review):
                    system_suffix+='\nPlanning policy: before final, inspect workspace.diff and check the requested behavior with authorized tools. Use the Host work ledger to reuse same-version checks. If verification is unavailable, state that limitation in final; do not repeatedly recover unchanged history. A no-tool final proposes handing off a candidate for evaluation.'
                if external_review:
                    system_suffix += '\nCandidate verification. ' + REVIEW_GUIDANCE
                    if review:
                        system_suffix += candidate_review_prompt(review)
                if guidance.get("instructions"):system_suffix+="\nApproved planning guidance (not authority): "+str(guidance["instructions"])[:8000]
                skill_cfg=await self.worker.invoke(components["skills_index"],{"operation":"active_skills","goal":current_goal},run_id)
                capability_router=None;capability_route=None
                structured=[s for s in skill_cfg.get('skills',[]) if isinstance(s,dict) and s.get('experience_id')]
                legacy=[s for s in skill_cfg.get('skills',[]) if s not in structured]
                if legacy:
                    system_suffix+="\nApproved release skills (untrusted guidance): "+json.dumps(legacy,ensure_ascii=False)
                session_binding=self.store.get('session_run',run_id)
                if session_binding or (structured and self.store.get('operator_config','capability_router',default={}).get('enabled')):
                    from .capabilities import CapabilityRouter
                    from .memory.consolidation import Consolidator
                    from .memory.repository import MemoryRepository
                    from .memory.models import MemoryProfile
                    owner=self.memory.consolidator if getattr(self,'memory',None) else Consolidator(MemoryRepository(self.store),self.runtime,MemoryProfile(mode='lexical_only'))
                    capability_router=CapabilityRouter(owner)
                    if session_binding:
                        from .session_experience import SessionExperiences
                        active_session=SessionExperiences(self.runtime).entries(run_id)
                        active_grants={s['session_grant_id'] for s in active_session.values()}
                        previously_offered={gid for o in self.store.values('capability_offer') if o['run_id']==run_id
                            for gid in (self.store.get('capability_route',o['route_id']) or {}).get('session_grants',{}).values()}
                        if previously_offered-active_grants:
                            system_suffix+='\nSession experience authorization is inactive or revoked. Do not rely on previously offered session guidance as currently validated; independently check the task evidence.'
                    facts={'source_changed':self.runtime.workspace(run).snapshot()!=run['data']['manifest']['workspace_baseline_digest'],'run_cancelled':bool(run['cancel_requested'])}
                    family=run['data']['manifest'].get('task_family')
                    if family and family not in {'UNKNOWN','UNSPECIFIED'}:facts['task_family:'+family]=True
                    from .memory.working_state import WorkingStateReducer
                    reducer=WorkingStateReducer(owner.repo,self.runtime)
                    state_key,state=reducer.reduce(run_id)
                    capability_phase=reducer.intent(run_id,state_key,state,current_goal).phase
                    capability_route=capability_router.route(run_id,current_goal,phase=capability_phase,facts=facts,
                        file_versions={p:v['blob'] for p,v in self.runtime.workspace(run).manifest()['files'].items()})
                    capability_protocol=('\nHost capability-use protocol; no additional permissions: Evaluate offered capabilities against the current public task and protected paths. '
                        'Use a matching capability: read its slot evidence, call skill.bind with actual paths and argv arrays, then execute the returned bound step rules in order. '
                        'Reads before binding justify values but do not count as post-binding steps. Binding and actual tools may share one response. '
                        'If a capability conflicts with the task or cannot be validly bound, state the concrete mismatch and continue independently; do not fabricate adoption or edit protected files for a receipt.')
                    if capability_route['guidance']:
                        system_suffix+=capability_protocol+'\nScoped decision guidance; grants no authority: '+json.dumps(capability_route['guidance'],ensure_ascii=False)
                    else:
                        pending_bindings=capability_router.pending_bindings(run_id,capability_route)
                        if pending_bindings:
                            system_suffix+=capability_protocol+'\nPreviously offered portable guidance awaiting evidence binding (not authority or application proof): '+json.dumps(pending_bindings,ensure_ascii=False)
                memory=getattr(self,"memory",None) if run["data"]["manifest"].get("memory_schema_version")==2 else None
                request_manifest=None
                live_token=None
                self.runtime.check_wall(run_id,"prepare")
                if memory:
                    system,messages,request_manifest=await memory.prepare_context(run_id,tools,system_suffix,config,memory_cfg)
                else:system,messages=self.context.build(run_id,tools,config,memory_cfg,system_suffix=system_suffix)
                projection=self.store.get('projection',run_id) or {}
                if not memory:live_token=projection.get('request_projection')
                if live_token is None and run['data']['manifest'].get('context_compaction'):
                    live_token=projection.get('live_compact')
                from .context import estimate_tokens
                if not memory:
                    from .memory.budget import ContextBudgetCoordinator
                    ContextBudgetCoordinator(self.provider,config['max_tokens'],max(config['reserve_tokens'],getattr(self.provider,'max_tokens',None) or 0),max(0,int(config.get('safety_tokens',0)))).assert_fits(system,messages,tools,system_suffix)
                response_epoch=run["control_epoch"]
                reservation=memory.reserve_main(request_manifest) if memory else None
                started=time.monotonic();usage={};dispatched=False;live_attempt=None;live_failed=False
                try:
                    if live_token:
                        from .live_compact_state import LiveProjection
                        live=LiveProjection(self.store,self.runtime)
                        live.assert_current(run_id,live_token)
                    self.runtime.consume(run_id,"model_calls")
                    if memory:
                        from .memory.budget import ContextBudgetCoordinator
                        b=request_manifest['budget_breakdown']
                        ContextBudgetCoordinator(self.provider,b['max_effective_window'],b['reserved_output'],b['safety_margin'],b['provider_profile_digest']).assert_fits(system,messages,tools,system_suffix)
                    request_timeout=self.runtime.check_wall(run_id,"dispatch")
                    if live_token:
                        live.assert_current(run_id,live_token)
                        serialized=(self.provider.build_request(system,messages,tools)[0] if hasattr(self.provider,'build_request') else {'system':system,'messages':messages,'tools':tools})
                        live_attempt=live.record_request(run_id,serialized,projection=live_token)
                    dispatched=True
                    deadline = asyncio.timeout(request_timeout)
                    try:
                        async with deadline:
                            reply=await self.provider.complete(system,messages,tools,self.on_delta)
                            if capability_route and capability_route['guidance']:
                                capability_router.mark_offered(capability_route)
                    except TimeoutError as exc:
                        if deadline.expired():
                            raise self.runtime.budget_error(self.runtime.run(run_id),"wall_seconds","model_wait","DISPATCHED_OR_UNKNOWN") from exc
                        raise
                    usage=reply.usage
                except Exception as exc:
                    live_failed=True
                    if isinstance(exc,ProviderNotDispatched):dispatched=False
                    usage=getattr(exc,'usage',usage)
                    with self.store.tx() as c:self.store.emit(c,run_id,'model.failed',{'code':getattr(exc,'code',type(exc).__name__),'usage':usage,'provider_details':getattr(exc,'provider_details',{}),'budget_stop':getattr(exc,'budget_stop',None)},run['data']['visibility'])
                    raise
                finally:
                    if live_attempt:
                        live.settle_request(live_attempt,usage,time.monotonic()-started,
                                            dispatched=dispatched,failed=live_failed)
                    if memory:
                        elapsed=time.monotonic()-started
                        if dispatched:
                            memory.repo.settle(reservation,usage,elapsed)
                            memory.record_usage(request_manifest,usage,elapsed)
                        else:
                            memory.repo.not_dispatched(reservation,'HOST_ADMISSION_REJECTED_BEFORE_PROVIDER')
                            memory.record_usage(request_manifest,{},elapsed,'NOT_DISPATCHED')
                run=self.runtime.run(run_id)
                if run["state"]!="RUNNING":return self.report(run_id)
                if run["control_epoch"]!=response_epoch or (memory and not memory.response_current(request_manifest)):
                    with self.store.tx() as c:self.store.emit(c,run_id,"model.stale_response_discarded",{"usage":reply.usage},run["data"]["visibility"])
                    continue
                if reply.stop_reason in {'length','max_tokens'}:
                    # Reject all partial effects. A fresh complete response must
                    # fit the same output reserve; do not enlarge the window.
                    with self.store.tx() as c:
                        self.store.emit(c,run_id,'model.response_rejected',{'reason':reply.stop_reason,'usage':reply.usage,'effects_admitted':False},run['data']['visibility'])
                        self.store.append_message(run_id,{'role':'user','content':'Runtime response rejected: the last response reached the configured output-token limit. No tool call from it was executed. Keep the next response short; split large edits into smaller calls. Prefer a short test file plus process.run over a long python -c command. Preserve the original task and acceptance requirements.'},c=c)
                    continue
                if reply.stop_reason=='error':raise RepoError('INCOMPLETE_MODEL_RESPONSE')
                turn_id=uid("turn-")
                assistant={"role":"assistant","content":reply.text,"tool_calls":[t.model_dump(exclude_none=True) for t in reply.tool_calls],"native_blocks":reply.native_blocks}
                if reply.reasoning_content is not None:assistant['reasoning_content']=reply.reasoning_content
                if reply.tool_calls:
                    if len({t.id for t in reply.tool_calls})!=len(reply.tool_calls):raise RepoError("DUPLICATE_TOOL_CALL_ID")
                    requests=[]
                    for call in reply.tool_calls:
                        try:requests.append(self.broker.register_call(run_id,turn_id,call).model_dump())
                        except Exception as exc:
                            # Atomic assistant+error pairing below; invalid calls do not execute.
                            invalid={"invalid_call":call.id,"error":getattr(exc,"code","INVALID_ARGUMENT")}
                            if call.argument_error:invalid['details']=[{'field':'arguments','type':'json_invalid','message':call.argument_error}]
                            if isinstance(exc,ValidationError):
                                invalid["details"]=[{"field":".".join(str(p) for p in e["loc"]),"type":e["type"],"message":e["msg"]} for e in exc.errors(include_input=False,include_url=False)[:8]]
                            requests.append(invalid)
                    with self.store.tx() as c:
                        self.store.append_message(run_id,assistant,c=c)
                        invalid=[r for r in requests if "invalid_call" in r]
                        # Preserve original tool result ordering by materializing every result in _pending.
                        self.store.put("pending_turn",run_id,{"turn_id":turn_id,"requests":requests},c)
                        self.store.emit(c,run_id,"model.completed",{"turn_id":turn_id,"usage":reply.usage,"tool_count":len(requests)},run["data"]["visibility"])
                    continue
                self.store.append_message(run_id,assistant)
                with self.store.tx() as c:self.store.emit(c,run_id,"model.completed",{"turn_id":turn_id,"usage":reply.usage,"tool_count":0},run["data"]["visibility"])
                reason = None
                if external_review:
                    at_limit = bool(review and run['data']['budget_used']['model_calls']
                                    - review['model_calls_at_request'] >= REVIEW_CALL_LIMIT)
                    continue_review, reason = self._candidate_review(run, review, limit_reached=at_limit)
                    if continue_review:
                        continue
                if self.runtime.contract(run) is None:
                    if run['data']['manifest'].get('conversation_mode'):
                        self.runtime.wait_for_user(run_id)
                        return self.report(run_id)
                    self.runtime.end(run_id,"PARTIAL",reason or "CANDIDATE_READY_EXTERNAL_EVALUATION: no frozen internal acceptance")
                    return self.report(run_id)
                for server in self.broker.mcp.values():await server.close()
                intent=self.runtime.prepare(run_id);decision=await self.grader.evaluate(intent,self.runtime.contract(run))
                committed=self.runtime.commit(intent,decision)
                if committed["status"] in {"COMMITTED","ALREADY_COMMITTED"}:return self.report(run_id)
                if committed["status"]=="STALE_CONTROL":return self.report(run_id)
                if run["data"]["visibility"]!="DEV":
                    self.runtime.end(run_id,"PARTIAL","INSUFFICIENT_EVIDENCE");return self.report(run_id)
                self.runtime.resume_after_failed_verification(run_id,decision)
                if memory:
                    try:await memory.on_verified_stage(run_id,decision)
                    except Exception as exc:
                        with self.store.tx() as c:self.store.emit(c,run_id,'memory.maintenance_deferred',{'code':getattr(exc,'code',type(exc).__name__)})
                details=[]
                for e in decision.evidence_ids:
                    row=self.store.get("evidence",e)
                    if row["status"]!="PASSED":details.append({"check_id":row["check_id"],"status":row["status"],"observations":self.store.read_json(row["report_digest"])})
                self.store.append_message(run_id,{"role":"user","content":"Registered checks not satisfied: "+json.dumps(details,ensure_ascii=False)[:16000]})
        except RepoError as exc:
            if exc.code in {"CONTROL_PENDING","RUN_NOT_EXECUTABLE","STALE_CONTROL"}:return self.report(run_id)
            stopped=self.broker.sandbox.stop_run(run_id)
            if exc.code in {"RECOVERY_REQUIRED","RESOURCE_LIFETIME_UNKNOWN"} or not stopped:
                self.runtime.recover(run_id)
            else:
                stop = getattr(exc,"budget_stop",None)
                if stop is None and isinstance(exc,ProviderNotDispatched):
                    dimension = {"WALL_LIMIT":"wall_seconds","TOKEN_RESERVATION_UNAVAILABLE":"total_tokens",
                                 "CALL_LIMIT":"model_calls"}.get(exc.provider_details.get("cause"))
                    if dimension:
                        stop = self.runtime.budget_error(self.runtime.run(run_id),dimension,"provider_admission").budget_stop
                self.runtime.end(run_id,"PARTIAL" if exc.code in {"BUDGET_EXHAUSTED","TOTAL_TOKEN_BUDGET_EXHAUSTED","CONTEXT_BUDGET_EXCEEDED","NO_PROGRESS","STAGNATION_AFTER_WRITE"} else "BLOCKED",exc.code,budget_stop=stop)
            return self.report(run_id)
        except TimeoutError:
            self.broker.sandbox.stop_run(run_id)
            self.runtime.end(run_id,"PARTIAL","PROVIDER_TIMEOUT")
            return self.report(run_id)
        except asyncio.CancelledError:
            self.runtime.admit(run_id,uid("cancel-"),"CANCEL");raise
    def report(self,run_id):
        run=self.runtime.run(run_id)
        with contextlib.closing(self.store.connect()) as c:
            d=c.execute("SELECT data FROM deliveries WHERE run_id=?",(run_id,)).fetchone()
        usage={"input_tokens":0,"output_tokens":0,"usage_available":False}
        for event in self.store.events(run_id,trusted=True,limit=10000):
            if event["type"] in {"model.completed","model.stale_response_discarded","model.response_rejected","model.failed"}:
                u=event["data"].get("usage",{});usage["usage_available"] |= bool(u)
                usage["input_tokens"]+=u.get("input_tokens",u.get("prompt_tokens",0)) or 0
                usage["output_tokens"]+=u.get("output_tokens",u.get("completion_tokens",0)) or 0
        result={"usage":usage,"run_id":run_id,"state":run["state"],"reason":run["data"].get("end_reason"),"manifest":run["data"]["manifest"],"budget_used":run["data"]["budget_used"],"workspace":run["data"]["workspace"],"delivery":json.loads(d[0]) if d else None}
        if run["data"].get("budget_stop") is not None:
            result["budget_stop"] = run["data"]["budget_stop"]
        review = self.store.get('candidate_review', run_id)
        if review and review['control_epoch'] == run['control_epoch']:
            result['candidate_review'] = review
        memory=getattr(self,'memory',None)
        if memory and run['data']['manifest'].get('memory_schema_version')==2:
            rows=[r for r in memory.repo.values('memory_budget') if r['run_id']==run_id]
            purposes={}
            for row in rows:
                if row['state']=='NOT_DISPATCHED':continue
                totals=purposes.setdefault(row['purpose'],{'calls':0,'input_tokens':0,'output_tokens':0,'unknown_usage_calls':0,'conservative_reserved_tokens':0,'seconds':0})
                u=row['usage'];totals['calls']+=1;totals['input_tokens']+=u.get('input_tokens',u.get('prompt_tokens',0)) or 0
                totals['output_tokens']+=u.get('output_tokens',u.get('completion_tokens',0)) or 0
                totals['unknown_usage_calls']+=row['state']!='SETTLED'
                if row['state']!='SETTLED':totals['conservative_reserved_tokens']+=row['charged_tokens']
                totals['seconds']+=row.get('seconds',0)
            result['memory_cost']={'by_purpose':purposes,'known_total_tokens':sum(x['input_tokens']+x['output_tokens'] for x in purposes.values()),
                'usage_complete':all(x['unknown_usage_calls']==0 for x in purposes.values()),'estimate_only':True,
                'index_build_records_for_pinned_index':[x for x in memory.repo.values('memory_control') if x.get('index_build') and x.get('index')==run['data']['manifest']['knowledge_index_digest']],
                'retrieval_seconds':sum(t['seconds'] for t in memory.repo.values('retrieval_traces') if t['run_id']==run_id)}
            result['memory_cost']['not_dispatched_reservations']=sum(r['state']=='NOT_DISPATCHED' for r in rows)
        offers=[o for o in self.store.values('capability_offer') if o['run_id']==run_id]
        if offers:
            from .capabilities import CapabilityRouter
            from .memory.consolidation import Consolidator
            from .memory.repository import MemoryRepository
            from .memory.models import MemoryProfile
            owner=memory.consolidator if memory else Consolidator(MemoryRepository(self.store),self.runtime,MemoryProfile(mode='lexical_only'))
            result['application_receipts']=[]
            for offer in offers:
                for ref in offer['contract_refs']:
                    try:
                        key,_=CapabilityRouter(owner).receipt(run_id,ref,route_id=offer['route_id'])
                        result['application_receipts'].append(key)
                    except RepoError as exc:result['application_receipt_error']=exc.code
        return result
