"""Small, Host-owned pointers to checked task stages. No model calls here."""
import json

from .models import Authority, Evidence
from .util import RepoError, digest


class VerifiedStages:
    EVENT = "l1.stage_verified"
    KIND = "l1_stage_candidate"

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    def record(self, run_id, *, intent=None, decision=None, review=None, c=None):
        """Persist only references to the checked snapshot and its outcome."""
        if c is None:
            with self.store.tx() as conn:
                return self.record(run_id, intent=intent, decision=decision, review=review, c=conn)
        memory = getattr(self.runtime, "memory", None)
        if memory is not None and not memory.profile.learning_enabled:
            return None
        run = self.runtime.run(run_id, c)
        m = run["data"]["manifest"]
        if (run["data"]["visibility"] != "DEV" or run["cancel_requested"]
                or self.runtime.unresolved(run_id, c)
                or c.execute("SELECT 1 FROM control WHERE run_id=? AND status LIKE 'PENDING%'", (run_id,)).fetchone()):
            return None
        if review is not None:
            if (run["state"] != "RUNNING" or review["status"] not in {"NEW_CHECK_PASSED", "CHECK_FAILED"}
                    or self.store.get("candidate_review", run_id, c) != review
                    or run["control_epoch"] != review["control_epoch"]):
                return None
            source = review["source_digest"]
            checks = sorted((ref, status) for refs, status in
                            ((review["new_passing_checks"] + review["repaired_checks"], "PASSED"),
                             (review["failing_checks"], "FAILED")) for ref in set(refs))
            kind, outcome = "LOCAL_REVIEW", review["status"]
            refs = []
            for ref, _ in checks:
                if not c.execute("SELECT 1 FROM operations WHERE id=? AND run_id=? AND approved_epoch=? AND result IS NOT NULL",
                                 (ref, run_id, run["control_epoch"])).fetchone():
                    return None
                refs.append(ref)
            pairs=[p for p in review.get('local_repair_pairs',[]) if (p['passed'],'PASSED') in checks]
            for pair in pairs:refs.append(pair['failed'])
            identity = [checks,pairs] if pairs else checks
        else:
            if (run["state"] not in {"VERIFYING", "SUCCEEDED"}
                    or decision.intent_digest != intent.digest or intent.run_id != run_id
                    or self.store.get("intent", intent.digest, c) != intent.model_dump(mode="json")
                    or self.store.get("decision", intent.digest, c) != decision.model_dump(mode="json")
                    or not self.store.verify(decision.model_dump(mode="json", exclude={"signature"}), decision.signature)
                    or run["control_epoch"] != intent.control_epoch or run["writer_epoch"] != intent.writer_epoch
                    or m["task_spec_revision"] != intent.task_spec_revision):
                return None
            if run["state"] == "SUCCEEDED":
                delivery = c.execute("SELECT data FROM deliveries WHERE run_id=?", (run_id,)).fetchone()
                if not delivery or json.loads(delivery[0])["intent_digest"] != intent.digest:
                    return None
            elif run["revision"] != intent.run_revision:
                return None
            source = intent.source_digest
            checks, refs = [], [intent.digest]
            contract = self.runtime.contract(run)
            registered = {check.id: check for check in contract.checks if check.id in contract.required_checks}
            for key in decision.evidence_ids:
                item = self.store.get("evidence", key, c)
                if not item:
                    return None
                evidence = Evidence.model_validate(item)
                check = registered.get(evidence.check_id)
                if (not self.store.verify(evidence.model_dump(mode="json", exclude={"signature"}), evidence.signature)
                        or evidence.id != key or evidence.run_id != run_id
                        or evidence.environment_digest != intent.environment_digest
                        or evidence.contract_digest != intent.contract_digest
                        or evidence.release_digest != intent.release_digest
                        or evidence.task_spec_revision != intent.task_spec_revision
                        or evidence.knowledge_snapshot_digest != intent.knowledge_snapshot_digest
                        or evidence.knowledge_index_digest != intent.knowledge_index_digest
                        or check is None or evidence.definition_digest != check.digest
                        or evidence.subject != check.subject or evidence.authority not in check.permitted_authorities):
                    return None
                if (evidence.status in {"PASSED", "FAILED"} and evidence.executed_ids
                        and evidence.executed_ids == evidence.expected_ids and not evidence.skipped_ids
                        and evidence.authority not in {Authority.IN_PROCESS, Authority.SELF}):
                    checks.append((evidence.check_id, evidence.status))
                    refs.extend((key, evidence.report_digest))
            kind = "REGISTERED_VERIFICATION"
            outcome = "DELIVERY_VERIFIED" if run["state"] == "SUCCEEDED" else "DELIVERY_NOT_VERIFIED"
            identity = sorted(checks)
        if not checks or source != self.runtime.workspace(run).snapshot():
            return None
        key = digest(["verified-stage-v1", run_id, run["control_epoch"], run["writer_epoch"], source, kind, outcome, identity])
        if prior := self.store.get(self.KIND, key, c):
            return self.payload(prior)["id"]
        binding = self.store.get("session_run", run_id, c)
        collection = None
        if binding and self.store.verify(binding["payload"], binding["signature"]):
            b = binding["payload"]
            if b["run_id"] == run_id and b["scope"] == m["scope"]:
                collection = b["session"]
        previous = c.execute("SELECT sequence FROM events WHERE run_id=? AND type=? ORDER BY sequence DESC LIMIT 1",
                             (run_id, self.EVENT)).fetchone()
        small = {"id": key, "kind": kind, "outcome": outcome, "status": "CANDIDATE",
                 "source_digest": source, "check_outcomes": sorted(status for _, status in checks)}
        sequence = self.store.emit(c, run_id, self.EVENT, small, "DEV")
        event = c.execute("SELECT event_id,data FROM events WHERE sequence=?", (sequence,)).fetchone()
        payload = {"id": key, "run_id": run_id, "scope": m["scope"], "collection": collection,
                   "event_id": event["event_id"], "sequence": sequence, "after": previous[0] if previous else 0,
                   "event_digest": digest(json.loads(event["data"])),
                   "source_digest": source, "kind": kind, "outcome": outcome,
                   "control_epoch": run["control_epoch"], "writer_epoch": run["writer_epoch"],
                   "refs": sorted(set(refs)), "status": "CANDIDATE",
                   "budget_used": run["data"]["budget_used"], "run_state": run["state"]}
        if review is not None and pairs:payload['local_repair_pairs']=pairs
        self.store.put(self.KIND, key, {"payload": payload, "signature": self.store.sign(payload)}, c, immutable=True)
        return key

    def payload(self, row):
        p = row["payload"]
        if not self.store.verify(p, row["signature"]):
            raise RepoError("L1_STAGE_UNTRUSTED")
        return p

    def materialize(self, key, *, collection=None):
        """Resolve original evidence only in the later Curator phase."""
        from .episodes import EpisodeBuilder
        with self.store.tx() as c:
            row = self.store.get(self.KIND, key, c)
            if not row:
                raise RepoError("L1_STAGE_MISSING")
            p = self.payload(row)
            if p["id"] != key or (collection is not None and p["collection"] != collection):
                raise RepoError("L1_STAGE_SCOPE")
            run = self.runtime.run(p["run_id"], c)
            if run["data"]["visibility"] != "DEV" or run["data"]["manifest"]["scope"] != p["scope"]:
                raise RepoError("L1_STAGE_SCOPE")
            return EpisodeBuilder(self.store).capture_run(p["run_id"], through=p["sequence"], c=c, stage=p, pipeline="causal_v2")


def record_stage(runtime, run_id, c, **evidence):
    """Record atomically with the Host verdict while isolating auxiliary failures."""
    c.execute("SAVEPOINT l1_stage")
    try:
        VerifiedStages(runtime).record(run_id, c=c, **evidence)
    except Exception as exc:
        c.execute("ROLLBACK TO l1_stage")
        run = runtime.run(run_id, c)
        runtime.store.emit(c, run_id, "l1.observation_deferred",
                           {"code": getattr(exc, "code", type(exc).__name__)}, run["data"]["visibility"])
    finally:
        c.execute("RELEASE l1_stage")
