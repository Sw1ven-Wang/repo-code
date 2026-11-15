"""Explicit deliverable export/integration; never silently modify the origin."""
import contextlib
from pathlib import Path
import json
from .workspace import Workspace, Mutator
from .util import RepoError, atomic_write, digest

class Delivery:
    def __init__(self,service): self.app=service;self.store=service.store
    def record(self,run_id):
        with contextlib.closing(self.store.connect()) as c:r=c.execute('SELECT data FROM deliveries WHERE run_id=?',(run_id,)).fetchone()
        if not r:raise RepoError('NO_VERIFIED_DELIVERY')
        return json.loads(r[0])
    def export(self,run_id,destination):
        rec=self.record(run_id);destination=Path(destination)
        if destination.exists() and any(destination.iterdir()):raise RepoError('EXPORT_REQUIRES_EMPTY_DESTINATION')
        destination.mkdir(parents=True,exist_ok=True)
        ws=Workspace(destination,self.store);ws.restore(rec['source_digest'])
        if rec['artifact_digest']:
            manifest=self.store.read_json(rec['artifact_digest'])
            for path,blob in manifest['files'].items():
                # Artifact manifest values may be metadata or content hashes.
                key=blob.get('blob') if isinstance(blob,dict) else blob
                atomic_write(ws.path('dist/'+path,True),self.store.read_blob(key))
        return {'exported':str(destination.resolve()),'delivery_id':rec['id'],'source_digest':rec['source_digest']}
    def integrate(self,run_id,confirm=False):
        if not confirm:raise RepoError('INTEGRATION_CONFIRMATION_REQUIRED')
        rec=self.record(run_id);run=self.app.runtime.run(run_id)
        original=Workspace(Path(run['data']['origin']),self.store)
        if original.snapshot()!=run['data']['origin_digest']:raise RepoError('ORIGIN_CHANGED')
        old=original.manifest()['files'];new=self.store.read_json(rec['source_digest'])['files'];ops=[]
        for p in sorted(set(old)|set(new)):
            if old.get(p)==new.get(p):continue
            if p not in new:ops.append({'kind':'delete','path':p,'expected_version':old[p]['blob']})
            elif p not in old:ops.append({'kind':'create','path':p,'content':self.store.read_blob(new[p]['blob'])})
            else:ops.append({'kind':'replace','path':p,'content':self.store.read_blob(new[p]['blob']),'expected_version':old[p]['blob']})
        if len(ops)>50:raise RepoError('INTEGRATION_TOO_LARGE','export the frozen snapshot for manual review')
        if ops:Mutator(original,'integrate-'+run_id,self.app.runtime.contract(run)).apply('integrate-'+rec['id'],ops)
        return {'integrated':True,'files':len(ops),'delivery':rec['id']}
