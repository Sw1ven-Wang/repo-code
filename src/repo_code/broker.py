"""Trusted tools: schema/approval/budget/intent/effect/result. Workers submit intent only."""
from __future__ import annotations
import asyncio
import contextlib
import fnmatch
import json
import re
import time
from .models import OperationRequest, UNRESOLVED
from .tool_models import SCHEMAS, READ_TOOLS, definitions
from .util import RepoError, digest, canonical, uid, now, Faults, redact
from .workspace import AnchorMatchError, Mutator, file_lock
from .knowledge import Knowledge
from .progress import ProcessProgress

class Broker:
    def __init__(self,store,runtime,sandbox,provider=None,faults=None):
        self.store,self.runtime,self.sandbox,self.provider=store,runtime,sandbox,provider
        self.faults=faults or Faults();self.mcp={}
        self.progress=ProcessProgress(store,runtime)
    def available_tools(self,run):
        names=set(SCHEMAS)
        if not run['data'].get('benchmark_allocation') or not getattr(self,'benchmark_self_test',None):names.discard('benchmark.self_test')
        if run['data'].get('benchmark_allocation'):names-={'delegate','process.start','mcp.call','skill.bind'}
        if run['data'].get('benchmark_allocation') and not self.sandbox.isolated:names.discard('process.run')
        if not self.store.get('session_run',run['id']) and not self.store.get('operator_config','capability_router',default={}).get('enabled'):
            names.discard('skill.bind')
        if not self.mcp:names-={'mcp.call','mcp.discover'}
        if not run['data'].get('authorization',{}).get('delegate') or not run['data']['manifest']['budget'].get('child_calls'):
            names.discard('delegate')
        if getattr(self.sandbox,'supports_background',True) is False or getattr(self.sandbox,'mode',None)=='swebench':
            names-={'process.start','process.status','process.stop'}
        return names
    def register_call(self,run_id,turn_id,call):
        if self.runtime.run(run_id)['data'].get('benchmark_allocation') and call.name=='process.run' and not self.sandbox.isolated:raise RepoError('BENCHMARK_ISOLATED_PROCESS_REQUIRED')
        if self.runtime.run(run_id)['data'].get('benchmark_allocation') and call.name in {'delegate','process.start','mcp.call','skill.bind'}:raise RepoError('BENCHMARK_TOOL_DISABLED')
        if call.argument_error or call.raw_arguments is not None:raise RepoError('INVALID_TOOL_JSON',call.argument_error or 'Unvalidated raw tool arguments')
        if call.name not in SCHEMAS:raise RepoError("UNKNOWN_TOOL",call.name)
        args=SCHEMAS[call.name][0].model_validate(call.arguments).model_dump()
        if call.name == 'process.run':
            if args.get('check_probe') is None:
                args.pop('check_probe', None)
            if not args.get('expect_workspace_change'):
                args.pop('expect_workspace_change', None)
        if call.name in {'edit','delete','move'} and self.runtime.run(run_id)['data']['manifest'].get('memory_schema_version')==2:
            path=args.get('path',args.get('source'));observed=self.store.get('observation',digest([run_id,path]))
            if not observed or (args.get('expected_version') and args['expected_version']!=observed['version']):raise RepoError('READ_REQUIRED','Read this file in the current Run before using its version for an edit')
        if call.name in {"edit","delete","move"} and not args.get("expected_version"):
            p=args.get("path",args.get("source"));obs=self.store.get("observation",digest([run_id,p]))
            if not obs:raise RepoError("READ_REQUIRED",p)
            args["expected_version"]=obs["version"]
        if call.name=='patch':
            v2=self.runtime.run(run_id)['data']['manifest'].get('memory_schema_version')==2
            for op in args['operations']:
                if op['kind']=='create':continue
                path=op.get('path',op.get('source'));obs=self.store.get('observation',digest([run_id,path]))
                if not obs or (v2 and op.get('expected_version') and op['expected_version']!=obs['version']):raise RepoError('READ_REQUIRED','Read this file in the current Run before patching it')
                if not op.get('expected_version'):op['expected_version']=obs['version']
        request=OperationRequest(run_id=run_id,turn_id=turn_id,tool_call_uid=call.id,tool_name=call.name,
            tool_spec_digest=digest(definitions([call.name])[0]),arguments=args,expected_state_digest=args.get("expected_version"))
        existing=self.store.get("host_call",request.key)
        if existing and existing["semantics"]!=request.semantics:raise RepoError("IDEMPOTENCY_CONFLICT")
        self.store.put("host_call",request.key,{"request":request.model_dump(),"semantics":request.semantics},immutable=True)
        return request
    def receipt(self,row):
        return {"operation_id":row["id"],"operation_key":row["op_key"],"status":row["state"],"result":json.loads(row["result"]) if row["result"] else None}
    def get(self,op_key,run_id=None):
        with contextlib.closing(self.store.connect()) as c:
            row=c.execute("SELECT * FROM operations WHERE op_key=?",(op_key,)).fetchone()
            if not row:raise RepoError("OPERATION_NOT_FOUND")
            if run_id and row["run_id"]!=run_id:raise RepoError("OPERATION_SCOPE_DENIED")
            return self.receipt(row)
    def allow(self,run,tool):
        if tool=='benchmark.self_test':return bool(run['data'].get('benchmark_allocation') and getattr(self,'benchmark_self_test',None))
        if run['data'].get('benchmark_allocation') and tool in {'delegate','process.start','mcp.call','skill.bind'}:return False
        if tool in READ_TOOLS:return True
        config=run["data"].get("authorization",{})
        if tool in {"create","edit","patch"}:return bool(config.get("workspace_write"))
        if tool in {"process.run","process.start","process.stop"}:return bool(config.get("process"))
        if tool=="delegate":return bool(config.get("delegate"))
        return False
    def authorize_run(self,run_id,workspace_write=False,process=False,delegate=False):
        with self.store.tx() as c:
            r=self.runtime.run(run_id,c);r["data"]["authorization"]={"workspace_write":workspace_write,"process":process,"delegate":delegate}
            self.runtime.update(c,r,data=r["data"])
    def approve(self,op_key,approve=True):
        with self.store.tx() as c:
            row=c.execute("SELECT * FROM operations WHERE op_key=?",(op_key,)).fetchone()
            if not row or row["state"]!="WAITING_APPROVAL":raise RepoError("STALE_APPROVAL")
            run=self.runtime.run(row["run_id"],c)
            if run["cancel_requested"] or run["state"] not in {"RUNNING","WAITING_APPROVAL"}:raise RepoError("STALE_APPROVAL")
            if now()-row["created"]>86400:raise RepoError("APPROVAL_EXPIRED")
            c.execute("UPDATE operations SET state=?,approved_epoch=? WHERE id=?",("AUTHORIZED" if approve else "CANCELLED",run["control_epoch"],row["id"]))
            self.store.emit(c,run["id"],"approval.resolved",{"operation_id":row["id"],"approved":approve},run["data"]["visibility"])
            if run["state"]=="WAITING_APPROVAL":self.runtime.update(c,run,state="RUNNING")
    async def submit(self,req: OperationRequest):
        stored=self.store.get("host_call",req.key)
        if not stored:raise RepoError("UNREGISTERED_HOST_CALL")
        if stored["semantics"]!=req.semantics:raise RepoError("IDEMPOTENCY_CONFLICT")
        if req.tool_name not in SCHEMAS or req.tool_spec_digest!=digest(definitions([req.tool_name])[0]):raise RepoError("TOOL_SCHEMA_DRIFT")
        SCHEMAS[req.tool_name][0].model_validate(req.arguments)
        with self.store.tx() as c:
            row=c.execute("SELECT * FROM operations WHERE op_key=?",(req.key,)).fetchone()
            if row:
                if row["semantics"]!=req.semantics:raise RepoError("IDEMPOTENCY_CONFLICT")
                if row["state"] not in {"REGISTERED","AUTHORIZED"}:return self.receipt(row)
            else:
                run=self.runtime.run(req.run_id,c)
                if run["state"]!="RUNNING" or run["cancel_requested"]:raise RepoError("RUN_NOT_EXECUTABLE")
                op_id=uid("op-")
                c.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)",(op_id,req.key,req.run_id,"REGISTERED",req.semantics,canonical(req).decode(),None,None,now()))
                self.store.emit(c,req.run_id,"operation.registered",{"operation_id":op_id,"tool":req.tool_name},run["data"]["visibility"])
        self.faults.hit("registered_before_execution")
        run=self.runtime.run(req.run_id);self.runtime.assert_executable(run)
        no_progress=self.progress.rejection(req)
        budget_error=None
        budget_stop=None
        with self.store.tx() as c:
            row=c.execute("SELECT * FROM operations WHERE op_key=?",(req.key,)).fetchone();run=self.runtime.run(req.run_id,c)
            if row["state"] not in {"REGISTERED","AUTHORIZED"}:return self.receipt(row)
            if run["state"]!="RUNNING" or run["cancel_requested"]:raise RepoError("RUN_NOT_EXECUTABLE")
            if c.execute("SELECT 1 FROM control WHERE run_id=? AND status LIKE 'PENDING%'",(run["id"],)).fetchone():raise RepoError("CONTROL_PENDING")
            allowed=self.allow(run,req.tool_name) or (row["state"]=="AUTHORIZED" and row["approved_epoch"]==run["control_epoch"])
            if not allowed:
                c.execute("UPDATE operations SET state='WAITING_APPROVAL' WHERE id=?",(row["id"],));self.runtime.update(c,run,state="WAITING_APPROVAL")
                return {"operation_id":row["id"],"operation_key":req.key,"status":"WAITING_APPROVAL","result":None}
            # Reserve exactly once while owning the operation state transition.
            used=run["data"]["budget_used"];limit=self.runtime.budget_limits(run,c)
            from .benchmark import check_budget
            allocation=None
            try:allocation=check_budget(self.runtime,run,'tool_calls',1,c)
            except RepoError as exc:
                if exc.code!='BUDGET_EXHAUSTED':raise
                exc.budget_stop['allocation_scope']=exc.budget_stop['stage']
                exc.budget_stop['stage']='tool'
                budget_stop=exc
            total=c.execute("SELECT total_calls FROM tasks WHERE id=?",(run["task_id"],)).fetchone()[0]
            if self.runtime.active_seconds(run)>=limit["wall_seconds"]:
                budget_stop = self.runtime.budget_error(run,"wall_seconds","tool")
            elif used["tool_calls"]>=limit["tool_calls"]:
                budget_stop = self.runtime.budget_error(run,"tool_calls","tool")
            if budget_stop:
                budget_error = budget_stop.code
            elif total>=(allocation['session_model_calls']+allocation['session_tool_calls'] if allocation else 1000):budget_error='TASK_TOTAL_BUDGET'
            if no_progress and not budget_error:
                c.execute("UPDATE operations SET state='CANCELLED',approved_epoch=NULL,result=? WHERE id=?",(canonical(no_progress).decode(),row['id']))
                self.store.emit(c,req.run_id,'operation.no_progress_rejected',{'operation_id':row['id'],**no_progress},run['data']['visibility'])
                return {'operation_id':row['id'],'operation_key':req.key,'status':'CANCELLED','result':no_progress}
            if budget_error:
                # Commit proof of non-dispatch before raising. Raising inside
                # this transaction rolls back cancellation and leaves REGISTERED.
                result={'is_error':True,'code':budget_error,'executed':False}
                if budget_stop:
                    result['budget_stop'] = budget_stop.budget_stop
                c.execute("UPDATE operations SET state='CANCELLED',approved_epoch=NULL,result=? WHERE id=?",(canonical(result).decode(),row['id']))
                self.store.emit(c,req.run_id,'operation.budget_rejected',{'operation_id':row['id'],**result},run['data']['visibility'])
            else:
                c.execute("UPDATE tasks SET total_calls=total_calls+1 WHERE id=?",(run["task_id"],))
                used["tool_calls"]+=1
                self.runtime.update(c,run,data=run["data"])
                c.execute("UPDATE operations SET state='EXECUTING',approved_epoch=? WHERE id=? AND state IN ('REGISTERED','AUTHORIZED')",(run["control_epoch"],row["id"]))
                op_id=row["id"]
        if budget_stop:
            raise budget_stop
        if budget_error:raise RepoError(budget_error)
        result=None
        try:
            # File writes re-check control state under the writer lock, after authorization.
            if req.tool_name in {"create","edit","delete","move","patch"}:
                result=await asyncio.to_thread(self._file_effect,req,op_id)
            else:result=await self._execute(req,op_id)
            self.faults.hit("effect_before_result_commit")
            state="FAILED" if result.get("is_error") else "SUCCEEDED"
        except AnchorMatchError as exc:
            state = "FAILED"
            result = {"is_error": True, "code": exc.code, "message": exc.message, **exc.anchor_details}
            if exc.code == "EDIT_ANCHOR_RETRY_BLOCKED":
                result["executed"] = False
        except RepoError as exc:
            state="UNCERTAIN" if exc.code in {"RECOVERY_REQUIRED","RESOURCE_LIFETIME_UNKNOWN","MCP_EFFECT_UNKNOWN"} else "FAILED"
            result={"is_error":True,"code":exc.code,"message":redact(exc.message)[:1000]}
        except Exception as exc:
            state="UNCERTAIN";result={"is_error":True,"code":"INTERNAL_ERROR","error_type":type(exc).__name__}
        except BaseException:
            with self.store.tx() as c:
                c.execute("UPDATE operations SET state='UNCERTAIN' WHERE id=?",(op_id,))
                interrupted = self.runtime.run(req.run_id, c)
                if interrupted["state"] not in {"SUCCEEDED","FAILED","CANCELLED","SUPERSEDED","PARTIAL","ANSWERED"}:
                    self.runtime.update(c, interrupted, state="NEEDS_RECONCILIATION")
            raise
        result=self.progress.record(req,result)
        result=self._bound_result(req.run_id,result)
        with self.store.tx() as c:
            c.execute("UPDATE operations SET state=?,result=? WHERE id=?",(state,canonical(result).decode(),op_id))
            run=self.runtime.run(req.run_id,c)
            if state == "UNCERTAIN" and run["state"] not in {"SUCCEEDED","FAILED","CANCELLED","SUPERSEDED","PARTIAL","ANSWERED"}:
                self.runtime.update(c,run,state="NEEDS_RECONCILIATION")
            self.store.emit(c,req.run_id,"tool.operation_finished",{"operation_id":op_id,"tool_name":req.tool_name,"action_digest":digest([req.tool_name,req.arguments]),"writer_epoch":run["writer_epoch"],"result":result},run["data"]["visibility"])
        self.runtime.settle_controls(req.run_id)
        self.faults.hit("result_committed_before_reply")
        return self.get(req.key)
    def _bound_result(self,run_id,result):
        raw=canonical(result);artifact=self.store.blob(raw)
        self.store.put("run_artifact",digest([run_id,artifact]),{"run_id":run_id,"artifact":artifact})
        if len(raw)>24000:
            return {"is_error":result.get("is_error",False),"code":result.get("code"),"exit_code":result.get("exit_code"),"artifact":artifact,"truncated":True,"preview":raw[:12000].decode(errors="replace"),"bytes":len(raw),**{k:result[k] for k in ('process_progress','inspection_progress','observed_probe','probe_print_arity','checked_probe') if result.get(k)}}
        return {**result,"result_artifact":artifact}
    def _file_effect(self,req,op_id):
        run=self.runtime.run(req.run_id);self.runtime.assert_executable(run)
        args=dict(req.arguments)
        if req.tool_name=="patch":ops=args["operations"]
        else:ops=[{"kind":req.tool_name,**args}]
        # Mutator holds the cross-process workspace lock. State is checked immediately before effects.
        mutator=Mutator(self.runtime.workspace(run),run["id"],self.runtime.contract(run),self.faults,
            effect_guard=lambda _: self.runtime.file_effect_guard(run["id"], op_id))
        if getattr(self,'write_validator',None):self.write_validator(run,mutator.ws,mutator.plan(ops))
        from .benchmark import write_scope
        write_scope(self.runtime,run,mutator.plan(ops))
        result=mutator.apply(op_id,ops)
        with self.store.tx() as c:
            fresh=self.runtime.run(run["id"],c);self.runtime.update(c,fresh,writer=True)
        for p,v in result["versions"].items():
            if v:self.store.put("observation",digest([run["id"],p]),{"path":p,"version":v})
        return result
    def _import_changes(self,run,op_id,snapshot,authority_operation_id=None):
        ws=self.runtime.workspace(run);old=ws.manifest()["files"];new=self.store.read_json(snapshot)["files"];ops=[]
        for p in sorted(set(old)|set(new)):
            if old.get(p)==new.get(p):continue
            if p not in new:ops.append({"kind":"delete","path":p,"expected_version":old[p]["blob"]})
            elif p not in old:ops.append({"kind":"create","path":p,"content":self.store.read_blob(new[p]["blob"])})
            else:ops.append({"kind":"replace","path":p,"content":self.store.read_blob(new[p]["blob"]),"expected_version":old[p]["blob"]})
        if ops:
            mutator=Mutator(ws,run["id"],self.runtime.contract(run),self.faults,
                effect_guard=lambda _: self.runtime.file_effect_guard(run["id"], authority_operation_id or op_id))
            if getattr(self,'write_validator',None):self.write_validator(run,ws,mutator.plan(ops))
            from .benchmark import write_scope
            write_scope(self.runtime,run,mutator.plan(ops))
            mutator.apply(op_id,ops)
            with self.store.tx() as c:self.runtime.update(c,self.runtime.run(run["id"],c),writer=True)
    def _probe_check_command(self, req):
        from .commands import probe_assertion_argv, canonical_python
        from .work_status import full_result
        args = req.arguments
        ref = args['check_probe']
        with contextlib.closing(self.store.connect()) as conn:
            row = conn.execute("SELECT request,result,approved_epoch FROM operations WHERE id=? AND run_id=? AND state='SUCCEEDED'",
                               (ref, req.run_id)).fetchone()
        if not row:
            raise RepoError("PROBE_REFERENCE_UNAVAILABLE", "Copy the completed observed_probe operation ID into check_probe. Put the assertion code in argv=['python','-c','assert ...'].")
        source = json.loads(row['request'])
        receipt = full_result(self.store, req.run_id, json.loads(row['result']))
        if (source['tool_name'] != 'process.run' or receipt.get('observed_probe') != ref
                or receipt.get('environment_digest') != self.sandbox.environment_digest
                or row['approved_epoch'] != self.runtime.run(req.run_id)['control_epoch']):
            raise RepoError("PROBE_REFERENCE_UNAVAILABLE", "Use a completed observed_probe from this environment.")
        original = canonical_python(source['arguments']['argv'], source['arguments'].get('stdin', ''))
        assertion = canonical_python(args['argv'], args.get('stdin', ''))
        command = probe_assertion_argv(original, assertion) if original and assertion else None
        if command is None:
            raise RepoError("PROBE_ASSERTION_REQUIRED", "Supply only Python assert statements using observed, with no stdin override.")
        return command, ''

    def _native_binding_handles(self,run,query):
        """Discovery of already offered guidance, never Draft activation."""
        if (not self.store.get('session_run',run['id'])
                and not self.store.get('operator_config','capability_router',default={}).get('enabled')):
            return []
        from .capabilities import CapabilityRouter
        from .memory.consolidation import Consolidator
        from .memory.repository import MemoryRepository
        from .memory.models import MemoryProfile
        from .memory.working_state import WorkingStateReducer
        memory=getattr(self,'memory',None)
        owner=memory.consolidator if memory else Consolidator(MemoryRepository(self.store),self.runtime,MemoryProfile(mode='lexical_only'))
        ws=self.runtime.workspace(run)
        facts={'source_changed':ws.snapshot()!=run['data']['manifest']['workspace_baseline_digest'],
               'run_cancelled':bool(run['cancel_requested'])}
        family=run['data']['manifest'].get('task_family')
        if family and family not in {'UNKNOWN','UNSPECIFIED'}:facts['task_family:'+family]=True
        reducer=WorkingStateReducer(owner.repo,self.runtime);key,state=reducer.reduce(run['id'])
        phase=reducer.intent(run['id'],key,state,self.runtime.current_goal(run)).phase
        router=CapabilityRouter(owner)
        route=router.route(run['id'],query or self.runtime.current_goal(run),phase=phase,facts=facts,
                           file_versions={p:v['blob'] for p,v in ws.manifest()['files'].items()})
        return router.pending_bindings(run['id'],route)

    async def _execute(self,req,op_id):
        run=self.runtime.run(req.run_id);ws=self.runtime.workspace(run);a=req.arguments;n=req.tool_name
        if n=='benchmark.self_test':
            from .benchmark import active_stage,allocation
            if not allocation(self.runtime,run) or not getattr(self,'benchmark_self_test',None):raise RepoError('BENCHMARK_SELF_TEST_UNAVAILABLE')
            stage=active_stage(self.runtime,run)
            if a['stage_id']!=stage['body']['stage_id']:raise RepoError('BENCHMARK_STAGE_SCOPE_DENIED')
            return await self.benchmark_self_test(req.run_id,a['stage_id'],op_id)
        if n=='workspace.diff':
            return ws.diff_view(run['data']['manifest']['workspace_baseline_digest'],a['path'],a['offset'],a['length'])
        if n=="read":
            result=ws.read(a["path"]);lines=result["content"].splitlines();start=a["start"]-1
            self.store.put("observation",digest([run["id"],a["path"]]),{"path":a["path"],"version":result["version"]})
            self.store.put("run_artifact",digest([run["id"],result["artifact"]]),{"run_id":run["id"],"artifact":result["artifact"]})
            return {**result,"content":"\n".join(f"{i+1}: {lines[i]}" for i in range(start,min(start+a["lines"],len(lines)))),"total_lines":len(lines)}
        if n in {"glob","search"}:
            pattern=a.get("pattern",a.get("glob","**/*"));files=[p for p in ws.manifest()["files"] if fnmatch.fnmatchcase(p,pattern) or (pattern.startswith("**/") and fnmatch.fnmatchcase(p,pattern[3:]))]
            if n=="glob":return {"paths":files[:a["limit"]],"truncated":len(files)>a["limit"]}
            matches=[]
            for p in files:
                try:lines=ws.path(p).read_text().splitlines()
                except UnicodeError:continue
                for i,line in enumerate(lines):
                    if a["query"] in line:matches.append({"path":p,"line":i+1,"text":line[:2000]})
                    if len(matches)>=a["limit"]:return {"matches":matches,"truncated":True}
            return {"matches":matches,"truncated":False}
        if n=="artifact.read":
            if not self.store.get("run_artifact",digest([run["id"],a["artifact"]])):raise RepoError("ARTIFACT_SCOPE_DENIED")
            if run['data'].get('benchmark_allocation'):
                from .memory.repository import MemoryRepository
                MemoryRepository(self.store).check([a['artifact']])
            if ((run['data']['manifest'].get('context_compaction') or {}).get('mode') in {'live-experimental-v2','live-additive'}
                    or self.store.get('active_request_projection',run['id'])):
                from .live_compact_state import LiveProjection
                LiveProjection(self.store,self.runtime)._reference(run['id'],a['artifact'])
            if run['data']['manifest'].get('memory_schema_version')==2:
                memory=getattr(self,'memory',None)
                if memory is None:raise RepoError('MEMORY_PROFILE_REQUIRED')
                memory.assert_run_profile(run['id']);memory.repo.check([a['artifact']])
            data=self.store.read_blob(a["artifact"]);start=min(a['offset'],len(data));end=min(start+a['length'],len(data))
            # Page original bytes even when JSON escaping expands the slice.
            # Re-archiving a wrapper here can create an endless wrapper chain.
            while True:
                result={'content':data[start:end].decode(errors='replace'),'bytes':len(data),'source_artifact':a['artifact'],
                        'offset':start,'next_offset':end if end<len(data) else None,'returned_bytes':end-start,'truncated':end<len(data)}
                if len(canonical(result))<=20000:return result
                end=start+max(1,(end-start)//2)
        if n=='skill.bind':
            from .capabilities import CapabilityRouter
            from .capability_binding import bind
            from .memory.consolidation import Consolidator
            from .memory.repository import MemoryRepository
            from .memory.models import MemoryProfile
            owner=Consolidator(MemoryRepository(self.store),self.runtime,MemoryProfile(mode='lexical_only'))
            return bind(CapabilityRouter(owner),run['id'],**a)
        if n in {"memory.search","skill.search"}:
            m=run["data"]["manifest"]
            if m.get("memory_schema_version")==2:
                memory=getattr(self,"memory",None)
                if memory is None:raise RepoError("MEMORY_PROFILE_REQUIRED")
                hits,trace=await memory.search(run["id"],a.get("query"),a["top_k"],a.get("token_budget"))
                if n=="skill.search":hits=[h for h in hits if h["kind"] in {"skill","PROCEDURE"}]
                result={"matches":hits,"trace_id":trace,"mode":memory.profile.mode}
            else:
                result={"matches":Knowledge(self.store).search(m["knowledge_snapshot_digest"],m["knowledge_index_digest"],a.get("query") or self.runtime.current_goal(run),a["top_k"],"skill" if n=="skill.search" else None)}
            handles=self._native_binding_handles(run,a.get('query'))
            if handles and n=='skill.search':
                native=[{'id':h['contract_ref'],'kind':'PORTABLE_DECISION_CAPABILITY','status':'OFFERED_UNBOUND',
                         **h,'grants_authority':False} for h in handles]
                result['matches']=(native+result['matches'])[:a['top_k']]
            elif handles:
                result['skill_search_hint']='An offered portable capability is available for this Run. Use skill.search to obtain its evidence-binding handle; knowledge matches do not enumerate native runtime capabilities.'
            return result
        if n in {"memory.read","memory.explain","history.search","history.expand","memory.propose"}:
            memory=getattr(self,"memory",None)
            if memory is None or run["data"]["manifest"].get("memory_schema_version")!=2:raise RepoError("MEMORY_PROFILE_REQUIRED")
            if n=="memory.read":return memory.repo.read_claim(run,a["memory_id"],a["version"])
            if n=="memory.explain":
                traces=[t for t in memory.repo.values("retrieval_traces") if t["run_id"]==run["id"] and a["hit_id"] in t["selected"]]
                if not traces:raise RepoError("MEMORY_HIT_SCOPE_DENIED")
                trace=traces[-1];memory.repo.check([digest(trace)])
                return {"hit_id":a["hit_id"],"trace":trace,"authority":"UNTRUSTED_DATA"}
            if n=="history.search":return {"matches":memory.history.search(run["id"],a["query"],a["limit"])}
            if n=="history.expand":return memory.history.expand(run["id"],a["reference"],a["start"],a["length"])
            if not memory.profile.learning_enabled:raise RepoError('MEMORY_LEARNING_DISABLED')
            claims=memory.consolidator.propose(run["id"],a["claims"],a["evidence_refs"])
            await asyncio.to_thread(memory.consolidator.compare_semantic,run['id'],claims)
            head=memory.repo.get("snapshot_heads",run["data"]["manifest"]["scope"])
            return {"candidates":memory.consolidator.commit(claims,expected_head=head),"activated":False}
        if n=="plan.update":
            self.store.put("plan",run["id"],{"steps":a["steps"],"turn_id":run['data'].get('active_turn',{}).get('turn_id')})
            return {"updated":True}
        if n in {"process.run","process.start"}:
            if run['data'].get('benchmark_allocation'):
                remaining=self.runtime.check_wall(run['id'],'tool')
                a={**a,'timeout':min(a['timeout'],remaining)}
            from .commands import git_inspections, inline_observation
            if getattr(self.sandbox,'mode',None)=='swebench' and git_inspections(a['argv']):
                return {'is_error':True,'code':'GIT_METADATA_UNAVAILABLE','executed':False,
                        'message':'This task copy has no Git metadata. Use workspace.diff for your changes relative to the public baseline; history and refs are not available. Normal tests and dependency commands remain available.'}
            if not self.sandbox.resources_stopped(run["id"]):raise RepoError("WORKSPACE_BUSY","stop or collect existing process first")
            # Build/test may modify files, hence process effects use the same versioned import path.
            baseline=ws.snapshot();self.store.put("process_baseline",op_id,{"snapshot":baseline})
            if n=="process.start":
                lease=await asyncio.to_thread(self.sandbox.start,run["id"],ws.root,a["argv"],a["stdin"],a["timeout"],False,False)
                self.store.put("process_origin",lease,{"operation_id":op_id,"baseline":baseline,"imported":False})
                return {"lease_id":lease,"running":True}
            probe_ref = a.get("check_probe")
            argv, stdin = self._probe_check_command(req) if probe_ref else (a["argv"], a["stdin"])
            completion_marker = None
            independent_ref = None
            if probe_ref:
                completion_marker = '__REPO_CODE_CHECK_COMPLETED_' + uid('')
                argv = [argv[0], argv[1], argv[2] + '\nprint(' + repr(completion_marker) + ')\n']
            elif not a.get('expect_workspace_change'):
                from .commands import independent_assertion_command
                token = '__REPO_CODE_CHECK_COMPLETED_' + uid('')
                independent = independent_assertion_command(argv,stdin,token)
                if independent:
                    argv,stdin = independent,''
                    completion_marker = token
                    independent_ref = 'local-check:' + digest([run['id'],a['argv'],a.get('stdin','')])
            result=await self.sandbox.run(run["id"],ws.root,argv,stdin,a["timeout"],False,cancelled=lambda:self.runtime.run(run["id"])["cancel_requested"])
            if result.get("output_snapshot") and not result["cancelled"]:
                if ws.snapshot()!=baseline:raise RepoError("RECOVERY_REQUIRED","workspace changed during process")
                try:
                    self._import_changes(self.runtime.run(run["id"]),op_id,result["output_snapshot"])
                except RepoError as exc:
                    if getattr(self.sandbox,'mode',None) != 'swebench' or not exc.code.startswith(('FIXTURE_','PATCH_TEST_OR_JUDGE_','PATCH_CONTROL_PATH_')):
                        raise
                    # Preserve diagnostics from the process. Its files were not
                    # admitted and its status is not a check of the Host tree.
                    return {"is_error":True,"code":exc.code,"message":redact(exc.message)[:1000],
                            "process_exit_code":result["exit_code"],"stdout":result.get("stdout",""),
                            "stderr":result.get("stderr",""),"import_rejected":True}
            receipt = {**result,"is_error":result["exit_code"]!=0,"code":"PROCESS_FAILED" if result["exit_code"] else None,"relevant_state":baseline}
            if (a.get("expect_workspace_change") and result["exit_code"] == 0
                    and not any(result.get(k) for k in ("timeout", "cancelled", "running"))
                    and result.get("output_snapshot") == baseline):
                receipt.update(is_error=True, code="WORKSPACE_WRITE_NO_EFFECT",
                               message="The command exited 0 and the workspace is unchanged. Inspect the intended target and use a versioned edit or patch.")
            if probe_ref or independent_ref:
                if probe_ref:receipt["checked_probe"] = probe_ref
                else:receipt['independent_check'] = independent_ref
                suffix = completion_marker + '\n'
                receipt['assertions_completed'] = receipt.get('stdout','').endswith(suffix)
                if receipt['assertions_completed']:
                    receipt['stdout'] = receipt['stdout'][:-len(suffix)]
                elif receipt['exit_code'] == 0:
                    receipt.update(is_error=True, code='PROBE_CHECK_INCOMPLETE',
                                   message='The process exited before the bound assertions completed.')
                from .execution_evidence import bind_execution
                receipt['execution_evidence'] = bind_execution(
                    self.store, run, op_id, a, argv, stdin, receipt, probe_ref or independent_ref)
            elif (not receipt["is_error"] and not any(result.get(k) for k in ("timeout", "cancelled", "running"))
                  and result.get("output_snapshot") == baseline):
                from .commands import canonical_python
                normalized = canonical_python(argv, stdin)
                observation = inline_observation(normalized) if normalized else None
                if observation is not None:
                    receipt["observed_probe"] = op_id
                    receipt["probe_print_arity"] = [len(call.args) for _, call in observation[1]]
            return receipt
        if n in {"process.status","process.stop"}:
            lease=self.store.get("lease",a["lease_id"])
            if not lease or lease["run_id"]!=run["id"]:raise RepoError("LEASE_SCOPE_DENIED")
            result=self.sandbox.status(a["lease_id"],n=="process.stop")
            origin=self.store.get("process_origin",a["lease_id"])
            if origin and not origin["imported"] and not result.get("running") and result.get("output_snapshot"):
                if ws.snapshot()!=origin["baseline"]:raise RepoError("RECOVERY_REQUIRED")
                self._import_changes(run,origin["operation_id"],result["output_snapshot"],authority_operation_id=op_id)
                origin["imported"]=True;self.store.put("process_origin",a["lease_id"],origin)
            return result
        if n=="delegate":return await self._delegate(run,a)
        if n=="mcp.discover":
            servers={k:v for k,v in self.mcp.items() if a["server"] is None or k==a["server"]}
            return {"servers":{k:{"tools":list(v.tools.values())[:100],"protocol":"2025-06-18-legacy-stdio"} for k,v in servers.items()}}
        if n=="mcp.call":
            server=self.mcp.get(a["server"])
            if not server:raise RepoError("MCP_NOT_CONFIGURED")
            if server.process is None or server.process.returncode is not None:await server.start()
            result = await server.call(a["tool"],a["arguments"],operation_id=op_id)
            return {**result, "is_error":bool(result.get("isError")),
                    "code":"MCP_TOOL_ERROR" if result.get("isError") else None}
        raise RepoError("UNKNOWN_TOOL")
    async def _delegate(self,run,args):
        if self.provider is None:raise RepoError("MODEL_REQUIRED")
        self.runtime.consume(run["id"],"child_calls")
        from .governance import Releases
        from .evolved import EvolvedWorker
        component=Releases(self.store,self.sandbox).components(run["data"]["manifest"]["release_digest"])["delegation_policy"]
        policy=await EvolvedWorker(self.store,self.sandbox).invoke(component,{"operation":"delegate","goal":args["goal"]},run["id"])
        turns=min(args["max_turns"],max(1,min(int(policy.get("max_turns",3)),8)))
        child_id=uid("child-");messages=[{"role":"user","content":args["goal"]}];readonly={"read","search","glob"};trace=[]
        for i in range(turns):
            current=self.runtime.run(run['id']);self.runtime.assert_executable(current)
            memory=getattr(self,'memory',None) if run['data']['manifest'].get('memory_schema_version')==2 else None
            system="Read-only explorer. Return sourced findings and unresolved questions. No writing, processes, delegation or external tools.\nApproved guidance: "+str(policy.get("instructions",""))[:3000]
            tools=definitions(readonly);reservation=None;dispatched=False;usage={};started=time.monotonic()
            if memory:
                from .memory.budget import ContextBudgetCoordinator
                memory.assert_run_profile(run['id']);m=run['data']['manifest'];memory.repo.check([m['knowledge_snapshot_digest'],m['knowledge_index_digest']])
                coordinator=ContextBudgetCoordinator.auxiliary(self.provider,memory.profile)
                budget=coordinator.assert_fits(system,messages,tools)
                reservation=memory.repo.reserve(run['id'],'delegate',budget.estimated_input_total,budget.reserved_output,memory.profile.auxiliary_calls,memory.profile.total_token_budget)
                fence=(current['control_epoch'],current['writer_epoch'],memory.repo.epoch())
            try:
                self.runtime.consume(run['id'],'model_calls');request_timeout=self.runtime.remaining_seconds(run['id'])
                if memory:coordinator.assert_fits(system,messages,tools)
                dispatched=True
                reply=await asyncio.wait_for(self.provider.complete(system,messages,tools),timeout=request_timeout);usage=reply.usage
            except Exception as exc:
                usage=getattr(exc,'usage',usage);raise
            finally:
                if memory:
                    if dispatched:memory.repo.settle(reservation,usage,time.monotonic()-started)
                    else:memory.repo.not_dispatched(reservation,'HOST_ADMISSION_REJECTED_BEFORE_PROVIDER')
            if reply.stop_reason in {'length','max_tokens','error'}:raise RepoError('INCOMPLETE_MODEL_RESPONSE')
            current=self.runtime.run(run['id']);self.runtime.assert_executable(current)
            if memory and (current['control_epoch'],current['writer_epoch'],memory.repo.epoch())!=fence:raise RepoError('MEMORY_REQUEST_STALE')
            messages.append({"role":"assistant","content":reply.text,"tool_calls":[c.model_dump() for c in reply.tool_calls],"native_blocks":reply.native_blocks})
            if reply.reasoning_content is not None:messages[-1]['reasoning_content']=reply.reasoning_content
            if not reply.tool_calls:
                self.store.put("child",child_id,{"run_id":run["id"],"trace":trace,"result":reply.text});return {"child_id":child_id,"result":reply.text}
            for call in reply.tool_calls:
                if call.name not in readonly:raise RepoError("CHILD_CAPABILITY_DENIED")
                req=self.register_call(run["id"],child_id+str(i),call);result=await self.submit(req);trace.append(result["operation_id"])
                messages.append({"role":"tool","tool_call_id":call.id,"content":json.dumps(result["result"],ensure_ascii=False)})
        return {"child_id":child_id,"partial":True,"reason":"CHILD_BUDGET_EXHAUSTED"}
    def append_result(self,req,receipt):
        if receipt["status"] not in {"SUCCEEDED","FAILED","CANCELLED"}:return
        content=receipt["result"] or {"is_error":True,"code":receipt["status"]}
        self.store.append_message(req.run_id,{"role":"tool","tool_call_id":req.tool_call_uid,"content":json.dumps(content,ensure_ascii=False),"is_error":content.get("is_error",False)},receipt["operation_id"])
    def reconcile(self,op_key):
        with contextlib.closing(self.store.connect()) as c:row=c.execute("SELECT * FROM operations WHERE op_key=?",(op_key,)).fetchone()
        if not row or row["state"]!="UNCERTAIN":raise RepoError("RECONCILIATION_NOT_REQUIRED")
        run=self.runtime.run(row["run_id"]);req=OperationRequest.model_validate_json(row["request"])
        if req.tool_name not in {"create","edit","patch","delete","move"}:raise RepoError("RESOURCE_LIFETIME_UNKNOWN")
        state=Mutator(self.runtime.workspace(run),run["id"],self.runtime.contract(run)).reconcile(row["id"])
        result={"reconciled":state,"is_error":state!="APPLIED"}
        with self.store.tx() as c:c.execute("UPDATE operations SET state=?,result=? WHERE id=?",("SUCCEEDED" if state=="APPLIED" else "FAILED",canonical(result).decode(),row["id"]))
        self.runtime.settle_controls(run["id"])
        return self.get(op_key)
