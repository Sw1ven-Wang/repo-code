"""SWE-bench public-input adapter; model tools execute only in an isolated container."""
import asyncio
import hashlib
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import tarfile
import time
from types import SimpleNamespace
import docker
from repo_code.acceptance import Grader
from repo_code.agent import CodingAgent
from repo_code.broker import Broker
from repo_code.governance import Releases
from repo_code.models import Budget
from repo_code.providers import OpenAIProvider
from repo_code.runtime import Runtime
from repo_code.service import RepoCode
from repo_code.storage import Store
from repo_code.util import RepoError, atomic_write, digest
from repo_code.workspace import Workspace, relative_path
from harbor_repo_code.bridge import HarborSandbox, EXPORT
from export_policy import VerifierResetPolicy

ROOT=Path(os.environ.get('REPO_CODE_EVAL_ROOT','/eval')); OUT=ROOT/'pilot2'
DEFAULT_TASK_NETWORK='repo-code-eval-egress'
PUBLIC_FIELDS={'instance_id','repo','base_commit','problem_statement'}
EXCLUDED={'.git','.venv','venv','__pycache__','.pytest_cache','.mypy_cache','.ruff_cache','node_modules'}

def save(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    atomic_write(path,(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())

def projected(row):
    return {k:row[k] for k in PUBLIC_FIELDS}

def shell(argv,cwd,**kw):
    return subprocess.run(argv,cwd=cwd,check=True,capture_output=True,**kw)

class Environment:
    def __init__(self,image,name,activate=True,network_mode=None,network_name=None):
        self.network_policy=self.policy(network_mode,network_name)
        self.client=docker.from_env(timeout=180)
        self.activate=activate
        network=self.ensure_network() if self.network_policy['mode']=='public' else 'none'
        self.container=self.client.containers.create(image,command=['sleep','infinity'],
            entrypoint=[],name=name,network_mode=network,mem_limit='6g',nano_cpus=4_000_000_000,
            pids_limit=512,cap_drop=['ALL'],security_opt=['no-new-privileges:true'],
            environment={'PYTHONDONTWRITEBYTECODE':'1'},working_dir='/testbed')
        self._owned_container=self.container.id
        try:self.container.start()
        except BaseException:
            self.container.remove(force=True,v=True);raise
    @staticmethod
    def policy(mode=None,name=None):
        mode=mode or os.environ.get('REPO_CODE_TASK_NETWORK','public')
        name=name or os.environ.get('REPO_CODE_DOCKER_NETWORK',DEFAULT_TASK_NETWORK)
        if mode not in {'public','offline'}:raise RepoError('INVALID_TASK_NETWORK_POLICY',mode)
        if name in {'host','none','bridge'} or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{1,63}',name):
            raise RepoError('INVALID_TASK_NETWORK_NAME',name)
        return {'mode':mode,'docker_network':name if mode=='public' else 'none',
            'driver':'bridge' if mode=='public' else 'null','published_ports':False,
            'inter_container_connectivity':False,'host_network':False}
    def ensure_network(self):
        name=self.network_policy['docker_network']
        try:network=self.client.networks.get(name)
        except docker.errors.NotFound:
            try:network=self.client.networks.create(name,driver='bridge',internal=False,
                options={'com.docker.network.bridge.enable_icc':'false'},
                labels={'repo-code.owner':'swebench-adapter','repo-code.policy':'public-egress'})
            except docker.errors.APIError as exc:
                if exc.status_code!=409:raise
                network=self.client.networks.get(name)
        network.reload();config=network.attrs
        if (config['Driver']!='bridge' or config.get('Internal') or
            config.get('Labels',{}).get('repo-code.owner')!='swebench-adapter' or
            config.get('Options',{}).get('com.docker.network.bridge.enable_icc')!='false'):
            raise RepoError('UNEXPECTED_DOCKER_NETWORK_CONFIGURATION',name)
        return name
    def inspect_isolation(self):
        self.container.reload();attrs=self.container.attrs;host=attrs['HostConfig']
        expected=self.network_policy['docker_network']
        if (host['NetworkMode']!=expected or attrs.get('Mounts') or host.get('Privileged') or
            host.get('PortBindings') or host.get('CapAdd') or
            'ALL' not in host.get('CapDrop',[]) or
            'no-new-privileges:true' not in host.get('SecurityOpt',[])):
            raise RepoError('CONTAINER_ISOLATION_CHANGED')
        if set(attrs['NetworkSettings']['Networks'])!={expected}:
            raise RepoError('UNEXPECTED_NETWORK_ATTACHMENT')
        return {'container':attrs['Id'],'network_policy':self.network_policy,'mounts':[],
            'privileged':False,'published_ports':False,'cap_drop':host['CapDrop'],
            'security_opt':host['SecurityOpt']}
    async def exec(self,command,cwd=None,timeout_sec=120):
        prefix='source /opt/miniconda3/bin/activate testbed && ' if self.activate else ''
        def execute():
            r=self.container.exec_run(['/bin/bash','-lc',prefix+command],workdir=cwd or '/testbed',demux=True)
            out,err=r.output
            return SimpleNamespace(return_code=r.exit_code,stdout=(out or b'').decode(errors='replace'),stderr=(err or b'').decode(errors='replace'))
        try:return await asyncio.wait_for(asyncio.to_thread(execute),timeout_sec)
        except (TimeoutError,asyncio.CancelledError):
            await asyncio.shield(self.stop(delete=True));raise
    async def upload_file(self,source,destination):
        data=Path(source).read_bytes();buf=io.BytesIO()
        with tarfile.open(fileobj=buf,mode='w') as tar:
            entry=tarfile.TarInfo(PurePosixPath(destination).name);entry.size=len(data);entry.mode=0o600
            tar.addfile(entry,io.BytesIO(data))
        if not await asyncio.to_thread(self.container.put_archive,str(PurePosixPath(destination).parent),buf.getvalue()):
            raise RepoError('CONTAINER_UPLOAD_FAILED')
    async def download_file(self,source,destination):
        chunks,_=await asyncio.to_thread(self.container.get_archive,source)
        data=await asyncio.to_thread(lambda:b''.join(chunks))
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            members=tar.getmembers()
            if len(members)!=1 or not members[0].isfile():raise RepoError('CONTAINER_DOWNLOAD_INVALID')
            Path(destination).write_bytes(tar.extractfile(members[0]).read())
    async def stop(self,delete=True):
        try:await asyncio.to_thread(self.container.remove,force=True,v=True)
        except docker.errors.NotFound:pass

# .git remains in the separate trusted Git checkout, never in model workspace.
WALK_EXPORT=EXPORT.replace("names=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=root).decode().split('\\0')",
    "names=[]\nfor base,dirs,files in os.walk(root,followlinks=False):\n    dirs[:]=[d for d in dirs if d not in {'.git','.venv','venv','__pycache__','.pytest_cache','.mypy_cache','.ruff_cache','node_modules'}]\n    names.extend(str((Path(base)/f).relative_to(root)) for f in files)")
assert WALK_EXPORT != EXPORT
ASSET_EXPORT=WALK_EXPORT.replace("known=json.load(open(p['known']))", """known=json.load(open(p['known']))
for name,asset in p.get('readonly_assets',{}).items():
    f=root/name
    if not f.is_file() or f.is_symlink() or f.stat().st_size!=asset['bytes'] or hashlib.sha256(f.read_bytes()).hexdigest()!=asset['sha256']:
        raise RuntimeError('READONLY_ASSET_CHANGED:'+name)
""").replace("if not f.is_file():continue", """if not f.is_file():continue
        asset=p.get('readonly_assets',{}).get(name)
        if asset:
            if f.stat().st_size!=asset['bytes'] or hashlib.sha256(f.read_bytes()).hexdigest()!=asset['sha256']:
                raise RuntimeError('READONLY_ASSET_CHANGED:'+name)
            continue""")

def asset_profiles():
    path=OUT/'config/workspace_view.json'
    return json.loads(path.read_text())['readonly_assets_by_task'] if path.exists() else {}

class TaskSandbox(HarborSandbox):
    mode='swebench'
    supports_background=False
    def __init__(self,store,environment,images,build):
        super().__init__(store,environment,'/testbed',images)
        self.identity.update(protocol='swebench-public-v1',suite_images=images,adapter_build=build,readonly_assets=asset_profiles())
        self.identity['network_policy']=environment.network_policy
    async def remote(self,script,payload,timeout=120):
        if script==EXPORT:
            script=ASSET_EXPORT;payload={**payload,'readonly_assets':getattr(self.environment,'readonly_assets',{})}
        return await super().remote(script,payload,timeout)

class TaskRuntime(Runtime):
    def create(self,goal,*args,**kwargs):
        from write_policy import PUBLIC_POLICY_HINT
        if PUBLIC_POLICY_HINT not in goal:goal+='\n\nLocal candidate policy: '+PUBLIC_POLICY_HINT
        return super().create(goal,*args,**kwargs)


class TaskBroker(Broker):
    unavailable=frozenset({'delegate','mcp.call','mcp.discover','process.start','process.status','process.stop'})
    def available_tools(self,run):
        return super().available_tools(run)-self.unavailable
    def register_call(self,run_id,turn_id,call):
        if call.name in self.unavailable:
            raise RepoError('SWE_TOOL_UNAVAILABLE','This task supports synchronous process.run and workspace.diff; delegation, MCP and background leases are not available.')
        return super().register_call(run_id,turn_id,call)


class App(RepoCode):
    def __init__(self,state,provider,env,images,build,memory_profile=None,memory_encoder=None,verifier_policy=None):
        self.provider=provider;self.store=Store(state)
        self.sandbox=TaskSandbox(self.store,env,images,build)
        self.runtime=TaskRuntime(self.store,self.sandbox)
        self.releases=Releases(self.store,self.sandbox);self.releases.bootstrap()
        self.broker=TaskBroker(self.store,self.runtime,self.sandbox,provider)
        from write_policy import PublicTestWritePolicy
        self.broker.write_validator=PublicTestWritePolicy(verifier_policy)
        self.grader=Grader(self.store,self.sandbox)
        self.agent=CodingAgent(self.store,self.runtime,self.broker,self.grader,self.releases,provider)
        self.memory=None
        if memory_profile is not None:
            from repo_code.memory.service import MemoryService
            self.memory=MemoryService(self.store,self.runtime,provider,memory_profile,memory_encoder)
            self.runtime.memory=self.broker.memory=self.agent.memory=self.memory

class CostProvider(OpenAIProvider):
    """Sequential pilot-wide ceiling; unknown Usage retains reserve and stops new calls."""
    def __init__(self,ledger,usage,**kwargs):
        super().__init__(**kwargs);self.ledger=Path(ledger);self.usage_path=Path(usage);self.records=[]
    async def complete(self,system,messages,tools,on_delta=None):
        ledger=json.loads(self.ledger.read_text())
        if ledger.get('halt'):raise RepoError('PILOT_COST_OR_PROTOCOL_STOP')
        # UTF-8 bytes + protocol margin exceeds token count for these text requests.
        input_upper=len(json.dumps([system,messages,tools],ensure_ascii=False).encode())+8192
        reserve=(input_upper*2+self.max_tokens*8)/1e6
        if ledger['charged_upper_cny']+reserve>ledger['limit_cny']:
            raise RepoError('PILOT_COST_LIMIT')
        ledger['charged_upper_cny']+=reserve;save(self.ledger,ledger)
        rec={'call':len(self.records)+1,'started':time.time(),'reserve_cny':reserve,'status':'STARTED'}
        self.records.append(rec);save(self.usage_path,self.records)
        usage={};error=None
        try:
            reply=await super().complete(system,messages,tools,on_delta)
            usage=reply.usage
            if not usage or usage.get('total_tokens') is None:raise RepoError('PROVIDER_USAGE_UNAVAILABLE')
            rec['status']='COMPLETED'
            return reply
        except BaseException as exc:
            error=getattr(exc,'code',type(exc).__name__)
            usage=getattr(self,'last_usage',{}) or usage
            rec.update(status='FAILED',error_code=error)
            raise
        finally:
            ledger=json.loads(self.ledger.read_text())
            if usage and usage.get('prompt_tokens') is not None and usage.get('completion_tokens') is not None:
                upper=(usage['prompt_tokens']*2+usage['completion_tokens']*8)/1e6
                ledger['charged_upper_cny']+=upper-reserve
                rec.update(usage=usage,charged_upper_cny=upper)
                if upper>reserve:ledger['halt']='RESERVE_BOUND_VIOLATED'
            else:ledger['halt']='UNKNOWN_USAGE_RETAINED_RESERVE'
            if error:ledger['halt']=error
            save(self.ledger,ledger)
            rec['finished']=time.time();save(self.usage_path,self.records)

async def prepare_container(row,images,name):
    if set(row)!=PUBLIC_FIELDS:raise RepoError('PUBLIC_PROJECTION_VIOLATION')
    env=Environment(images[row['instance_id']]['id'],name)
    env.readonly_assets=asset_profiles().get(row['instance_id'],{})
    try:
        r=await env.exec('git rev-parse HEAD && git status --porcelain --untracked-files=no')
        if r.return_code or r.stdout.strip()!=row['base_commit']:raise RepoError('BASE_COMMIT_OR_TREE_MISMATCH',r.stdout[:500])
        r=await env.exec("python3 -I -c "+shlex.quote("import os,shutil;from pathlib import Path; p=Path('/testbed/.git');assert p.is_dir() and not p.is_symlink();shutil.rmtree(p);assert not p.exists();assert not any(k in os.environ for k in ['DEEPSEEK_API_KEY_REPO','OPENAI_API_KEY','ANTHROPIC_API_KEY']);assert not any(Path(p).exists() for p in ['/var/run/docker.sock','/eval','/tests/test.sh','/solution/gold_patch.diff'])"))
        if r.return_code:raise RepoError('INPUT_ISOLATION_FAILED',r.stderr[:500])
        env.inspect_isolation()
        return env
    except BaseException:
        await env.stop();raise

def trusted_baseline(row):
    path=Path('/work/pilot2/trusted-git')/row['instance_id']
    if not path.exists():
        path.mkdir(parents=True)
        shell(['git','init'],path)
        shell(['git','config','core.autocrlf','false'],path)
        shell(['git','-c','core.autocrlf=false','fetch','--depth','1','https://github.com/'+row['repo']+'.git',row['base_commit']],path)
        shell(['git','checkout','--detach','FETCH_HEAD'],path)
    head=shell(['git','rev-parse','HEAD'],path).stdout.decode().strip()
    if head!=row['base_commit']:raise RepoError('TRUSTED_BASE_COMMIT_MISMATCH')
    if shell(['git','status','--porcelain'],path).stdout:raise RepoError('TRUSTED_BASE_DIRTY')
    return path

def patch_from_workspace(baseline,source,workspace,replay,verifier_policy=None,regression_proofs=None):
    """Replay only observed file deltas onto the exact original Git commit."""
    if replay.exists():raise RepoError('PATCH_REPLAY_ALREADY_EXISTS')
    if verifier_policy is not None:
        if not isinstance(verifier_policy,VerifierResetPolicy):raise RepoError('INVALID_VERIFIER_POLICY')
        head=shell(['git','rev-parse','HEAD'],baseline).stdout.decode().strip()
        if head!=verifier_policy.base_commit:raise RepoError('VERIFIER_BASE_COMMIT_MISMATCH')
    for root in (source,workspace):
        if root.is_symlink() or any(p.is_symlink() for p in root.rglob('*')):
            raise RepoError('PATCH_SYMLINK_DENIED')
    shell(['git','-c','core.autocrlf=false','clone','--local','--no-hardlinks',str(baseline),str(replay)],baseline.parent)
    shell(['git','config','core.autocrlf','false'],replay)
    before={p.relative_to(source).as_posix():p for p in source.rglob('*') if p.is_file()}
    after={p.relative_to(workspace).as_posix():p for p in workspace.rglob('*') if p.is_file()}
    changed=[]
    for name in sorted(before.keys()|after.keys()):
        a=before[name].read_bytes() if name in before else None
        b=after[name].read_bytes() if name in after else None
        aexec=bool(before[name].stat().st_mode&0o111) if name in before else False
        bexec=bool(after[name].stat().st_mode&0o111) if name in after else False
        if a==b and aexec==bexec:continue
        relative_path(name)
        parts=PurePosixPath(name).parts
        if any(p in {'.git','.repo-code','.github'} for p in parts):raise RepoError('PATCH_CONTROL_PATH_DENIED',name)
        if name not in before and subprocess.run(['git','check-ignore','--no-index','--',name],cwd=baseline,capture_output=True).returncode==0:
            continue
        # Public tests reset by the official verifier cannot override its tests.
        # Preserve the complete candidate, rather than filtering those edits.
        is_test=any(p in {'tests','test','testing'} for p in parts) or Path(name).name.startswith('test_')
        restored=verifier_policy is not None and name in verifier_policy.restored_paths
        proof=(regression_proofs or {}).get(name)
        if proof is not None:
            from regression_fixtures import RuleFixtureAddition
            if not isinstance(proof,RuleFixtureAddition) or not proof.verify(name,a,b):raise RepoError('INVALID_REGRESSION_ADDITION_PROOF',name)
        if (is_test and name in before and not restored and proof is None) or Path(name).name in {'pytest.ini','tox.ini','conftest.py','setup.cfg','pyproject.toml'}:
            raise RepoError('PATCH_TEST_OR_JUDGE_CHANGE_DENIED',name)
        if (a is not None and b'\0' in a) or (b is not None and b'\0' in b):raise RepoError('PATCH_BINARY_DENIED',name)
        target=replay/name
        if target.is_symlink() or any(p.is_symlink() for p in target.parents if replay in p.parents):raise RepoError('PATCH_SYMLINK_DENIED',name)
        if b is None:target.unlink()
        else:
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b)
            target.chmod(0o755 if bexec else 0o644)
        changed.append(name)
    shell(['git','add','-N','--','.'],replay)
    patch=shell(['git','diff','--binary','HEAD'],replay).stdout
    if patch:
        # Validate the actual frozen patch on another clean, exact baseline.
        result=subprocess.run(['git','apply','--check','-'],cwd=baseline,input=patch,capture_output=True)
        if result.returncode:raise RepoError('PATCH_APPLY_CHECK_FAILED',result.stderr.decode(errors='replace')[:1000])
    return patch,changed
