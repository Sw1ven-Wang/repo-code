"""Offline private backups and conservative scratch reclamation.

Evidence, lineage, pinned snapshots and unresolved recovery data are never
silently removed to satisfy a quota. Archive pressure blocks further evolution.
"""
import contextlib
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
from .models import TERMINAL
from .util import RepoError, private_dir, atomic_write, digest
from .workspace import file_lock

class Maintenance:
    def __init__(self,store):self.store=store
    def _idle(self):
        with contextlib.closing(self.store.connect()) as c:
            if any(r[0] not in TERMINAL|{'INTERRUPTED','NEEDS_RECONCILIATION','BLOCKED'} for r in c.execute('SELECT state FROM runs')):raise RepoError('MAINTENANCE_REQUIRES_STOPPED_HOST')
        if any(x['state']!='STOPPED' for x in self.store.values('lease')):raise RepoError('UNRESOLVED_RESOURCES')
    def inspect(self):
        with contextlib.closing(self.store.connect()) as c:
            check=c.execute('PRAGMA integrity_check').fetchone()[0]
            if check!='ok':raise RepoError('DATABASE_INTEGRITY')
            runs=c.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
        blobs=list(self.store.artifact_root.iterdir())
        if any(p.is_symlink() for p in blobs):raise RepoError('LINKED_ARTIFACT')
        return {'integrity':check,'runs':runs,'artifact_count':len(blobs),'artifact_bytes':sum(x.stat().st_size for x in blobs),'database_bytes':sum(p.stat().st_size for p in self.store.root.glob('runtime.db*')),'protected_history_retained':True}
    def backup(self,destination):
        self._idle();destination=Path(destination).absolute()
        if destination.exists() or destination.is_relative_to(self.store.root):raise RepoError('NEW_BACKUP_DIRECTORY_REQUIRED')
        private_dir(destination)
        with file_lock(self.store.root/'maintenance.lock'):
            with contextlib.closing(self.store.connect()) as source:
                target=sqlite3.connect(destination/'runtime.db')
                try:source.backup(target)
                finally:target.close()
            shutil.copytree(self.store.artifact_root,destination/'artifacts')
            shutil.copy2(self.store.root/'host.key',destination/'host.key')
            files={p.relative_to(destination).as_posix():digest(p.read_bytes()) for p in destination.rglob('*') if p.is_file()}
            for p in destination.rglob('*'):p.chmod(0o700 if p.is_dir() else 0o600)
            atomic_write(destination/'manifest.json',json.dumps({'format':1,'files':files},sort_keys=True).encode())
        return {'backup':str(destination),'files':len(files),'contains_source_and_private_credentials':True}
    def prune(self,apply=False,backup=None):
        self._idle();paths=[]
        # Only stopped lease scratch; artifacts and signed records remain protected.
        for row in self.store.values('lease'):
            raw=row.get('root')
            if row['state']=='STOPPED' and raw:
                p=Path(raw)
                if p.is_relative_to(self.store.root/'jails') and p.is_dir() and not p.is_symlink():paths.append(p)
        report={'dry_run':not apply,'scratch_paths':[str(x) for x in paths],'bytes':sum(f.stat().st_size for p in paths for f in p.rglob('*') if f.is_file()),'history_deleted':False}
        if apply:
            if backup is None:raise RepoError('BACKUP_REQUIRED')
            self.backup(backup)
            for path in paths:shutil.rmtree(path)
        return report

def restore_backup(source,destination):
    source=Path(source);destination=Path(destination)
    if destination.exists():raise RepoError('RESTORE_REQUIRES_NEW_DIRECTORY')
    manifest=json.loads((source/'manifest.json').read_text())
    if manifest.get('format')!=1:raise RepoError('BACKUP_VERSION')
    for name,key in manifest['files'].items():
        path=source/name
        if Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink() or digest(path.read_bytes())!=key:raise RepoError('CORRUPT_BACKUP')
    private_dir(destination);atomic_write(destination/'RESTORE_IN_PROGRESS',b'1')
    for name in manifest['files']:atomic_write(destination/name,(source/name).read_bytes())
    with contextlib.closing(sqlite3.connect(destination/'runtime.db')) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RepoError('DATABASE_INTEGRITY')
        if c.execute("SELECT 1 FROM meta WHERE key='memory_schema'").fetchone():
            # A backup cannot prove that no later external revocation occurred.
            c.execute("INSERT OR REPLACE INTO memory_control VALUES('restore_fence',?)",(json.dumps({'requires_current_revocation_ledger':True}),))
            c.commit()
    (destination/'RESTORE_IN_PROGRESS').unlink()
    return {'restored':str(destination),'requires_explicit_run_reconciliation':True}
