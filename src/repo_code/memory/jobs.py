"""Bounded persistent queue with per-project leases and crash recovery."""
from __future__ import annotations
from ..util import RepoError, digest, now, uid


class MemoryJobs:
    def __init__(self, repository, max_jobs=100): self.repo, self.max_jobs = repository, max_jobs

    def enqueue(self, scope, run_id, prefix, profile_digest, trigger, fence=None):
        key = digest([scope, run_id, prefix, profile_digest, trigger])
        with self.repo.store.tx() as c:
            existing = self.repo.get("memory_jobs", key, c)
            if existing: return existing
            rows = c.execute("SELECT data FROM memory_jobs").fetchall()
            import json
            if sum(json.loads(r[0])["state"] in {"QUEUED", "RUNNING"} for r in rows) >= self.max_jobs:
                raise RepoError("MEMORY_QUEUE_FULL")
            row = {"id": key, "scope": scope, "run_id": run_id, "prefix": prefix, "profile": profile_digest,
                   "trigger": trigger, "state": "QUEUED", "attempts": 0, "lease_until": 0,"fence":fence}
            self.repo.put("memory_jobs", key, row, c)
        return row

    def claim(self, key):
        with self.repo.store.tx() as c:
            row = self.repo.get("memory_jobs", key, c)
            if row is None or row["state"] in {"DONE", "CANCELLED", "REJECTED"}: return None
            import json
            others = [json.loads(r[0]) for r in c.execute("SELECT data FROM memory_jobs")]
            if any(r["scope"] == row["scope"] and r["state"] == "RUNNING" and r["lease_until"] > now() for r in others): return None
            row.update(state="RUNNING", lease_until=now()+120, token=uid("job-owner-"), attempts=row["attempts"]+1)
            self.repo.put("memory_jobs", key, row, c, False)
            return row

    def finish(self, job, outputs, c):
        current = self.repo.get("memory_jobs", job["id"], c)
        if current["state"] != "RUNNING" or current.get("token") != job["token"] or current["lease_until"] < now():
            raise RepoError("MEMORY_JOB_LEASE_LOST")
        current.update(state="DONE", outputs=outputs, lease_until=0)
        self.repo.put("memory_jobs", job["id"], current, c, False)

    def cancel(self, key):
        with self.repo.store.tx() as c:
            row = self.repo.get("memory_jobs", key, c)
            if row and row["state"] != "DONE":
                row.update(state="CANCELLED", lease_until=0)
                self.repo.put("memory_jobs", key, row, c, False)
