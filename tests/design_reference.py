"""RepoForge v1.5 design-only reference tests.

Purpose: catch the two reviewed P1 logic errors in a small deterministic model.
NOT production code, NOT a test of actual RepoForge, OS sandboxing or external graders.
Run: python tests/design_reference.py
1.5.1 portability fix: explicitly close SQLite connections; assertions unchanged.
"""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Contract:
    required_checks: tuple[str, ...]
    global_required_checks: frozenset[str]
    mandatory_requirements: tuple[str, ...]
    coverage: dict[str, frozenset[str]]
    status: str = "FROZEN"


@dataclass(frozen=True)
class CheckDef:
    protected: bool = True
    active: bool = True


def contract_structure_valid(c: Contract, registry: dict[str, CheckDef]) -> bool:
    required = set(c.required_checks)
    global_checks = set(c.global_required_checks)
    if not c.mandatory_requirements or not required:
        return False
    if len(required) != len(c.required_checks):
        return False
    if not global_checks <= required or not required <= set(registry):
        return False
    referenced = set()
    for requirement in c.mandatory_requirements:
        refs = set(c.coverage.get(requirement, ()))
        if not refs or not refs <= required:
            return False
        referenced |= refs
    if not set().union(*(set(x) for x in c.coverage.values())) <= set(registry):
        return False
    if referenced | global_checks != required:
        return False
    return all(registry[x].protected and registry[x].active for x in required)


def delivery_verified(c: Contract, registry: dict[str, CheckDef], evidence: dict[str, str]) -> bool:
    if c.status != "FROZEN" or not contract_structure_valid(c, registry):
        return False
    # First guard is the full registered mandatory set, not only the coverage rows.
    if any(evidence.get(check_id) != "PASSED" for check_id in c.required_checks):
        return False
    # Second guard separately checks required business-requirement coverage.
    return all(c.coverage.get(req) for req in c.mandatory_requirements)


class ContractChecks(unittest.TestCase):
    registry = {"feature": CheckDef(), "static": CheckDef()}

    def make_contract(self, global_checks=(), refs=("feature",)):
        return Contract(
            required_checks=("feature", "static"),
            global_required_checks=frozenset(global_checks),
            mandatory_requirements=("login",),
            coverage={"login": frozenset(refs)},
        )

    def test_unmapped_unmarked_mandatory_check_rejected_at_freeze(self):
        c = self.make_contract()
        self.assertFalse(contract_structure_valid(c, self.registry))
        self.assertFalse(delivery_verified(c, self.registry, {"feature": "PASSED", "static": "FAILED"}))

    def test_declared_global_static_failure_still_blocks_delivery(self):
        c = self.make_contract(global_checks=("static",))
        self.assertTrue(contract_structure_valid(c, self.registry))
        self.assertFalse(delivery_verified(c, self.registry, {"feature": "PASSED", "static": "FAILED"}))

    def test_missing_global_required_evidence_blocks(self):
        c = self.make_contract(global_checks=("static",))
        self.assertFalse(delivery_verified(c, self.registry, {"feature": "PASSED"}))

    def test_all_required_checks_and_coverage_pass(self):
        c = self.make_contract(global_checks=("static",))
        self.assertTrue(delivery_verified(c, self.registry, {"feature": "PASSED", "static": "PASSED"}))

    def test_unknown_check_reference_blocks_freeze(self):
        c = self.make_contract(global_checks=("static",), refs=("missing",))
        self.assertFalse(contract_structure_valid(c, self.registry))

    def test_inactive_required_check_blocks(self):
        c = self.make_contract(global_checks=("static",))
        self.assertFalse(contract_structure_valid(c, {"feature": CheckDef(), "static": CheckDef(active=False)}))

    def test_duplicate_required_check_id_blocks(self):
        c = Contract(("feature", "feature"), frozenset(), ("login",), {"login": frozenset(("feature",))})
        self.assertFalse(contract_structure_valid(c, self.registry))


class TransactionalOutcome(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "control.sqlite")
        conn = self.connect()
        conn.executescript("""
            CREATE TABLE runs (
              id TEXT PRIMARY KEY, epoch INTEGER NOT NULL, status TEXT NOT NULL,
              cancel INTEGER NOT NULL, writer_epoch INTEGER NOT NULL, sealed_source TEXT NOT NULL
            );
            CREATE TABLE control_events (
              event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL
            );
            CREATE TABLE deliveries (run_id TEXT PRIMARY KEY, source_digest TEXT NOT NULL);
            CREATE TABLE outbox (run_id TEXT PRIMARY KEY, message TEXT NOT NULL);
            INSERT INTO runs VALUES('r1',0,'VERIFYING',0,7,'sha:SOURCE');
        """)
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=5, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def prepare(self):
        with contextlib.closing(self.connect()) as conn:
            return conn.execute("SELECT epoch, writer_epoch, sealed_source FROM runs WHERE id='r1'").fetchone()

    def admit(self, event_id: str, kind: str):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM control_events WHERE event_id=?", (event_id,)).fetchone():
                conn.execute("COMMIT")
                return "DUPLICATE"
            status = conn.execute("SELECT status FROM runs WHERE id='r1'").fetchone()[0]
            if status == "SUCCEEDED":
                conn.execute("COMMIT")
                return "ALREADY_COMMITTED"
            conn.execute("INSERT INTO control_events VALUES(?,?,?)", (event_id,"r1",kind))
            conn.execute("UPDATE runs SET epoch=epoch+1, status='CONTROL_PENDING', cancel=? WHERE id='r1'", (int(kind=="CANCEL"),))
            conn.execute("COMMIT")
            return "ADMITTED"
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def finish(self, prepared):
        epoch, writer_epoch, digest = prepared
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            r = conn.execute("SELECT epoch,status,cancel,writer_epoch,sealed_source FROM runs WHERE id='r1'").fetchone()
            if r[0] != epoch or r[1] != "VERIFYING" or r[2]:
                conn.execute("COMMIT")
                return "STALE_CONTROL"
            if r[3] != writer_epoch or r[4] != digest:
                conn.execute("COMMIT")
                return "REVERIFY_REQUIRED"
            conn.execute("INSERT INTO deliveries VALUES('r1',?)", (digest,))
            conn.execute("UPDATE runs SET status='SUCCEEDED' WHERE id='r1'")
            conn.execute("INSERT INTO outbox VALUES('r1','VERIFIED')")
            conn.execute("COMMIT")
            return "COMMITTED"
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return "DUPLICATE_COMMIT"
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def count(self, table):
        with contextlib.closing(self.connect()) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_steering_admitted_after_tests_before_commit_blocks_old_outcome(self):
        prepared = self.prepare()
        self.assertEqual(self.admit("change1", "CONTRACT_CHANGE"), "ADMITTED")
        self.assertEqual(self.finish(prepared), "STALE_CONTROL")
        self.assertEqual(self.count("deliveries"), 0)
        self.assertEqual(self.count("outbox"), 0)

    def test_cancel_admitted_after_tests_before_commit_blocks_old_outcome(self):
        prepared = self.prepare()
        self.assertEqual(self.admit("cancel1", "CANCEL"), "ADMITTED")
        self.assertEqual(self.finish(prepared), "STALE_CONTROL")
        self.assertEqual(self.count("deliveries"), 0)

    def test_delivery_commits_before_steering_remains_historically_valid(self):
        prepared = self.prepare()
        self.assertEqual(self.finish(prepared), "COMMITTED")
        self.assertEqual(self.admit("change1", "CONTRACT_CHANGE"), "ALREADY_COMMITTED")
        self.assertEqual(self.count("deliveries"), 1)
        self.assertEqual(self.count("outbox"), 1)

    def test_replayed_control_event_does_not_advance_epoch_twice(self):
        self.assertEqual(self.admit("change1", "CONTRACT_CHANGE"), "ADMITTED")
        self.assertEqual(self.admit("change1", "CONTRACT_CHANGE"), "DUPLICATE")
        self.assertEqual(self.prepare()[0], 1)

    def test_writer_epoch_change_invalidates_prepared_delivery(self):
        prepared = self.prepare()
        with contextlib.closing(self.connect()) as conn:
            conn.execute("UPDATE runs SET writer_epoch=writer_epoch+1 WHERE id='r1'")
        self.assertEqual(self.finish(prepared), "REVERIFY_REQUIRED")
        self.assertEqual(self.count("deliveries"), 0)

    def test_simultaneous_admission_vs_delivery_is_totally_ordered(self):
        prepared = self.prepare()
        barrier = threading.Barrier(2)
        outcomes = {}
        def do_finish():
            barrier.wait()
            outcomes["finish"] = self.finish(prepared)
        def do_control():
            barrier.wait()
            outcomes["control"] = self.admit("change1", "CONTRACT_CHANGE")
        t1 = threading.Thread(target=do_finish)
        t2 = threading.Thread(target=do_control)
        t1.start(); t2.start(); t1.join(); t2.join()
        allowed = {
            ("COMMITTED", "ALREADY_COMMITTED"),
            ("STALE_CONTROL", "ADMITTED"),
        }
        self.assertIn((outcomes["finish"], outcomes["control"]), allowed)
        self.assertLessEqual(self.count("deliveries"), 1)
        self.assertEqual(self.count("deliveries"), self.count("outbox"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
