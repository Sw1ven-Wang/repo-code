"""Broker-governed source mirror and process execution in the Harbor task image."""
import asyncio
import io
import json
import os
import shlex
import tarfile
import tempfile
import time
from pathlib import Path

from repo_code.util import RepoError,atomic_write,digest,uid
from repo_code.workspace import Workspace,relative_path


def host_path(path):
    path=Path(path).resolve()
    # Public repositories contain paths longer than Windows MAX_PATH. Keep
    # Python's trusted mirror operations in the extended-length namespace.
    if os.name=='nt' and not str(path).startswith('\\\\?\\'):
        return Path('\\\\?\\'+str(path))
    return path


EXPORT = r'''
import hashlib,json,os,subprocess,sys,tarfile
from pathlib import Path
p=json.loads(sys.argv[1]);root=Path(p['root']);omitted=[];modes={};current={}
known=json.load(open(p['known']))
names=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=root).decode().split('\0')
excluded={'.git','.venv','venv','__pycache__','.pytest_cache','.mypy_cache','.ruff_cache','node_modules'}
with tarfile.open(p['archive'],'w') as tar:
    for name in sorted(set(names)):
        if not name or any(x in excluded for x in Path(name).parts):continue
        f=root/name
        if f.is_symlink() or any(q.is_symlink() for q in f.parents if q!=root and root in q.parents):
            omitted.append(name);continue
        if not f.is_file():continue
        if f.stat().st_size>8*1024*1024:raise RuntimeError('FILE_TOO_LARGE:'+name)
        sha='sha256:'+hashlib.sha256(f.read_bytes()).hexdigest();current[name]=sha
        if sha!=known.get(name):tar.add(f,arcname=name,recursive=False)
        modes[name]=f.stat().st_mode&0o777
print(json.dumps({'omitted_symlinks':omitted,'modes':modes,'current':current}))
'''

SYNC = r'''
import hashlib,json,os,sys,tarfile,tempfile
from pathlib import Path,PurePosixPath
p=json.loads(sys.argv[1]);root=Path(p['root'])
with tarfile.open(p['archive']) as tar:
    plan=json.load(tar.extractfile('plan.json'))
    def path(name):
        parts=PurePosixPath(name).parts
        if not parts or name.startswith('/') or '..' in parts or any(x in {'.git','.repo-code'} for x in parts):raise RuntimeError('SYNC_PATH_DENIED')
        f=root/name
        if any(q.is_symlink() for q in [f,*f.parents] if q==root or root in q.parents):raise RuntimeError('SYNC_LINK_DENIED')
        return f
    for name,before in plan['before'].items():
        f=path(name);actual='sha256:'+hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
        if actual!=before:raise RuntimeError('SYNC_STALE_VERSION:'+name)
    for name in plan['delete']:path(name).unlink()
    for name,mode in plan['write'].items():
        f=path(name);f.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(dir=f.parent)
        try:
            with os.fdopen(fd,'wb') as out:out.write(tar.extractfile('files/'+name).read())
            os.chmod(tmp,mode);os.replace(tmp,f)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
print(json.dumps({'applied':True,'paths':sorted(plan['before'])}))
'''

PROCESS = r'''
import json,os,signal,subprocess,sys,tempfile,time
from pathlib import Path
p=json.loads(sys.argv[1]);started=time.monotonic();timed_out=False
with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
    try:
        proc=subprocess.Popen(p['argv'],cwd=p['root'],stdin=subprocess.PIPE,stdout=out,stderr=err,start_new_session=True)
    except (FileNotFoundError,PermissionError) as exc:
        missing=isinstance(exc,FileNotFoundError)
        print(json.dumps({'exit_code':127 if missing else 126,'stdout':'',
            'stderr':'Executable or working directory not found' if missing else 'Process launch permission denied',
            'timeout':False,'elapsed':time.monotonic()-started,
            'launch_error':'EXECUTABLE_OR_CWD_NOT_FOUND' if missing else 'PROCESS_LAUNCH_DENIED'}))
        sys.exit(0)
    try:
        proc.communicate(p['stdin'].encode(),timeout=p['timeout'])
    except subprocess.TimeoutExpired:
        timed_out=True
    finally:
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        proc.wait(timeout=5)
    deadline=time.monotonic()+5
    while True:
        active=False
        for entry in Path('/proc').glob('[0-9]*/stat'):
            try:
                parts=entry.read_text().rsplit(')',1)[1].split()
                if int(parts[2])==proc.pid and parts[0]!='Z':active=True;break
            except (OSError,ValueError,IndexError):pass
        if not active:break
        if time.monotonic()>deadline:raise RuntimeError('RESOURCE_LIFETIME_UNKNOWN')
        time.sleep(.01)
    out.seek(0);err.seek(0)
    print(json.dumps({'exit_code':proc.returncode,'stdout':out.read(4*1024*1024).decode(errors='replace'),
        'stderr':err.read(4*1024*1024).decode(errors='replace'),'timeout':timed_out,'elapsed':time.monotonic()-started}))
'''


class HarborSandbox:
    """No local generated code execution and no internal official verdicts.

    Source operations retain the original Broker/Mutator version checks and
    journal. Commands execute only in the current official task container.
    Failed transport leaves a durable uncertain lease/sync, never blind replay.
    """
    mode='harbor'
    isolated=True

    def __init__(self,store,environment,root,image_identity,process_cwd=None):
        self.store,self.environment,self.root=store,environment,root
        self.image=image_identity;self.kit=None;self.synced={};self.modes={}
        self.process_cwd=process_cwd or root
        self.identity={'mode':'harbor','task_image':image_identity,'root':root,
            'repo_code_build':digest({p.name:digest(p.read_bytes()) for p in Path(__file__).parents[1].joinpath('repo_code').glob('*.py')}),
            'bridge_build':digest(Path(__file__).read_bytes()),'protocol':'mirror-broker-v1'}
        self.identity['process_cwd']=self.process_cwd
        self.identity['adapter_build']=digest({p.name:digest(p.read_bytes()) for p in Path(__file__).parent.glob('*.py')})

    @property
    def environment_digest(self):return digest(self.identity)
    def prepare(self):pass

    async def remote(self,script,payload,timeout=120):
        result=await self.environment.exec('python3 -I -c '+shlex.quote(script)+' '+shlex.quote(json.dumps(payload)),timeout_sec=timeout)
        if result.return_code:raise RepoError('HARBOR_TRANSPORT_FAILED',(result.stderr or result.stdout or '')[-1000:])
        return json.loads(result.stdout or '{}')

    async def export(self,destination):
        name='/tmp/'+uid('repo-code-export-')+'.tar'
        destination=host_path(destination);destination.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='repo-code-transfer-') as tmp:
            known=Path(tmp)/'known.json';known.write_text(json.dumps({p:v['blob'] for p,v in self.synced.items()}),encoding='utf-8')
            remote_known=name+'.json';await self.environment.upload_file(known,remote_known)
            meta=await self.remote(EXPORT,{'root':self.root,'archive':name,'known':remote_known})
            archive=Path(tmp)/'source.tar'
            await self.environment.download_file(name,archive)
            extracted=set()
            try:
                with tarfile.open(archive) as tar:
                    for member in tar:
                        extracted.add(relative_path(member.name))
                        if not member.isfile():raise RepoError('HARBOR_EXPORT_NONREGULAR')
                    tar.extractall(destination,filter='data')
                downloaded=Workspace(destination,self.store).manifest()['files']
            except OSError as exc:
                path=Path(exc.filename).name if exc.filename else '<unknown>'
                raise RepoError('HARBOR_SOURCE_MATERIALIZATION_FAILED',
                                f'HARBOR_SOURCE_MATERIALIZATION_FAILED: {path}: host filesystem error (errno {exc.errno})') from exc
        files={}
        for p,sha in meta['current'].items():
            relative_path(p)
            item=downloaded.get(p,self.synced.get(p))
            if p not in downloaded and (p in extracted or item is None or item['blob']!=sha):
                raise RepoError('HARBOR_SOURCE_MATERIALIZATION_FAILED',
                                f'HARBOR_SOURCE_MATERIALIZATION_FAILED: {p}: expected source file missing')
            if item['blob']!=sha:raise RepoError('HARBOR_EXPORT_DIGEST_MISMATCH')
            files[p]=item
        self.modes.update(meta['modes'])
        return files,meta

    async def sync(self,workspace):
        files=Workspace(workspace,self.store).manifest()['files']
        changed=sorted(p for p in self.synced.keys()|files.keys() if self.synced.get(p)!=files.get(p))
        if not changed:return
        plan={'before':{p:self.synced[p]['blob'] if p in self.synced else None for p in changed},
            'write':{p:self.modes.get(p,0o644) for p in changed if p in files},'delete':[p for p in changed if p not in files]}
        operation=uid('sync-');self.store.put('harbor_sync',operation,{'state':'PREPARED','plan':plan})
        try:
            with tempfile.TemporaryDirectory(prefix='repo-code-transfer-') as tmp:
                archive=Path(tmp)/'sync.tar'
                with tarfile.open(archive,'w') as tar:
                    data=json.dumps(plan).encode();entry=tarfile.TarInfo('plan.json');entry.size=len(data);tar.addfile(entry,io.BytesIO(data))
                    for p in plan['write']:
                        data=self.store.read_blob(files[p]['blob']);entry=tarfile.TarInfo('files/'+p);entry.size=len(data);tar.addfile(entry,io.BytesIO(data))
                remote='/tmp/'+operation+'.tar'
                await self.environment.upload_file(archive,remote)
                result=await self.remote(SYNC,{'root':self.root,'archive':remote})
            if result.get('applied') is not True:raise RepoError('HARBOR_SYNC_UNCONFIRMED')
        except asyncio.CancelledError:
            self.store.put('harbor_sync',operation,{'state':'UNCERTAIN','plan':plan})
            await asyncio.shield(self.environment.stop(delete=True))
            self.store.put('harbor_sync',operation,{'state':'CANCELLED','plan':plan})
            raise
        except Exception:
            self.store.put('harbor_sync',operation,{'state':'UNCERTAIN','plan':plan})
            raise RepoError('RECOVERY_REQUIRED','Harbor source sync outcome is uncertain') from None
        self.synced=files
        self.store.put('harbor_sync',operation,{'state':'COMMITTED','plan':plan})

    async def run(self,run_id,workspace,argv,stdin='',timeout=30,export=False,readonly=False,cancelled=None):
        if readonly:raise RepoError('HARBOR_EVOLVED_CODE_UNSUPPORTED')
        if not isinstance(argv,list) or not argv or any(not isinstance(a,str) or '\0' in a for a in argv):raise RepoError('INVALID_ARGV')
        if cancelled and cancelled():raise RepoError('RUN_NOT_EXECUTABLE')
        await self.sync(workspace)
        lease=uid('harbor-lease-');record={'id':lease,'run_id':run_id,'state':'OWNED','mode':'harbor','container':getattr(self.environment,'_owned_container',None)}
        self.store.put('lease',lease,record)
        try:
            result=await self.remote(PROCESS,{'root':self.process_cwd,'argv':argv,'stdin':stdin,'timeout':timeout},timeout=int(timeout)+15)
            with tempfile.TemporaryDirectory(dir=self.store.root) as tmp:
                files,meta=await self.export(tmp)
                snapshot=self.store.json_blob({'files':files})
            self.synced=files
            result.update(lease_id=lease,running=False,cancelled=False,isolated=True,
                environment_digest=self.environment_digest,output_snapshot=snapshot)
            record.update(state='STOPPED',result=result);self.store.put('lease',lease,record)
            return result
        except asyncio.CancelledError:
            # Killing the Harbor exec client alone cannot prove remote process
            # death. Stop the owned container before acknowledging cancellation.
            try:await asyncio.shield(self.environment.stop(delete=True))
            except Exception:
                record['state']='UNCERTAIN';self.store.put('lease',lease,record);raise
            record['state']='STOPPED';self.store.put('lease',lease,record);raise
        except Exception:
            record['state']='UNCERTAIN';self.store.put('lease',lease,record)
            raise RepoError('RESOURCE_LIFETIME_UNKNOWN','Harbor transport or source export failed') from None

    def start(self,*args,**kwargs):raise RepoError('HARBOR_ASYNC_PROCESS_UNSUPPORTED')
    def status(self,lease_id,stop=False):
        lease=self.store.get('lease',lease_id)
        if not lease or lease['state']!='STOPPED':raise RepoError('RESOURCE_LIFETIME_UNKNOWN')
        return lease.get('result',{'running':False,'cancelled':True})
    def resources_stopped(self,run_id):
        return (all(x['state']=='STOPPED' for x in self.store.values('lease') if x['run_id']==run_id)
            and not any(x['state']=='UNCERTAIN' for x in self.store.values('harbor_sync')))
    def stop_run(self,run_id):return self.resources_stopped(run_id)
