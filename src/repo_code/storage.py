"""Trusted SQLite facts, transactional outbox, content addressed private evidence."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from pathlib import Path
from .util import canonical, digest, private_dir, atomic_write, RepoError, now, uid, release_content_identity

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objects(kind TEXT NOT NULL,id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,active_run_id TEXT,revision INTEGER NOT NULL,total_calls INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,control_epoch INTEGER NOT NULL,writer_epoch INTEGER NOT NULL,cancel_requested INTEGER NOT NULL DEFAULT 0,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE,run_id TEXT,type TEXT NOT NULL,visibility TEXT NOT NULL,data TEXT NOT NULL,created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id,sequence);
CREATE TABLE IF NOT EXISTS control(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,digest TEXT NOT NULL,data TEXT NOT NULL,sequence INTEGER);
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,op_key TEXT NOT NULL UNIQUE,run_id TEXT NOT NULL,state TEXT NOT NULL,semantics TEXT NOT NULL,request TEXT NOT NULL,result TEXT,approved_epoch INTEGER,created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS operations_run ON operations(run_id,state);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,operation_id TEXT UNIQUE,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries(run_id TEXT PRIMARY KEY,id TEXT UNIQUE,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,event_sequence INTEGER UNIQUE,kind TEXT NOT NULL,data TEXT NOT NULL,delivered INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS active_release(scope TEXT PRIMARY KEY,revision INTEGER NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lineage(child TEXT NOT NULL,parent TEXT NOT NULL,PRIMARY KEY(child,parent));
CREATE TABLE IF NOT EXISTS exposures(artifact TEXT NOT NULL,cohort TEXT NOT NULL,family TEXT NOT NULL,class TEXT NOT NULL,PRIMARY KEY(artifact,cohort,family,class));
CREATE TABLE IF NOT EXISTS cohort_claims(candidate TEXT NOT NULL,cohort TEXT NOT NULL,gate TEXT NOT NULL,PRIMARY KEY(candidate,cohort,gate));
CREATE TABLE IF NOT EXISTS cohort_reservations(subject TEXT NOT NULL,scope TEXT NOT NULL,candidate TEXT NOT NULL,cohort TEXT NOT NULL,gate TEXT NOT NULL,PRIMARY KEY(subject,scope));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,problem TEXT NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_problem ON jobs(problem) WHERE state IN ('QUEUED','RUNNING');
"""

class Store:
    def __init__(self, root: Path | str):
        if (Path(root)/"RESTORE_IN_PROGRESS").exists():raise RepoError("INCOMPLETE_RESTORE")
        self.root = private_dir(Path(root))
        self.db_path = self.root / "runtime.db"
        self.artifact_root = private_dir(self.root / "artifacts")
        for p in (self.db_path, self.root / "host.key"):
            if p.is_symlink(): raise RepoError("LINKED_STATE")
        key = self.root / "host.key"
        try:
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError: pass
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_bytes(32)); f.flush(); os.fsync(f.fileno())
        self._key = key.read_bytes()
        if len(self._key) != 32: raise RepoError("CORRUPT_HOST_KEY")
        with contextlib.closing(self.connect()) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                version = c.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 15, 16, 17): raise RepoError("UNSUPPORTED_SCHEMA")
                if version == 15:
                    # v15's PK dropped all but the first task-family exposure.
                    c.execute("ALTER TABLE exposures RENAME TO exposures_v15")
                for statement in SCHEMA.split(";"):
                    if statement.strip(): c.execute(statement)
                if version == 15:
                    c.execute("INSERT OR IGNORE INTO exposures SELECT * FROM exposures_v15")
                    c.execute("DROP TABLE exposures_v15")
                    self._migrate_holdout_identities(c)
                # Old writers reject 16; they must not revive v15's gate semantics.
                c.execute("PRAGMA user_version="+str(17 if version==17 else 16))
                c.execute("COMMIT")
            except BaseException:
                if c.in_transaction: c.execute("ROLLBACK")
                raise
        self.db_path.chmod(0o600)

    def _migrate_holdout_identities(self, c):
        """Conservatively preserve prior hidden use; never requalify old gates."""
        candidates = {row[0]: json.loads(row[1]) for row in c.execute(
            "SELECT id,data FROM objects WHERE kind='candidate'")}
        identities = {}
        for cid, candidate in candidates.items():
            release = self.get("release", candidate["candidate_release_digest"], c)
            if release is None: raise RepoError("MIGRATION_MISSING_RELEASE")
            identity = release_content_identity(release)
            identities[cid] = identity
            c.execute("INSERT OR IGNORE INTO lineage VALUES(?,?)", (cid, identity))
            exposures = c.execute("SELECT * FROM exposures WHERE artifact IN (?,?)",
                                  (cid, candidate["candidate_release_digest"])).fetchall()
            for exposure in exposures:
                c.execute("INSERT OR IGNORE INTO exposures VALUES(?,?,?,?)",
                          (identity, exposure["cohort"], exposure["family"], exposure["class"]))
                cohort = self.get("cohort", exposure["cohort"], c, {})
                if not cohort.get("synthetic_integration"):
                    for fp in cohort.get("task_fingerprints", []):
                        c.execute("INSERT OR IGNORE INTO exposures VALUES(?,?,?,?)",
                                  (identity, exposure["cohort"], "task:"+fp, exposure["class"]))
        for claim in c.execute("SELECT * FROM cohort_claims").fetchall():
            identity = identities.get(claim["candidate"])
            if identity is None: raise RepoError("MIGRATION_MISSING_CANDIDATE")
            meta = self.get("cohort", claim["cohort"], c)
            if not meta: raise RepoError("MIGRATION_MISSING_COHORT")
            scopes = ["cohort:"+claim["cohort"], "family:"+meta["family"]]
            if not meta.get("synthetic_integration"):
                scopes.extend("task:"+f for f in meta.get("task_fingerprints", []))
            for scope in scopes:
                c.execute("INSERT OR IGNORE INTO cohort_reservations VALUES(?,?,?,?,?)",
                          (identity, scope, claim["candidate"], claim["cohort"], claim["gate"]))

    def connect(self):
        c = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA busy_timeout=10000")
        return c

    @contextlib.contextmanager
    def tx(self):
        c = self.connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction: c.execute("ROLLBACK")
            raise
        finally: c.close()

    def put(self, kind, key, data, c=None, immutable=False):
        raw = canonical(data).decode()
        def op(db):
            old = db.execute("SELECT data FROM objects WHERE kind=? AND id=?", (kind,key)).fetchone()
            if old and immutable and old[0] != raw: raise RepoError("IMMUTABLE_CONFLICT")
            db.execute("INSERT INTO objects VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data", (kind,key,raw))
        if c is not None: op(c)
        else:
            with self.tx() as db: op(db)

    def get(self, kind, key, c=None, default=None):
        own = c is None; c = c or self.connect()
        try:
            row = c.execute("SELECT data FROM objects WHERE kind=? AND id=?", (kind,key)).fetchone()
            return json.loads(row[0]) if row else default
        finally:
            if own: c.close()

    def values(self, kind):
        with contextlib.closing(self.connect()) as c:
            return [json.loads(r[0]) for r in c.execute("SELECT data FROM objects WHERE kind=? ORDER BY rowid", (kind,))]

    def blob(self, data: bytes | str) -> str:
        if isinstance(data, str): data = data.encode()
        if len(data) > 32*1024*1024: raise RepoError("ARTIFACT_TOO_LARGE")
        key = digest(data)
        path = self.artifact_root / key[7:]
        if path.exists():
            if self.read_blob(key) != data: raise RepoError("ARTIFACT_CORRUPTION")
        else: atomic_write(path, data)
        return key

    def read_blob(self, key: str) -> bytes:
        if not isinstance(key,str) or len(key)!=71 or not key.startswith("sha256:") or any(x not in "0123456789abcdef" for x in key[7:]):
            raise RepoError("INVALID_ARTIFACT_ID")
        p = self.artifact_root / key[7:]
        if p.is_symlink(): raise RepoError("LINKED_ARTIFACT")
        data = p.read_bytes()
        if digest(data) != key: raise RepoError("ARTIFACT_CORRUPTION")
        return data

    def json_blob(self, value): return self.blob(canonical(value))
    def read_json(self, key): return json.loads(self.read_blob(key))
    def sign(self, value): return hmac.new(self._key, canonical(value), hashlib.sha256).hexdigest()
    def verify(self, value, signature): return hmac.compare_digest(self.sign(value), signature)

    def emit(self, c, run_id, typ, data, visibility="DEV"):
        from .util import redact_data
        data=redact_data(data)
        cur = c.execute("INSERT INTO events(event_id,run_id,type,visibility,data,created) VALUES(?,?,?,?,?,?)", (uid("evt-"),run_id,typ,visibility,canonical(data).decode(),now()))
        return cur.lastrowid

    def events(self, run_id=None, after=0, trusted=False, limit=1000):
        sql = "SELECT * FROM events WHERE sequence>?"; args=[after]
        if run_id is not None: sql += " AND run_id=?"; args.append(run_id)
        if not trusted: sql += " AND visibility='DEV'"
        sql += " ORDER BY sequence LIMIT ?"; args.append(min(limit,10000))
        with contextlib.closing(self.connect()) as c:
            return [{**dict(r),"data":json.loads(r["data"])} for r in c.execute(sql,args)]

    def append_message(self, run_id, data, operation_id=None, c=None):
        def op(db):
            db.execute("INSERT OR IGNORE INTO messages(run_id,operation_id,data) VALUES(?,?,?)", (run_id,operation_id,canonical(data).decode()))
        if c is not None: op(c)
        else:
            with self.tx() as db: op(db)

    def messages(self, run_id):
        with contextlib.closing(self.connect()) as c:
            return [json.loads(r[0]) for r in c.execute("SELECT data FROM messages WHERE run_id=? ORDER BY id", (run_id,))]

    def ref(self, key, owner):
        self.put("ref", digest([key,owner]), {"artifact":key,"owner":owner})

    def backup(self, destination: Path):
        import shutil
        if destination.exists(): raise RepoError("BACKUP_EXISTS")
        private_dir(destination)
        with contextlib.closing(self.connect()) as src, contextlib.closing(sqlite3.connect(destination/"runtime.db")) as dst:
            src.backup(dst)
        shutil.copytree(self.artifact_root, destination/"artifacts")
        shutil.copy2(self.root/"host.key", destination/"host.key")
        manifest={p.relative_to(destination).as_posix():digest(p.read_bytes()) for p in destination.rglob("*") if p.is_file()}
        atomic_write(destination/"BACKUP_COMPLETE.json",canonical(manifest))
        return manifest
