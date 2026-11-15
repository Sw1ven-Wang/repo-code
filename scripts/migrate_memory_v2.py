"""Offline shadow migration. Never mutates source state or historical Run pins."""
import argparse
import json
import sqlite3
from pathlib import Path
from repo_code.storage import Store
from repo_code.maintenance import Maintenance, restore_backup
from repo_code.memory.models import MemoryProfile
from repo_code.memory.service import MemoryService
from repo_code.providers import ScriptedProvider
from repo_code.runtime import Runtime
from repo_code.util import digest, atomic_write,RepoError


class ReadOnlySource(Store):
    """Do not run Store initialization/migrations against the source authority."""
    def __init__(self,root):
        self.root=Path(root).absolute();self.db_path=self.root/'runtime.db';self.artifact_root=self.root/'artifacts'
        for path in [self.root,self.db_path,self.artifact_root,self.root/'host.key']:
            if path.is_symlink() or not path.exists():raise RepoError('INVALID_MIGRATION_SOURCE')
        if (self.root/'RESTORE_IN_PROGRESS').exists():raise RepoError('INCOMPLETE_RESTORE')
        self._key=(self.root/'host.key').read_bytes()
        if len(self._key)!=32:raise RepoError('CORRUPT_HOST_KEY')
    def connect(self):
        c=sqlite3.connect(self.db_path.as_uri()+'?mode=ro',uri=True,isolation_level=None,timeout=10)
        c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('PRAGMA busy_timeout=10000')
        return c


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-state',type=Path,required=True)
    p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--profile',type=Path,required=True)
    p.add_argument('--reviewer',required=True,help='Confirms source-state is the current revocation authority')
    args=p.parse_args()
    if args.destination.exists():p.error('destination must be new')
    original=ReadOnlySource(args.source_state);Maintenance(original)._idle()
    before={x['id']:x['data']['manifest'] for x in read_runs(original)}
    backup=args.destination/'backup';Maintenance(original).backup(backup)
    restore_backup(backup,args.destination/'state')
    shadow=Store(args.destination/'state')
    profile=MemoryProfile.model_validate(json.loads(args.profile.read_text(encoding='utf-8')))
    service=MemoryService(shadow,Runtime(shadow),ScriptedProvider(),profile)
    service.repo.sync_revocations(original,args.reviewer)
    try:
        scopes=sorted({row['scope'] for row in original.values('knowledge')})
        pins={scope:service.pin(scope) for scope in scopes}
        after={x['id']:x['data']['manifest'] for x in read_runs(shadow)}
        if before!=after:raise RuntimeError('historical Run manifest changed')
        report={'schema_version':2,'source_state':str(args.source_state.resolve()),'shadow_state':str(shadow.root),
                'legacy_run_manifests_unchanged':True,'legacy_manifest_digest':digest(before),'pins':pins,
                'legacy_source_grade':'legacy_unverified','repository_scope_mapping':'UNCHANGED_PATH_IDENTITY',
                'reviewer':args.reviewer,'source_mutated':False}
        atomic_write(args.destination/'migration-report.json',json.dumps(report,indent=2).encode())
        print(json.dumps(report,indent=2))
    finally:service.close()


def read_runs(store):
    import contextlib
    with contextlib.closing(store.connect()) as c:
        return [{'id':r['id'],'data':json.loads(r['data'])} for r in c.execute('SELECT id,data FROM runs')]


if __name__=='__main__':main()
