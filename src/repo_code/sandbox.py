from __future__ import annotations
import asyncio
import contextlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import threading
import importlib.metadata
from pathlib import Path
from .util import RepoError, digest, canonical, uid, now, private_dir, atomic_write
from .workspace import Workspace, file_lock

BASE_PACKAGES=("fastapi","starlette","pydantic","pydantic_core","typing_extensions","annotated_types","annotated_doc","typing_inspection","anyio","sniffio","idna","httpx","httpcore","h11","certifi","pytest","_pytest","pluggy","packaging","iniconfig","pygments","opentelemetry")

class Sandbox:
    """No implicit downgrade. native = Linux chroot + unique UID + seccomp.
    local is explicit developer mode and NEVER produces trusted verdicts.
    docker requires a preinstalled digest-pinned image.
    """
    def __init__(self,store,mode="auto",image=None):
        self.store=store;self.image=image
        package=Path(__file__).parent
        self._host_build_digest=digest({p.relative_to(package).as_posix():digest(p.read_bytes()) for p in sorted(package.rglob("*.py"))})
        self.mode=("native" if sys.platform=="linux" and os.geteuid()==0 else "docker") if mode=="auto" else mode
        if self.mode not in {"native","docker","local"}:raise RepoError("INVALID_SANDBOX")
        if self.mode=="native" and (sys.platform!="linux" or os.geteuid()!=0): raise RepoError("SANDBOX_UNAVAILABLE")
        if self.mode=="docker":
            if not image or not re.fullmatch(r"\S+@sha256:[0-9a-fA-F]{64}",image):raise RepoError("PINNED_IMAGE_REQUIRED")
            if not shutil.which("docker"):raise RepoError("SANDBOX_UNAVAILABLE","Docker missing; no local fallback")
            subprocess.run(["docker","image","inspect",image],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
        self.kit=self.store.root/"sandbox_runtime"
        self._processes={};self._external={}
        self._lease_locks={};self._locks_guard=threading.Lock()
    @property
    def isolated(self):return self.mode in {"native","docker"}
    def environment(self):
        versions={}
        for name in ("pydantic","fastapi","pytest","httpx"):
            try:versions[name]=importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:versions[name]=None
        return {"dependencies":versions,"mode":self.mode,"python":sys.version.split()[0],"image":self.image,"launcher":digest(Path(__file__).with_name("sandbox_entry.py").read_bytes()),"protocol":1,"host_build_digest":self._host_build_digest}
    @property
    def environment_digest(self):return digest(self.environment())
    def prepare(self):
        if self.mode!="native":return
        with file_lock(self.store.root/"sandbox-build.lock"):
            if (self.kit/"READY").exists():return
            if self.kit.exists():shutil.rmtree(self.kit)
            self.kit.mkdir(mode=0o755)
            python=Path("/usr/bin/python3").resolve()
            if not python.exists():raise RepoError("SYSTEM_PYTHON_REQUIRED")
            info=json.loads(subprocess.check_output([str(python),"-c","import sysconfig,json;print(json.dumps({'stdlib':sysconfig.get_path('stdlib'),'version':sysconfig.get_python_version()}))"]))
            def copy_file(p):
                p=Path(p);dest=self.kit/str(p).lstrip("/");dest.parent.mkdir(parents=True,exist_ok=True)
                if not dest.exists():shutil.copy2(p,dest,follow_symlinks=True);dest.chmod(0o755 if os.access(p,os.X_OK) else 0o644)
            copy_file(python)
            (self.kit/"usr/bin/python3").symlink_to(python.name)
            # Shell-based probes use PATH rather than the direct argv alias.
            (self.kit/"usr/bin/python").symlink_to("python3")
            std=Path(info["stdlib"]);dest=self.kit/str(std).lstrip("/")
            shutil.copytree(std,dest,ignore=shutil.ignore_patterns("__pycache__","test","tests","site-packages","dist-packages"),dirs_exist_ok=True)
            site=dest/"site-packages";site.mkdir(exist_ok=True)
            # Debian system Python searches dist-packages even with -I.
            deb=self.kit/"usr/lib/python3";deb.mkdir(exist_ok=True)
            (deb/"dist-packages").symlink_to("../python"+info["version"]+"/site-packages")
            for name in BASE_PACKAGES:
                spec=importlib.util.find_spec(name)
                if spec is None:continue
                if spec.submodule_search_locations:
                    # Namespace packages have no origin/__init__.py and can
                    # span multiple installed directories. Copy every portion.
                    for location in spec.submodule_search_locations:
                        shutil.copytree(Path(location),site.joinpath(*name.split(".")),ignore=shutil.ignore_patterns("__pycache__","tests"),dirs_exist_ok=True)
                elif spec.origin:
                    src=Path(spec.origin);shutil.copy2(src,site/src.name)
            binaries=[python,Path("/bin/sh").resolve()]
            for b in ["/bin/bash","/bin/ls","/bin/cat","/bin/mkdir","/bin/cp","/bin/mv","/bin/rm","/usr/bin/env","/bin/sleep","/usr/bin/timeout"]:
                if Path(b).exists():binaries.append(Path(b))
            for b in binaries:copy_file(b)
            (self.kit/"bin").mkdir(exist_ok=True)
            if not (self.kit/"bin/sh").exists():shutil.copy2(Path("/bin/sh").resolve(),self.kit/"bin/sh")
            # Collect ELF dependencies of the interpreter, stdlib modules and optional packages.
            elfs=[*binaries,*dest.rglob("*.so")]
            for f in elfs:
                out=subprocess.run(["ldd",str(f)],capture_output=True,text=True,timeout=10).stdout
                for dep in re.findall(r"(?:=>\s*)?(/[^\s()]+)",out):
                    if Path(dep).is_file():copy_file(dep)
            for p in self.kit.rglob("*"):
                if p.is_symlink():continue
                p.chmod(0o755 if p.is_dir() or os.access(p,os.X_OK) else 0o644)
            (self.kit/"READY").write_text(canonical(self.environment()).decode())
    def _jail(self,workspace,readonly=False):
        self.prepare()
        root=Path(tempfile.mkdtemp(prefix="jail-",dir=private_dir(self.store.root/"jails")))
        shutil.copytree(self.kit,root,dirs_exist_ok=True,copy_function=os.link,symlinks=True)
        user=100000+secrets.randbelow(1000000000)
        target=root/"workspace";shutil.copytree(workspace,target,ignore=shutil.ignore_patterns(".git",".venv","__pycache__",".pytest_cache"))
        for p in [target,*target.rglob("*")]:
            if p.is_symlink():raise RepoError("LINK_DENIED")
            if readonly: p.chmod(0o555 if p.is_dir() else 0o444)
            else:os.chown(p,user,user);p.chmod(0o700 if p.is_dir() else 0o600)
        tmp=root/"tmp";tmp.mkdir(exist_ok=True);os.chown(tmp,user,user);tmp.chmod(0o700)
        root.chmod(0o755)
        return root,user
    def start(self,run_id,workspace,argv,stdin="",timeout=30,export=True,readonly=False):
        if not isinstance(argv,list) or not argv or any(not isinstance(s,str) or "\x00" in s for s in argv):raise RepoError("INVALID_ARGV")
        lease_id=uid("lease-");root=None
        self.store.put("lease",lease_id,{"id":lease_id,"run_id":run_id,"state":"RESERVED","protocol":1,"mode":self.mode,"created":now()})
        workspace=Path(workspace)
        if self.mode=="native":
            root,user=self._jail(workspace,readonly)
            args=list(argv)
            aliases={"python":"/usr/bin/python3","python3":"/usr/bin/python3","sh":"/bin/sh","bash":"/bin/bash","pytest":"/usr/bin/python3"}
            if args[0]=="pytest":args=["/usr/bin/python3","-m","pytest",*args[1:]]
            else:args[0]=aliases.get(args[0],args[0] if args[0].startswith("/") else "/bin/"+args[0])
            cfg={"root":str(root),"uid":user,"argv":args,"cpu_seconds":max(1,int(timeout)+1),"memory_bytes":768*1024*1024,"max_output":4*1024*1024}
            cmd=[sys.executable,"-I","-S",str(Path(__file__).with_name("sandbox_entry.py")),json.dumps(cfg)]
            cwd=None
        elif self.mode=="docker":
            root=Path(tempfile.mkdtemp(prefix="container-",dir=private_dir(self.store.root/"jails")))
            shutil.copytree(workspace,root/"workspace");(root/"workspace").chmod(0o777)
            for p in (root/"workspace").rglob("*"):
                if p.is_symlink():raise RepoError("LINK_DENIED")
                p.chmod(0o777 if p.is_dir() else 0o666)
            name="repo-code-"+lease_id
            cmd=["docker","run","-i","--rm","--name",name,"--network=none","--read-only","--cap-drop=ALL","--security-opt=no-new-privileges","--pids-limit=32","--memory=768m","--cpus=1","--user=65534:65534","--tmpfs=/tmp:rw,nosuid,size=64m","-v",f"{root/'workspace'}:/workspace"+(":ro" if readonly else ""),"-w","/workspace",self.image,*argv];cwd=None
        else:
            cmd=argv;cwd=workspace
        out=tempfile.TemporaryFile();err=tempfile.TemporaryFile()
        env={k:v for k,v in os.environ.items() if k in {"PATH","SYSTEMROOT","WINDIR","LANG"}}
        env.update({"PYTHONDONTWRITEBYTECODE":"1","PYTHONNOUSERSITE":"1"})
        try:
            proc=subprocess.Popen(cmd,cwd=cwd,env=env,stdin=subprocess.PIPE,stdout=out,stderr=err,start_new_session=os.name=="posix")
            start_ticks=None
            if sys.platform=="linux":
                with contextlib.suppress(OSError):start_ticks=Path(f"/proc/{proc.pid}/stat").read_text().split()[21]
            self._processes[lease_id]={"proc":proc,"out":out,"err":err,"root":root,"workspace":workspace,"export":export,"start":time.monotonic(),"timeout":timeout}
            self.store.put("lease",lease_id,{"id":lease_id,"run_id":run_id,"state":"OWNED","protocol":1,"pid":proc.pid,"start_ticks":start_ticks,"mode":self.mode,"created":now(),"root":str(root) if root else None})
            # Do not block admission/cancellation on a DUT that refuses to read.
            # The owner can now kill this lease while the bounded stdin drains.
            def send_input():
                try:proc.stdin.write(stdin.encode());proc.stdin.close()
                except (BrokenPipeError,OSError,ValueError):pass
            threading.Thread(target=send_input,name="repo-code-stdin",daemon=True).start()
            return lease_id
        except BaseException:
            out.close();err.close()
            if root:shutil.rmtree(root,ignore_errors=True)
            raise
    def _group_quiet(self,pgid,timeout=2):
        if sys.platform!="linux":return True
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            active=False
            for f in Path("/proc").glob("[0-9]*/stat"):
                try:
                    parts=f.read_text().rsplit(")",1)[1].split()
                    if int(parts[2])==pgid and parts[0] not in {"Z","X"}:active=True;break
                except (OSError,ValueError,IndexError):continue
            if not active:return True
            time.sleep(.01)
        return False
    def _kill(self,item):
        proc=item["proc"]
        if self.mode=="docker":
            # Container IDs are owned, generated before execution and never caller supplied.
            for lid,p in self._processes.items():
                if p is item:
                    subprocess.run(["docker","rm","-f","repo-code-"+lid],capture_output=True,timeout=10)
        if os.name=="posix":
            with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGKILL)
        else:
            subprocess.run(["taskkill","/PID",str(proc.pid),"/T","/F"],capture_output=True,timeout=10)
        with contextlib.suppress(subprocess.TimeoutExpired):proc.wait(timeout=3)
    def status(self,lease_id,stop=False):
        with self._locks_guard: lock=self._lease_locks.setdefault(lease_id,threading.RLock())
        with lock:return self._status_locked(lease_id,stop)
    def _status_locked(self,lease_id,stop=False):
        saved=self.store.get("lease",lease_id)
        if not saved:raise RepoError("LEASE_NOT_FOUND")
        if saved["state"]=="STOPPED":return saved["result"]
        external=self._external.get(lease_id)
        if external:
            if not stop:return {"lease_id":lease_id,"running":external["process"].returncode is None}
            with contextlib.suppress(ProcessLookupError):os.killpg(external["process"].pid,signal.SIGKILL)
            if not self._group_quiet(external["process"].pid):raise RepoError("RESOURCE_LIFETIME_UNKNOWN")
            result={"lease_id":lease_id,"running":False,"cancelled":True}
            saved.update(state="STOPPED",result=result);self.store.put("lease",lease_id,saved)
            return result
        item=self._processes.get(lease_id)
        if item is None:raise RepoError("RESOURCE_LIFETIME_UNKNOWN","restart requires reconciliation, not PID guessing")
        elapsed=time.monotonic()-item["start"];timeout=elapsed>item["timeout"]
        if stop or timeout:self._kill(item)
        proc=item["proc"]
        if proc.poll() is None:return {"lease_id":lease_id,"running":True,"elapsed":elapsed}
        # No descendant may retain the workspace after its parent exits.
        if os.name=="posix":
            with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGKILL)
        if os.name=="posix" and not self._group_quiet(proc.pid):raise RepoError("RESOURCE_LIFETIME_UNKNOWN")
        item["out"].seek(0);item["err"].seek(0)
        stdout=item["out"].read(4*1024*1024).decode(errors="replace");stderr=item["err"].read(4*1024*1024).decode(errors="replace")
        result={"lease_id":lease_id,"running":False,"exit_code":proc.returncode,"stdout":stdout,"stderr":stderr,"timeout":timeout,"cancelled":stop,"isolated":self.isolated,"environment_digest":self.environment_digest,"elapsed":elapsed}
        root=item["root"]
        try:
            if root and not stop:
                result["output_snapshot"]=Workspace(root/"workspace",self.store).snapshot()
            if root and item["export"] and not stop:
                ws=Workspace(root/"workspace",self.store);snapshot=ws.snapshot()
                dest=Workspace(item["workspace"],self.store)
                old=dest.manifest()["files"];new=self.store.read_json(snapshot)["files"]
                for rel in set(old)-set(new):dest.path(rel,True).unlink()
                for rel,v in new.items():atomic_write(dest.path(rel,True),self.store.read_blob(v["blob"]),0o600)
                result["workspace_snapshot"]=snapshot
        except Exception as exc:
            result["export_error"]=type(exc).__name__;result["exit_code"]=-1
        finally:
            item["out"].close();item["err"].close()
            if root:shutil.rmtree(root,ignore_errors=True)
            self._processes.pop(lease_id,None)
        saved.update(state="STOPPED",result=result,stopped_at=now());self.store.put("lease",lease_id,saved)
        return result
    async def run(self,run_id,workspace,argv,stdin="",timeout=30,export=False,readonly=False,cancelled=None):
        lease=await asyncio.to_thread(self.start,run_id,workspace,argv,stdin,timeout,export,readonly)
        try:
            while True:
                stop=bool(cancelled and cancelled())
                result=await asyncio.to_thread(self.status,lease,stop)
                if not result.get("running"):return result
                await asyncio.sleep(.025)
        except BaseException:
            await asyncio.to_thread(self.status,lease,True)
            raise
    def stop_run(self,run_id):
        ok=True
        for row in self.store.values("lease"):
            if row["run_id"]==run_id and row["state"]!="STOPPED":
                try:self.status(row["id"],True)
                except RepoError:ok=False
        return ok
    def resources_stopped(self,run_id):return all(x["state"]=="STOPPED" for x in self.store.values("lease") if x["run_id"]==run_id)
