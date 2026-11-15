"""Replayable outbox observer. Symptoms are NOT causal proof."""
from __future__ import annotations
import contextlib
import json
import threading
from .util import digest, now, uid, RepoError

class Observer:
    """Cheap symptom extraction, with explicit evidence-based scheduling reasons.

    This is NOT a causal inference engine. Error strings only narrow a local
    retry symptom; all root-cause hypotheses still require independent trials.
    """
    EVIDENCE_TOOLS = {"read", "search", "glob", "artifact.read", "memory.search", "skill.search"}
    MUTATION_TOOLS = {"create", "write", "edit", "patch", "move", "delete"}
    FAMILIES = ("ModuleNotFoundError", "ImportError", "SyntaxError", "NameError", "AssertionError")

    def __init__(self, store): self.store = store

    def _reset(self, run, c=None):
        self.store.put("observer_run", run, {"last": None, "repeats": 0, "events": []}, c)

    def consume(self):
        with self.store.tx() as c:
            return self._consume(c)

    def _consume(self, c):
        cursor = self.store.get("observer", "cursor", c, default={"sequence": 0})["sequence"]
        events = self.store.events(after=cursor, limit=10000)
        new = []
        for event in events:
            run, data = event["run_id"], event["data"]
            if event["type"] == "tool.operation_finished":
                tool = data["tool_name"]
                if tool in self.EVIDENCE_TOOLS | self.MUTATION_TOOLS:
                    # Even a failed evidence request or attempted mutation ends
                    # this cheap uninterrupted-streak rule. Do not bridge gaps.
                    self._reset(run,c)
                elif tool == "process.run":
                    result = data["result"]
                    error = (result.get("stderr") or result.get("preview") or "")[:4000]
                    family = next((f for f in self.FAMILIES if f in error), None)
                    qualified = (result.get("is_error") and not result.get("timeout")
                                 and result.get("relevant_state") and family
                                 and data.get("action_digest"))
                    if not qualified:
                        # Transient/unknown errors are not evidence of a stable
                        # deterministic failure, and cannot extend an old streak.
                        self._reset(run,c)
                    else:
                        fingerprint = digest([data["action_digest"], result["relevant_state"], family, error])
                        last = self.store.get("observer_run", run, c, default={"last": None, "repeats": 0, "events": []})
                        same = last["last"] == fingerprint
                        count = last["repeats"] + 1 if same else 1
                        support = ((last.get("events", []) if same else []) + [event["event_id"]])[-8:]
                        self.store.put("observer_run", run, {"last": fingerprint, "repeats": count, "events": support},c)
                        if count >= 2:
                            pid = digest([run, event["sequence"], "ineffective_retry"])
                            pattern_id = digest(["ineffective_retry", family])
                            problem = {"id": pid, "run_id": run, "event_id": event["event_id"],
                                       "sequence": event["sequence"], "symptom": "ineffective_retry",
                                       "error_family": family, "state_digest": result["relevant_state"],
                                       "action_digest": data["action_digest"], "fingerprint": fingerprint,
                                       "evidence": support, "repeat_count": count,
                                       "new_evidence_observed": False,
                                       "confidence": "observed_symptom_not_root_cause"}
                            self.store.put("problem", pid, problem, c, immutable=True)
                            pattern = self.store.get("pattern", pattern_id, c, default={
                                "id": pattern_id, "symptom": "ineffective_retry", "error_family": family,
                                "support": [], "contradictions": [], "status": "OBSERVING", "candidates": []})
                            if pid not in pattern["support"]: pattern["support"].append(pid)
                            if pattern["status"] != "COOLDOWN": pattern["status"] = "ELIGIBLE_FOR_TRIAGE"
                            self.store.put("pattern", pattern_id, pattern,c)
                            new.append(pattern_id)
            from .episodes import EpisodeBuilder
            if event["type"] in EpisodeBuilder.TRIGGERS:
                EpisodeBuilder(self.store).capture_run(run, through=event["sequence"], c=c)
            cursor = event["sequence"]
            self.store.put("observer", "cursor", {"sequence": cursor},c)
        return list(dict.fromkeys(new))

    def triage(self, pattern_id, min_distinct_runs=2):
        pattern = self.store.get("pattern", pattern_id)
        if not pattern: raise RepoError("PATTERN_NOT_FOUND")
        problems = [self.store.get("problem", x) for x in pattern["support"]]
        problems = [p for p in problems if p]
        runs, tasks, failed_tasks = set(), set(), set()
        eligible = []
        with contextlib.closing(self.store.connect()) as conn:
            for problem in problems:
                runs.add(problem["run_id"])
                row = conn.execute("SELECT task_id,state FROM runs WHERE id=?", (problem["run_id"],)).fetchone()
                if row:
                    tasks.add(row["task_id"])
                    if row["state"] in {"FAILED", "PARTIAL"}: failed_tasks.add(row["task_id"])
                ids = set(problem.get("evidence", []))
                evidence_present = len(ids) >= 2 and all(conn.execute(
                    "SELECT 1 FROM events WHERE event_id=? AND run_id=? AND visibility='DEV'",
                    (eid, problem["run_id"])).fetchone() for eid in ids)
                if (row and evidence_present and problem.get("fingerprint")
                        and problem.get("state_digest") and problem.get("action_digest")
                        and problem.get("new_evidence_observed") is False):
                    eligible.append(problem)
        reasons = []
        if pattern["status"] == "COOLDOWN": reasons.append("COOLDOWN_ACTIVE")
        if pattern.get("contradictions"): reasons.append("CONTRADICTORY_EVIDENCE")
        if pattern.get("symptom") != "ineffective_retry": reasons.append("NO_BOUNDED_REPAIR_HYPOTHESIS")
        if not eligible: reasons.append("INSUFFICIENT_TRACE_BINDING")
        # Recurrence alone is not sufficient. Distinct Task IDs (not repeated
        # successor Runs of one task), unchanged-state evidence and no unresolved
        # contradiction are all required for this initial conservative policy.
        eligible_runs = {p["run_id"] for p in eligible}
        with contextlib.closing(self.store.connect()) as conn:
            eligible_tasks = {row[0] for rid in eligible_runs
                              if (row := conn.execute("SELECT task_id FROM runs WHERE id=?", (rid,)).fetchone())}
        if len(eligible_tasks) < max(1, min_distinct_runs): reasons.append("LIMITED_CROSS_TASK_SUPPORT")
        decision = "WAIT_FOR_EVIDENCE" if reasons else "SCHEDULE_TARGETED_EVOLUTION"
        out = {"pattern_id": pattern_id, "decision": decision, "distinct_runs": len(runs),
               "distinct_tasks": len(tasks), "supporting_problems": len(problems),
               "qualified_problems": len(eligible), "affected_failed_tasks": len(failed_tasks),
               "reason_codes": reasons or ["REPEATED_UNCHANGED_STATE", "CROSS_TASK_RECURRENCE", "BOUNDED_TARGET_REVIEW"],
               "reason": "Symptom warrants bounded diagnosis, not a proven cause or automatic release",
               "repair_scope_hint": "failure feedback and retry handling; do not scan unrelated modules",
               "risk": "Must retain transient retry negative controls",
               "search_budget_hint": {"max_candidates": 3, "max_rounds": 2}}
        self.store.put("triage", pattern_id, out)
        return out

class EvolutionQueue:
    def __init__(self,store):self.store=store;self._owned={}
    def enqueue(self,problem,kind="L1"):
        import sqlite3
        job=uid("job-")
        try:
            with self.store.tx() as c:c.execute("INSERT INTO jobs VALUES(?,?,?,?,?)",(job,kind,problem,"QUEUED","{}"))
        except sqlite3.IntegrityError:raise RepoError("PATTERN_ALREADY_QUEUED") from None
        return job
    def claim(self,kind="L1",lease_seconds=120,*,problem=None):
        if not 0 < lease_seconds <= 3600:raise RepoError('INVALID_JOB_LEASE')
        with self.store.tx() as c:
            if c.execute("SELECT 1 FROM runs WHERE state IN ('RUNNING','VERIFYING','CONTROL_PENDING') LIMIT 1").fetchone():return None
            for running in c.execute("SELECT * FROM jobs WHERE state='RUNNING'").fetchall():
                data=json.loads(running['data'])
                if data.get('lease_until',0)>now():return None
                c.execute("UPDATE jobs SET state='QUEUED' WHERE id=?",(running['id'],))
            row=c.execute("SELECT * FROM jobs WHERE state='QUEUED' AND kind=? AND (? IS NULL OR problem=?) ORDER BY rowid LIMIT 1",(kind,problem,problem)).fetchone()
            if not row:return None
            if kind=='L1_CURATE':
                from .episodes import EpisodeBuilder
                try:EpisodeBuilder(self.store).get(row['problem'],c)
                except RepoError as exc:
                    data={**json.loads(row['data']),'rejection':exc.code}
                    c.execute("UPDATE jobs SET state='REJECTED',data=? WHERE id=?",(json.dumps(data),row['id']))
                    return None
            data=json.loads(row['data']);token=uid('job-owner-')
            data.update(owner_token=token,lease_until=now()+lease_seconds,attempts=data.get('attempts',0)+1)
            c.execute("UPDATE jobs SET state='RUNNING',data=? WHERE id=? AND state='QUEUED'",(json.dumps(data),row["id"]))
            self._owned[(threading.get_ident(),row['id'])]=token
            return {**dict(row),'state':'RUNNING','data':json.dumps(data),'owner_token':token,'lease_until':data['lease_until']}
    def finish(self,job,state="DONE",*,owner_token=None):
        if state not in {"DONE","FAILED","COOLDOWN","QUEUED","REJECTED"}:raise RepoError("JOB_STATE")
        token=owner_token or self._owned.get((threading.get_ident(),job))
        with self.store.tx() as c:
            row=c.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
            data=json.loads(row['data']) if row else {}
            if not token or not row or row['state']!='RUNNING' or data.get('owner_token')!=token or data.get('lease_until',0)<=now():
                raise RepoError('JOB_LEASE_LOST')
            data['lease_until']=0
            c.execute("UPDATE jobs SET state=?,data=? WHERE id=?",(state,json.dumps(data),job))
    def renew(self,job,owner_token,lease_seconds=120):
        if not 0<lease_seconds<=3600:raise RepoError('INVALID_JOB_LEASE')
        with self.store.tx() as c:
            row=c.execute('SELECT state,data FROM jobs WHERE id=?',(job,)).fetchone()
            data=json.loads(row['data']) if row else {}
            if not row or row['state']!='RUNNING' or data.get('owner_token')!=owner_token or data.get('lease_until',0)<=now():
                raise RepoError('JOB_LEASE_LOST')
            data['lease_until']=now()+lease_seconds
            c.execute('UPDATE jobs SET data=? WHERE id=?',(json.dumps(data),job))
