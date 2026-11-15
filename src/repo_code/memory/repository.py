"""Additive memory schema; immutable versions and fenced publication.

Legacy runtime schema/Run JSON and legacy artifact codecs are never rewritten.
"""
from __future__ import annotations

import contextlib
import json
from ..util import RepoError, canonical, digest, now
from ..governance import Lineage

TABLES = {"memory_versions", "memory_relations", "summary_nodes", "working_state_versions",
          "memory_jobs", "retrieval_traces", "context_requests", "snapshot_heads",
          "revocation_derivatives", "memory_budget", "memory_cache", "memory_control"}


class MemoryRepository:
    def __init__(self, store):
        self.store = store
        if getattr(store,"_memory_schema_ready",False):return
        with store.tx() as c:
            for table in sorted(TABLES):
                c.execute(f"CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY,data TEXT NOT NULL)")
            c.execute("INSERT OR IGNORE INTO meta VALUES('memory_schema','2')")
            if c.execute("SELECT value FROM meta WHERE key='memory_schema'").fetchone()[0] != "2":
                raise RepoError("UNSUPPORTED_MEMORY_SCHEMA")
        store._memory_schema_ready=True

    def get(self, table, key, c=None, default=None):
        if table not in TABLES: raise RepoError("MEMORY_TABLE_DENIED")
        own = c is None
        c = c or self.store.connect()
        try:
            row = c.execute(f"SELECT data FROM {table} WHERE id=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default
        finally:
            if own: c.close()

    def put(self, table, key, value, c=None, immutable=True):
        if table not in TABLES: raise RepoError("MEMORY_TABLE_DENIED")
        def write(db):
            old = self.get(table, key, db)
            if old is not None and immutable and old != value: raise RepoError("IMMUTABLE_MEMORY_VERSION")
            db.execute(f"INSERT INTO {table} VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                       (key, canonical(value).decode()))
        if c is None:
            with self.store.tx() as db: write(db)
        else: write(c)

    def values(self, table):
        if table not in TABLES: raise RepoError("MEMORY_TABLE_DENIED")
        with contextlib.closing(self.store.connect()) as c:
            return [json.loads(r[0]) for r in c.execute(f"SELECT data FROM {table} ORDER BY id")]

    def epoch(self, c=None):
        return self.get("memory_control", "revocation", c, {"epoch": 0})["epoch"]

    def check(self, roots, c=None):
        own = c is None
        c = c or self.store.connect()
        try:
            if self.get("memory_control", "restore_fence", c): raise RepoError("REVOCATION_SYNC_REQUIRED")
            for root in roots:
                for parent in {root, *Lineage(self.store).ancestors(root, c)}:
                    if self.store.get("revocation", parent, c): raise RepoError("MEMORY_SOURCE_REVOKED")
        finally:
            if own: c.close()

    def derive(self, child, parents, c):
        for parent in set(parents):
            if parent == child or child in Lineage(self.store).ancestors(parent, c): raise RepoError("LINEAGE_CYCLE")
            c.execute("INSERT OR IGNORE INTO lineage VALUES(?,?)", (child, parent))

    def ensure_dev(self, roots, c=None):
        own = c is None
        c = c or self.store.connect()
        try:
            self.check(roots, c)
            for root in roots:
                for parent in {root, *Lineage(self.store).ancestors(root, c)}:
                    if c.execute("SELECT 1 FROM exposures WHERE artifact=?", (parent,)).fetchone():
                        raise RepoError("HIDDEN_MEMORY_LEARNING_DENIED")
        finally:
            if own: c.close()

    def revoke(self, root, reason="operator_revoked"):
        with self.store.tx() as c:
            epoch = self.epoch(c) + 1
            self.store.put("revocation", root, {"at": now(), "reason": reason}, c)
            self.put("memory_control", "revocation", {"epoch": epoch}, c, False)
            self.put("revocation_derivatives", digest([root, epoch]),
                     {"root": root, "epoch": epoch, "action": "BLOCK_IMMEDIATELY_REBUILD_DERIVATIVES", "physical_erasure": False}, c)
        return epoch

    def reserve(self, run_id, purpose, estimated, output, call_limit, token_limit):
        from ..util import uid
        key = uid("reservation-")
        with self.store.tx() as c:
            rows = [json.loads(r[0]) for r in c.execute("SELECT data FROM memory_budget")]
            rows = [r for r in rows if r["run_id"] == run_id]
            if call_limit is not None and sum(r["purpose"] != "main" for r in rows) >= call_limit and purpose != "main":
                raise RepoError("MEMORY_AUXILIARY_BUDGET")
            reserved = estimated + output
            if token_limit is not None and sum(r["charged_tokens"] for r in rows) + reserved > token_limit: raise RepoError("MEMORY_TOKEN_BUDGET")
            self.put("memory_budget", key, {"id": key, "run_id": run_id, "purpose": purpose,
                     "estimated_input": estimated, "reserved_output": output, "charged_tokens": reserved,
                     "state": "RESERVED", "usage": {}, "started": now()}, c)
        return key

    def settle(self, key, usage, seconds):
        with self.store.tx() as c:
            row = self.get("memory_budget", key, c)
            if row["state"] != "RESERVED": raise RepoError("BUDGET_ALREADY_SETTLED")
            inputs = usage.get("input_tokens", usage.get("prompt_tokens"))
            outputs = usage.get("output_tokens", usage.get("completion_tokens"))
            known = isinstance(inputs, int) and isinstance(outputs, int)
            # reasoning_tokens is a completion subfield, never added again.
            row.update(state="SETTLED" if known else "USAGE_UNKNOWN", usage=usage, seconds=seconds)
            if known: row["charged_tokens"] = inputs + outputs
            self.put("memory_budget", key, row, c, False)

    def not_dispatched(self,key,reason):
        """Known pre-dispatch rejection is distinct from unknown API usage."""
        with self.store.tx() as c:
            row=self.get('memory_budget',key,c)
            if row['state']!='RESERVED':raise RepoError('BUDGET_ALREADY_SETTLED')
            row.update(state='NOT_DISPATCHED',charged_tokens=0,seconds=0,usage={},reason=reason)
            self.put('memory_budget',key,row,c,False)

    def publish(self, scope, snapshot, index, expected_head):
        src = self.store.read_json(snapshot); idx = self.store.read_json(index)
        if src.get("schema_version") != 2 or idx.get("source") != snapshot or idx.get("health") != "READY":
            raise RepoError("MEMORY_INDEX_NOT_READY")
        entries = {e["id"]: e["digest"] for e in src["items"]}
        if entries != idx.get("membership"): raise RepoError("MEMORY_INDEX_MEMBERSHIP")
        self.check([snapshot, index])
        with self.store.tx() as c:
            if self.get("snapshot_heads", scope, c) != expected_head: raise RepoError("KNOWLEDGE_HEAD_CAS")
            self.check([snapshot, index], c)
            self.put("snapshot_heads", scope, {"snapshot": snapshot, "index": index,
                     "revision": (expected_head or {}).get("revision", 0) + 1}, c, False)

    def read_claim(self, run, memory_id, version):
        manifest = run["data"]["manifest"]
        source = self.store.read_json(manifest["knowledge_snapshot_digest"])
        entry = next((e for e in source["items"] if e["id"] == memory_id and e.get("version", 1) == version), None)
        if entry is None: raise RepoError("MEMORY_SCOPE_DENIED")
        self.check([entry["digest"], memory_id])
        item = self.store.read_json(entry["digest"])
        if item.get("scope_id", item.get("scope")) not in {manifest["scope"], "user"}: raise RepoError("MEMORY_SCOPE_DENIED")
        return item

    def sync_revocations(self, authoritative_store, reviewer):
        """Explicit Host/operator recovery using the current independent ledger."""
        if not reviewer:raise RepoError("CURRENT_REVOCATION_AUTHORITY_REQUIRED")
        if authoritative_store.root==self.store.root:raise RepoError("INDEPENDENT_REVOCATION_LEDGER_REQUIRED")
        ledger=authoritative_store.values("revocation")
        import contextlib
        with contextlib.closing(authoritative_store.connect()) as source:
            rows=source.execute("SELECT id,data FROM objects WHERE kind='revocation'").fetchall()
        signed={"source":str(authoritative_store.root),"revocations":[[r[0],json.loads(r[1])] for r in rows],"at":now(),"reviewer":reviewer}
        signature=authoritative_store.sign(signed)
        if not self.store.verify(signed,signature):raise RepoError("REVOCATION_LEDGER_IDENTITY_MISMATCH")
        with self.store.tx() as c:
            for key,value in signed["revocations"]:self.store.put("revocation",key,value,c)
            self.put("memory_control","revocation",{"epoch":self.epoch(c)+1},c,False)
            c.execute("DELETE FROM memory_control WHERE id='restore_fence'")
            self.put("memory_control",uid_for_ledger(signed),{"ledger":signed,"signature":signature},c)


def uid_for_ledger(ledger):return "recovery:"+digest(ledger)
