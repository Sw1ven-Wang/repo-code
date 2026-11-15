"""Trusted task/run state and linearized finalization. No candidate code imported."""
from __future__ import annotations
import contextlib
import json
from pathlib import Path
from .models import Budget, Contract, TaskSpec, VerificationIntent, Evidence, PreparedDecision, TERMINAL, UNRESOLVED
from .util import RepoError, canonical, digest, uid, now, private_dir, Faults
from .workspace import Workspace, file_lock
from .knowledge import Knowledge
from .acceptance import validate_contract, delivery_verified

class Runtime:
    def __init__(self,store,sandbox=None):self.store,self.sandbox=store,sandbox;self.knowledge=Knowledge(store)
    def run(self,run_id,c=None):
        own=c is None;c=c or self.store.connect()
        try:
            row=c.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone()
            if row is None:raise RepoError("RUN_NOT_FOUND")
            return {**dict(row),"data":json.loads(row["data"])}
        finally:
            if own:c.close()
    def update(self,c,run,state=None,data=None,control=False,writer=False):
        target=state or run["state"]
        data=data or run["data"]
        stamp=now();started=data.get("clock_active_since")
        if started is not None:data["active_seconds"]=data.get("active_seconds",0)+max(0,stamp-started)
        data["clock_active_since"]=stamp if target in {"RUNNING","VERIFYING"} else None
        if run["state"] in TERMINAL and target!=run["state"]:raise RepoError("TERMINAL_RUN")
        cur=c.execute("UPDATE runs SET state=?,revision=revision+1,control_epoch=control_epoch+?,writer_epoch=writer_epoch+?,data=? WHERE id=? AND revision=?",
            (target,int(control),int(writer),canonical(data or run["data"]).decode(),run["id"],run["revision"]))
        if cur.rowcount!=1:raise RepoError("STALE_RUN")
    def contract(self,run):
        d=run["data"]["manifest"]["contract_digest"]
        return Contract.model_validate(self.store.read_json(d)) if d else None
    def workspace(self,run):return Workspace(Path(run["data"]["workspace"]),self.store)
    def current_goal(self,run):
        """The active conversation request does not replace the frozen TaskSpec."""
        turn=run['data'].get('active_turn') if run['data']['manifest'].get('conversation_mode') else None
        return turn['text'] if turn else run['data']['goal']
    def create(self,goal,source,release,contract=None,budget=None,scope=None,model_config=None,visibility="DEV",task_id=None,task_revision=1,knowledge_pin=None,expected_active=None,integration_origin=None,conversation=False):
        if conversation and contract is not None:raise RepoError("CONVERSATION_CONTRACT_UNSUPPORTED")
        source=Path(source).resolve(strict=True)
        from .knowledge import project_scope
        scope=scope or project_scope(source)
        if self.store.root.is_relative_to(source):raise RepoError("STATE_INSIDE_WORKSPACE")
        if contract:validate_contract(contract,True)
        original=Workspace(source,self.store);baseline=original.snapshot()
        run_id=uid("run-");task_id=task_id or uid("task-")
        folder=private_dir(self.store.root/"workspaces"/run_id)
        ws=Workspace(folder,self.store);ws.restore(baseline)
        memory=getattr(self,"memory",None)
        kd,ki=knowledge_pin or (memory.pin(scope,visibility) if memory else self.knowledge.pin(scope,include_user=visibility=="DEV"));self.knowledge.validate(kd,ki)
        cd=self.store.json_blob(contract) if contract else None
        spec=TaskSpec(task_id=task_id,revision=task_revision,goal=goal,contract_digest=cd,exploration_only=contract is None)
        sd=self.store.json_blob(spec);budget=budget or Budget()
        manifest={"run_id":run_id,"task_id":task_id,"task_spec_revision":task_revision,"task_spec_digest":sd,"contract_digest":cd,"release_digest":release,
            "knowledge_snapshot_digest":kd,"knowledge_index_digest":ki,"workspace_baseline_digest":baseline,
            "environment_digest":self.sandbox.environment_digest if self.sandbox else digest({"sandbox":"none"}),"model_config_digest":digest(model_config or {}),"budget":budget.model_dump(),"scope":scope}
        # A successor inherits implementation bytes, NOT the integration target.
        if conversation:manifest['conversation_mode']=True
        # The user directory and its baseline remain fixed through the lineage.
        origin = integration_origin or {"path":str(source), "baseline":baseline}
        manifest["integration_origin_digest"] = digest(origin)
        if memory:
            if self.store.read_json(kd).get("schema_version")!=2:raise RepoError("MEMORY_PIN_SCHEMA_MISMATCH")
            manifest["memory_schema_version"]=2
            manifest["memory_profile_digest"]=memory.profile_digest
        data={"manifest":manifest,"model_config":model_config or {},"goal":goal,"workspace":str(folder),"inherited_source":str(source),"origin":origin["path"],"origin_digest":origin["baseline"],"visibility":visibility,"created":now(),"active_seconds":0,"clock_active_since":now(),"budget_used":{"model_calls":0,"tool_calls":0,"child_calls":0},"directives":[],"protected_baseline":ws.guarded_snapshot(contract.protected_paths) if contract else digest({}),"permission_mode":"default"}
        if conversation:data['active_turn']={'turn_id':None,'text':goal}
        with self.store.tx() as c:
            task=c.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
            if task:
                if task["active_run_id"]!=expected_active:raise RepoError("TASK_ACTIVE_CONFLICT")
                c.execute("UPDATE tasks SET active_run_id=?,revision=? WHERE id=?",(run_id,task_revision,task_id))
            else:c.execute("INSERT INTO tasks(id,active_run_id,revision) VALUES(?,?,?)",(task_id,run_id,task_revision))
            c.execute("INSERT INTO runs(id,task_id,state,revision,control_epoch,writer_epoch,data) VALUES(?,?,?,?,?,?,?)",(run_id,task_id,"RUNNING",0,0,0,canonical(data).decode()))
            self.store.emit(c,run_id,"run.created",{"manifest":manifest},visibility)
            self.store.append_message(run_id,{"role":"user","content":goal},c=c)
        self.store.ref(baseline,run_id);return self.run(run_id)
    def _conversation_ready(self,run,c):
        if not run['data']['manifest'].get('conversation_mode'):raise RepoError('CONVERSATION_REQUIRED')
        if run['cancel_requested']:raise RepoError('CANCEL_PENDING')
        if c.execute("SELECT 1 FROM control WHERE run_id=? AND status LIKE 'PENDING%'",(run['id'],)).fetchone():raise RepoError('CONTROL_PENDING')
        if self.unresolved(run['id'],c):raise RepoError('RECOVERY_REQUIRED')
        if self.sandbox and not self.sandbox.resources_stopped(run['id']):raise RepoError('RESOURCE_LIFETIME_UNKNOWN')
    def wait_for_user(self,run_id):
        with self.store.tx() as c:
            run=self.run(run_id,c);self.assert_executable(run);self._conversation_ready(run,c)
            self.update(c,run,state='WAITING_USER')
            self.store.emit(c,run_id,'conversation.waiting_user',{},run['data']['visibility'])
        return self.run(run_id)
    def submit_turn(self,run_id,turn_id,text):
        if not isinstance(turn_id,str) or not turn_id or not isinstance(text,str) or not text.strip():raise RepoError('INVALID_CONVERSATION_TURN')
        key=digest([run_id,turn_id]);content=digest(text)
        with self.store.tx() as c:
            run=self.run(run_id,c)
            old=self.store.get('conversation_turn',key,c)
            if old:
                if old['content_digest']!=content:raise RepoError('IDEMPOTENCY_CONFLICT')
                return run
            if run['state']!='WAITING_USER':raise RepoError('CONVERSATION_NOT_WAITING')
            self._conversation_ready(run,c)
            if self.active_seconds(run)>=run['data']['manifest']['budget']['wall_seconds']:raise RepoError('BUDGET_EXHAUSTED','wall_seconds')
            self.store.append_message(run_id,{'role':'user','content':text},c=c)
            self.store.put('conversation_turn',key,{'run_id':run_id,'turn_id':turn_id,'content_digest':content},c)
            run['data']['active_turn']={'turn_id':turn_id,'text':text}
            self.update(c,run,state='RUNNING',control=True)
            self.store.emit(c,run_id,'conversation.turn_submitted',{'turn_id':turn_id},run['data']['visibility'])
        return self.run(run_id)
    def finish_conversation(self,run_id):
        with self.store.tx() as c:
            run=self.run(run_id,c)
            if run['state']!='WAITING_USER':raise RepoError('CONVERSATION_NOT_WAITING')
            self._conversation_ready(run,c)
            run['data']['end_reason']='CONVERSATION_FINISHED: external acceptance not registered'
            self.update(c,run,state='PARTIAL')
            self.store.emit(c,run_id,'conversation.finished',{},run['data']['visibility'])
        return self.run(run_id)
    def assert_executable(self,run):
        if run["state"]!="RUNNING" or run["cancel_requested"]:raise RepoError("RUN_NOT_EXECUTABLE",run["state"])
        with contextlib.closing(self.store.connect()) as c:
            if c.execute("SELECT 1 FROM control WHERE run_id=? AND status LIKE 'PENDING%'",(run["id"],)).fetchone():raise RepoError("CONTROL_PENDING")
        m=run["data"]["manifest"];self.knowledge.validate(m["knowledge_snapshot_digest"],m["knowledge_index_digest"])
    def active_seconds(self,run):
        data=run["data"];started=data.get("clock_active_since")
        return data.get("active_seconds",0)+(max(0,now()-started) if started is not None else 0)
    def remaining_seconds(self,run_id):
        r=self.run(run_id);return max(0,r["data"]["manifest"]["budget"]["wall_seconds"]-self.active_seconds(r))
    def budget_error(self, run, dimension, stage, dispatch_status="NOT_DISPATCHED"):
        active = self.active_seconds(run)
        limit = run["data"]["manifest"]["budget"]["wall_seconds"]
        error = RepoError("BUDGET_EXHAUSTED", dimension)
        error.budget_stop = {"dimension": dimension, "stage": stage, "dispatch_status": dispatch_status,
                             "active_seconds": active, "remaining_seconds": max(0, limit-active)}
        return error

    def check_wall(self, run_id, stage):
        run = self.run(run_id)
        remaining = run["data"]["manifest"]["budget"]["wall_seconds"] - self.active_seconds(run)
        if remaining <= 0:
            raise self.budget_error(run, "wall_seconds", stage)
        return remaining

    def consume(self,run_id,kind,count=1,*,stage="dispatch"):
        if count<0:raise RepoError("INVALID_BUDGET")
        with self.store.tx() as c:
            run=self.run(run_id,c);data=run["data"];limit=data["manifest"]["budget"]
            if self.active_seconds(run)>=limit["wall_seconds"]:raise self.budget_error(run,"wall_seconds",stage)
            used=data["budget_used"];value=used.get(kind,0)+count
            if kind not in limit or (limit[kind] is not None and value>limit[kind]):raise self.budget_error(run,kind,stage)
            task=c.execute("SELECT total_calls FROM tasks WHERE id=?",(run["task_id"],)).fetchone()
            if limit.get('model_calls') is not None and task[0]+count>1000:raise RepoError("TASK_TOTAL_BUDGET")
            used[kind]=value;self.update(c,run,data=data)
            c.execute("UPDATE tasks SET total_calls=total_calls+? WHERE id=?",(count,run["task_id"]))
    def admit(self,run_id,event_id,kind,text=""):
        if kind not in {"STEER","CANCEL","PAUSE"}:raise RepoError("INVALID_CONTROL")
        raw=digest([run_id,kind,text])
        with self.store.tx() as c:
            old=c.execute("SELECT * FROM control WHERE id=?",(event_id,)).fetchone()
            if old:
                if old["digest"]!=raw:raise RepoError("IDEMPOTENCY_CONFLICT")
                return dict(old)
            run=self.run(run_id,c)
            status="AFTER_TERMINAL" if run["state"] in TERMINAL else "PENDING_CANCEL" if kind=="CANCEL" else "PENDING_CLASSIFICATION"
            seq=self.store.emit(c,run_id,"control.admitted",{"event_id":event_id,"kind":kind,"status":status},run["data"]["visibility"])
            c.execute("INSERT INTO control VALUES(?,?,?,?,?,?,?)",(event_id,run_id,kind,status,raw,canonical({"text":text}).decode(),seq))
            if status!="AFTER_TERMINAL":
                self.update(c,run,state="CONTROL_PENDING",control=True)
                if kind=="CANCEL":c.execute("UPDATE runs SET cancel_requested=1 WHERE id=?",(run_id,))
                c.execute("UPDATE operations SET approved_epoch=NULL,state='CANCELLED' WHERE run_id=? AND state IN ('WAITING_APPROVAL','AUTHORIZED','REGISTERED')",(run_id,))
        if kind in {"CANCEL","PAUSE"} and status!="AFTER_TERMINAL" and self.sandbox:
            stopped=self.sandbox.stop_run(run_id)
            if kind=="CANCEL":self.finish_control(event_id,"cancel",resources_stopped=stopped)
        return {"id":event_id,"status":status,"sequence":seq}
    @contextlib.contextmanager
    def file_effect_guard(self, run_id, operation_id):
        """Linearize each forward filesystem effect with control admission.

        Called with the workspace writer lock held. Only bounded local file I/O
        occurs inside this transaction. This is NOT a filesystem/SQLite ACID
        transaction; the journal still reconciles a crash between their commits.
        """
        with self.store.tx() as c:
            run = self.run(run_id, c)
            op = c.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if (run["state"] != "RUNNING" or run["cancel_requested"] or not op
                    or op["run_id"] != run_id or op["state"] != "EXECUTING"
                    or op["approved_epoch"] != run["control_epoch"]):
                raise RepoError("STALE_CONTROL", "File effect authority was revoked")
            if c.execute("SELECT 1 FROM control WHERE run_id=? AND status LIKE 'PENDING%'", (run_id,)).fetchone():
                raise RepoError("CONTROL_PENDING")
            m = run["data"]["manifest"]
            self.knowledge.validate(m["knowledge_snapshot_digest"], m["knowledge_index_digest"])
            yield

    def finish_control(self,event_id,classification,resources_stopped=True):
        if classification not in {"cancel", "pause", "execution_only", "contract_change"}:
            raise RepoError("CLASSIFICATION_REQUIRED")
        with self.store.tx() as c:
            event=c.execute("SELECT * FROM control WHERE id=?",(event_id,)).fetchone()
            if not event:raise RepoError("CONTROL_NOT_FOUND")
            run=self.run(event["run_id"],c)
            if not event["status"].startswith("PENDING"):return run
            if event["kind"] == "CANCEL" and classification != "cancel":
                raise RepoError("CANCEL_PENDING")
            if run["cancel_requested"] and event["kind"] != "CANCEL":
                # A prior steer/pause cannot reopen or prevent draining a later
                # cancellation. Preserve the receipt, explicitly superseded.
                c.execute("UPDATE control SET status='SUPERSEDED_BY_CANCEL' WHERE id=?", (event_id,))
                return run
            # Process leases alone do not prove that file/MCP operations stopped.
            stopped = bool(self.sandbox is not None and self.sandbox.resources_stopped(run["id"]))
            unresolved = self.unresolved(run["id"], c)
            uncertain = bool(c.execute("SELECT 1 FROM operations WHERE run_id=? AND state='UNCERTAIN'", (run["id"],)).fetchone())
            if unresolved or not stopped:
                target = "NEEDS_RECONCILIATION" if uncertain or not stopped else "CONTROL_PENDING"
                status = "PENDING_DRAIN_" + classification.upper()
            else:
                target = {"cancel":"CANCELLED", "pause":"INTERRUPTED",
                          "contract_change":"SUSPENDING_FOR_SUCCESSOR",
                          "execution_only":"RUNNING"}[classification]
                if classification == "execution_only":
                    run["data"]["directives"].append(json.loads(event["data"])["text"])
                status = "APPLIED_" + classification.upper()
                if classification == "cancel":
                    c.execute("UPDATE control SET status='SUPERSEDED_BY_CANCEL' WHERE run_id=? AND id<>? AND status LIKE 'PENDING%'",
                              (run["id"], event_id))
            c.execute("UPDATE control SET status=? WHERE id=?",(status,event_id))
            self.update(c,run,state=target,data=run["data"])
            self.store.emit(c,run["id"],"control.applied" if status.startswith("APPLIED") else "control.drain_pending",
                            {"event_id":event_id,"classification":classification},run["data"]["visibility"])
        return self.run(run["id"])

    def settle_controls(self, run_id):
        """Revisit admitted controls after a tracked operation is resolved."""
        with contextlib.closing(self.store.connect()) as c:
            rows = c.execute("SELECT id,status FROM control WHERE run_id=? AND status LIKE 'PENDING_DRAIN_%' ORDER BY sequence", (run_id,)).fetchall()
        for row in rows:
            self.finish_control(row["id"], row["status"][len("PENDING_DRAIN_"):].lower())

    def unresolved(self,run_id,c=None):
        own=c is None;c=c or self.store.connect()
        try:return bool(c.execute("SELECT 1 FROM operations WHERE run_id=? AND state IN ('REGISTERED','WAITING_APPROVAL','AUTHORIZED','EXECUTING','UNCERTAIN') LIMIT 1",(run_id,)).fetchone())
        finally:
            if own:c.close()
    def successor(self,run_id,goal,contract,knowledge_pin=None):
        old=self.run(run_id)
        if self.sandbox and not self.sandbox.resources_stopped(run_id):raise RepoError("RESOURCE_LIFETIME_UNKNOWN")
        if self.unresolved(run_id):raise RepoError("UNRESOLVED_OPERATIONS")
        if old["state"] not in {"SUSPENDING_FOR_SUCCESSOR","INTERRUPTED",*TERMINAL}:raise RepoError("NOT_READY_FOR_SUCCESSOR")
        with self.store.tx() as c:
            r=self.run(run_id,c)
            if r["state"] not in TERMINAL:self.update(c,r,state="SUPERSEDED",writer=True)
        m=old["data"]["manifest"]
        new=self.create(goal,old["data"]["workspace"],m["release_digest"],contract,Budget.model_validate(m["budget"]),m["scope"],model_config=old["data"].get("model_config",{}),visibility=old["data"]["visibility"],task_id=old["task_id"],task_revision=m["task_spec_revision"]+1,knowledge_pin=knowledge_pin,expected_active=run_id,
            integration_origin={"path":old["data"]["origin"],"baseline":old["data"]["origin_digest"]})
        self.store.put("successor",new["id"],{"old_run":run_id,"new_run":new["id"],"snapshot":new["data"]["manifest"]["workspace_baseline_digest"]},immutable=True)
        return new
    def prepare(self,run_id):
        with file_lock(self.store.root/"locks"/(run_id+".lock")):
            with self.store.tx() as c:
                run=self.run(run_id,c)
                if run["state"] not in {"RUNNING","VERIFYING"} or run["cancel_requested"]:raise RepoError("STALE_CONTROL")
                if c.execute("SELECT 1 FROM control WHERE run_id=? AND status LIKE 'PENDING%'",(run_id,)).fetchone():raise RepoError("STALE_CONTROL")
                if self.unresolved(run_id,c):raise RepoError("UNRESOLVED_OPERATIONS")
                if self.sandbox and not self.sandbox.resources_stopped(run_id):raise RepoError("RESOURCE_LIFETIME_UNKNOWN")
                contract=self.contract(run)
                if contract is None:raise RepoError("EXPLORATION_ONLY")
                validate_contract(contract,True)
                ws=self.workspace(run);snapshot=ws.snapshot()
                if ws.guarded_snapshot(contract.protected_paths)!=run["data"]["protected_baseline"]:raise RepoError("PROTECTED_CHECK_CHANGED")
                m=run["data"]["manifest"];self.knowledge.validate(m["knowledge_snapshot_digest"],m["knowledge_index_digest"])
                self.update(c,run,state="VERIFYING")
                intent=VerificationIntent(run_id=run_id,task_spec_revision=m["task_spec_revision"],contract_digest=m["contract_digest"],release_digest=m["release_digest"],knowledge_snapshot_digest=m["knowledge_snapshot_digest"],knowledge_index_digest=m["knowledge_index_digest"],environment_digest=m["environment_digest"],control_epoch=run["control_epoch"],run_revision=run["revision"]+1,writer_epoch=run["writer_epoch"],source_digest=snapshot,required_check_set_digest=digest(sorted(contract.required_checks)),protected_baseline=run["data"]["protected_baseline"])
                self.store.put("intent",intent.digest,intent,c,immutable=True)
                self.store.emit(c,run_id,"verification.prepared",{"intent":intent.digest},run["data"]["visibility"])
        return intent
    def commit(self,intent,decision,faults=None):
        faults=faults or Faults()
        if decision.intent_digest!=intent.digest or not self.store.verify(decision.model_dump(mode="json",exclude={"signature"}),decision.signature):raise RepoError("UNTRUSTED_DECISION")
        with file_lock(self.store.root/"locks"/(intent.run_id+".lock")):
            with self.store.tx() as c:
                run=self.run(intent.run_id,c);task=c.execute("SELECT * FROM tasks WHERE id=?",(run["task_id"],)).fetchone()
                exists=c.execute("SELECT data FROM deliveries WHERE run_id=?",(run["id"],)).fetchone()
                if exists:return {"status":"ALREADY_COMMITTED","delivery":json.loads(exists[0])}
                if task["active_run_id"]!=run["id"] or task["revision"]!=intent.task_spec_revision or run["revision"]!=intent.run_revision or run["control_epoch"]!=intent.control_epoch or run["state"]!="VERIFYING" or run["cancel_requested"]:return {"status":"STALE_CONTROL"}
                if c.execute("SELECT 1 FROM control WHERE run_id=? AND status LIKE 'PENDING%'",(run["id"],)).fetchone():return {"status":"STALE_CONTROL"}
                m=run["data"]["manifest"]
                if m["contract_digest"]!=intent.contract_digest or m["release_digest"]!=intent.release_digest:return {"status":"STALE_CONTROL"}
                if run["writer_epoch"]!=intent.writer_epoch or self.workspace(run).snapshot()!=intent.source_digest:return {"status":"REVERIFY_REQUIRED"}
                if self.unresolved(run["id"],c) or (self.sandbox and not self.sandbox.resources_stopped(run["id"])):return {"status":"REVERIFY_REQUIRED"}
                self.knowledge.validate(m["knowledge_snapshot_digest"],m["knowledge_index_digest"])
                es={}
                for eid in decision.evidence_ids:
                    e=Evidence.model_validate(self.store.get("evidence",eid,c));es[e.check_id]=e
                valid,reason=delivery_verified(intent,self.contract(run),es,decision.artifact_digest,self.store)
                if not decision.verified or not valid:return {"status":"NOT_VERIFIED","reason":reason}
                delivery={"id":uid("delivery-"),"run_id":run["id"],"outcome":"VERIFIED","coverage":"registered_checks_only","task_spec_revision":intent.task_spec_revision,"contract_digest":intent.contract_digest,"release_digest":intent.release_digest,"knowledge_snapshot_digest":intent.knowledge_snapshot_digest,"knowledge_index_digest":intent.knowledge_index_digest,"source_digest":intent.source_digest,"artifact_digest":decision.artifact_digest,"evidence_ids":decision.evidence_ids,"intent_digest":intent.digest,"control_epoch":run["control_epoch"],"writer_epoch":run["writer_epoch"]}
                seq=self.store.emit(c,run["id"],"delivery.committed",delivery,run["data"]["visibility"]);delivery["sequence"]=seq
                c.execute("INSERT INTO deliveries VALUES(?,?,?)",(run["id"],delivery["id"],canonical(delivery).decode()))
                self.update(c,run,state="SUCCEEDED")
                faults.hit("delivery_before_outbox")
                c.execute("INSERT INTO outbox(event_sequence,kind,data) VALUES(?,?,?)",(seq,"DELIVERY",canonical(delivery).decode()))
        return {"status":"COMMITTED","delivery":delivery}
    def resume_after_failed_verification(self,run_id,decision):
        with self.store.tx() as c:
            run=self.run(run_id,c)
            if run["state"]!="VERIFYING":return
            self.update(c,run,state="RUNNING")
            self.store.append_message(run_id,{"role":"user","content":"Trusted verification failed: "+decision.reason+". Inspect diagnostics and correct the implementation. No success was committed."},c=c)
    def end(self,run_id,state,reason,*,budget_stop=None):
        if state not in {"PARTIAL","FAILED","BLOCKED","ANSWERED"}:raise RepoError("INVALID_OUTCOME")
        with self.store.tx() as c:
            run=self.run(run_id,c)
            if run["state"] in TERMINAL:return run
            if self.unresolved(run_id, c) or (self.sandbox and not self.sandbox.resources_stopped(run_id)):
                state = "NEEDS_RECONCILIATION"
            run["data"]["end_reason"]=reason
            outcome = {"state":state,"reason":reason}
            if budget_stop is not None:
                run["data"]["budget_stop"] = budget_stop
                outcome["budget_stop"] = budget_stop
            self.update(c,run,state=state,data=run["data"])
            self.store.emit(c,run_id,"run.ended",outcome,run["data"]["visibility"])
        return self.run(run_id)
    def recover(self,run_id):
        with self.store.tx() as c:
            run=self.run(run_id,c)
            if run["state"] in TERMINAL:return run
            c.execute("UPDATE operations SET state='UNCERTAIN' WHERE run_id=? AND state='EXECUTING'",(run_id,))
            unknown=self.unresolved(run_id,c) or bool(self.sandbox and not self.sandbox.resources_stopped(run_id))
            self.update(c,run,state="NEEDS_RECONCILIATION" if unknown else "INTERRUPTED")
        return self.run(run_id)
    def resume(self,run_id):
        with self.store.tx() as c:
            run=self.run(run_id,c)
            if run["state"] not in {"RUNNING","INTERRUPTED","WAITING_APPROVAL","BLOCKED"}:raise RepoError("CANNOT_RESUME")
            if c.execute("SELECT 1 FROM control WHERE run_id=? AND status LIKE 'PENDING%'",(run_id,)).fetchone():raise RepoError("CONTROL_PENDING")
            if c.execute("SELECT 1 FROM operations WHERE run_id=? AND state IN ('EXECUTING','UNCERTAIN')",(run_id,)).fetchone():raise RepoError("RECOVERY_REQUIRED")
            if self.sandbox and not self.sandbox.resources_stopped(run_id):raise RepoError("RESOURCE_LIFETIME_UNKNOWN")
            if run["cancel_requested"]:raise RepoError("CANCEL_PENDING")
            self.update(c,run,state="RUNNING")
            self.store.emit(c,run_id,'run.resumed',{'writer_epoch':run['writer_epoch']},run['data']['visibility'])
        return self.run(run_id)
