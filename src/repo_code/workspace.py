"""Version-safe workspace API. File effects are not a SQLite transaction."""
from __future__ import annotations
import contextlib
import difflib
import fnmatch
import json
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from .util import RepoError, digest, canonical, atomic_write, Faults

EXCLUDE = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
CONTROL = {".git", ".repo-code", ".mini_pi", ".repoforge"}

@contextlib.contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a+b") as f:
        if os.name=="nt":
            import msvcrt
            f.write(b"0");f.flush();f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(f,fcntl.LOCK_EX)
        try: yield
        finally:
            if os.name=="nt": f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(f,fcntl.LOCK_UN)


def relative_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value or ":" in value or value.startswith("/"):
        raise RepoError("PATH_DENIED",value)
    p=PurePosixPath(value)
    if any(x in {"..","."} for x in value.split("/")) or PureWindowsPath(value).drive:
        raise RepoError("PATH_DENIED",value)
    for part in p.parts:
        base=part.split(".")[0].upper()
        if base in {"CON","PRN","AUX","NUL"} or re.fullmatch(r"(COM|LPT)[1-9]",base) or part.endswith((" ",".")):
            raise RepoError("PATH_DENIED",value)
    return p.as_posix()

class Workspace:
    def __init__(self, root: Path | str, store):
        self.root=Path(root).resolve(strict=True);self.store=store
        if not self.root.is_dir(): raise RepoError("NOT_DIRECTORY")
    def path(self, value: str, writing=False):
        rel=relative_path(value); cur=self.root
        for part in PurePosixPath(rel).parts:
            if part in CONTROL: raise RepoError("CONTROL_PATH_DENIED")
            cur=cur/part
            if cur.is_symlink(): raise RepoError("LINK_DENIED",rel)
        if not cur.resolve(strict=False).is_relative_to(self.root): raise RepoError("PATH_ESCAPE")
        if cur.exists() and not cur.is_file() and not cur.is_dir(): raise RepoError("SPECIAL_FILE_DENIED")
        return cur
    def read(self,path):
        p=self.path(path)
        if not p.is_file(): raise RepoError("NOT_FILE",path)
        if p.stat().st_size>8*1024*1024: raise RepoError("FILE_TOO_LARGE")
        data=p.read_bytes()
        return {"path":path,"version":digest(data),"content":data.decode("utf-8-sig"),"artifact":self.store.blob(data)}
    def manifest(self, *, stats=None):
        entries={};total=0
        for base,dirs,files in os.walk(self.root,followlinks=False):
            dirs[:]=sorted(d for d in dirs if d not in EXCLUDE)
            for d in dirs:
                p=Path(base)/d
                if p.is_symlink(): raise RepoError("LINK_DENIED",p.name)
                if d in CONTROL: raise RepoError("CONTROL_PATH_DENIED")
            for name in sorted(files):
                p=Path(base)/name;rel=p.relative_to(self.root).as_posix()
                self.path(rel)
                s=p.stat()
                if not stat.S_ISREG(s.st_mode): raise RepoError("SPECIAL_FILE_DENIED")
                if s.st_size>8*1024*1024: raise RepoError("FILE_TOO_LARGE")
                total+=s.st_size
                if total>128*1024*1024 or len(entries)>=20000: raise RepoError("WORKSPACE_LIMIT")
                entries[rel]={"blob":self.store.blob(p.read_bytes()),"executable":bool(s.st_mode&0o111)}
        if stats is not None:
            stats["preparation_worktree_bytes"] = total
        return {"files":entries}
    def snapshot(self): return self.store.json_blob(self.manifest())
    def restore(self,snapshot,empty_only=True):
        if empty_only and any(self.root.iterdir()): raise RepoError("DESTINATION_NOT_EMPTY")
        data=self.store.read_json(snapshot)
        for rel,item in data["files"].items():
            p=self.path(rel,True)
            atomic_write(p,self.store.read_blob(item["blob"]),0o700 if item["executable"] else 0o600)
    def diff(self,baseline):
        old=self.store.read_json(baseline)["files"];new=self.manifest()["files"];parts=[]
        for rel in sorted(old.keys()|new.keys()):
            a=self.store.read_blob(old[rel]["blob"]) if rel in old else b""
            b=self.store.read_blob(new[rel]["blob"]) if rel in new else b""
            if a!=b:
                parts.extend(difflib.unified_diff(a.decode(errors="replace").splitlines(True),b.decode(errors="replace").splitlines(True),fromfile="a/"+rel,tofile="b/"+rel))
        return "".join(parts)
    def diff_view(self,baseline,path=None,offset=0,length=8000):
        """Bounded public-baseline diagnostic, never a Git patch exporter.

        Binary/large files and mode changes are metadata only. No refs/history
        or files outside this workspace can be selected by the model.
        """
        if path is not None:self.path(path)
        old=self.store.read_json(baseline)['files'];new=self.manifest()['files']
        changed=[p for p in sorted(old.keys()|new.keys()) if old.get(p)!=new.get(p)]
        selected=[path] if path in changed else [] if path else changed
        parts=[];metadata=[]
        for name in selected:
            a=self.store.read_blob(old[name]['blob']) if name in old else b''
            b=self.store.read_blob(new[name]['blob']) if name in new else b''
            row={'path':name,'before':old.get(name),'after':new.get(name),'before_bytes':len(a),'after_bytes':len(b)}
            if len(a)>65536 or len(b)>65536 or b'\0' in a or b'\0' in b:
                row['text_diff']='BINARY_OR_LARGE_FILE_METADATA_ONLY'
            else:
                try:
                    parts.extend(difflib.unified_diff(a.decode('utf-8-sig').splitlines(True),b.decode('utf-8-sig').splitlines(True),fromfile='a/'+name,tofile='b/'+name))
                except UnicodeError:row['text_diff']='BINARY_OR_ENCODING_METADATA_ONLY'
            metadata.append(row)
        text=''.join(parts);end=min(len(text),offset+length)
        return {'baseline':baseline,'changed_paths':changed[:100],'changed_path_count':len(changed),
                'files':metadata[:100],'diff':text[offset:end],'offset':offset,'next_offset':end if end<len(text) else None,
                'truncated':end<len(text) or len(metadata)>100,'diagnostic_only':True,
                'message':'Only changes in the authorized task copy relative to its public starting snapshot. Formal patch export and acceptance are separate.'}
    def guarded_snapshot(self,patterns):
        return digest({p:e for p,e in self.manifest()["files"].items() if any(fnmatch.fnmatchcase(p,x) for x in patterns)})

class AnchorMatchError(RepoError):
    def __init__(self, details, repeated=False):
        if repeated:
            code = "EDIT_ANCHOR_RETRY_BLOCKED"
            message = "This anchor already failed on this file version. Choose a different exact span."
        else:
            code = "AMBIGUOUS_OR_MISSING_MATCH"
            if details["match_count"] == 0:
                message = "The anchor is absent from this file version. Read the target and copy its current text."
            elif details.get("same_match_regions"):
                message = "This different anchor still overlaps the same matches. Use the suggested read windows and include the enclosing definition unique to the intended match."
            else:
                message = "The anchor matches multiple locations. Use the suggested read windows to choose enclosing context unique to the target."
        super().__init__(code, f"{code}. {message}")
        self.anchor_details = details


class Mutator:
    def __init__(self,workspace,run_id,contract=None,faults=None,effect_guard=None):
        self.ws,self.store,self.run_id,self.contract=workspace,workspace.store,run_id,contract
        self.faults=faults or Faults()
        self.effect_guard=effect_guard
    def scope(self,paths):
        if not self.contract: return
        for p in paths:
            if not any(fnmatch.fnmatchcase(p,g) for g in self.contract.allowed_paths): raise RepoError("SCOPE_DENIED",p)
            if any(fnmatch.fnmatchcase(p,g) for g in [*self.contract.forbidden_paths,*self.contract.protected_paths]): raise RepoError("PROTECTED_PATH",p)
        changed=self.store.get("changed",self.run_id,default={"paths":[]})["paths"]
        if len(set(changed)|set(paths))>self.contract.max_changed_files: raise RepoError("FILE_COUNT_LIMIT")
    def anchor_start(self, path, version, text, old):
        key = digest([self.run_id, path, version, digest(old)])
        prior = self.store.get("failed_edit_anchor", key)
        if prior is not None:
            raise AnchorMatchError(prior, repeated=True)
        count = text.count(old)
        if count == 1:
            return text.index(old)
        positions = []
        regions = []
        offset = 0
        for _ in range(min(count, 8)):
            start = text.index(old, offset)
            positions.append({
                "line": text.count("\n", 0, start) + 1,
                "column": start - text.rfind("\n", 0, start),
            })
            offset = start + len(old)
            regions.append((start, offset))
        details = {"path": path, "file_version": version, "anchor_digest": digest(old),
                   "match_count": count, "positions": positions,
                   "positions_truncated": count > len(positions),
                   "read_windows": [{"path": path, "start": start, "lines": 45}
                                    for start in sorted({max(1, pos["line"] - 20) for pos in positions[:4]})]}
        # Record the first failure durably, including when parallel requests
        # discover the same mismatch before either has committed its receipt.
        with self.store.tx() as conn:
            prior = self.store.get("failed_edit_anchor", key, conn)
            if prior is None:
                if 1 < count == len(regions):
                    region_key = digest([self.run_id, path, version])
                    last = self.store.get("ambiguous_edit_regions", region_key, conn, default=[])
                    if len(last) == count and all(a < d and c < b for (a, b), (c, d) in zip(last, regions)):
                        details["same_match_regions"] = True
                    self.store.put("ambiguous_edit_regions", region_key, regions, conn)
                self.store.put("failed_edit_anchor", key, details, conn, immutable=True)
        raise AnchorMatchError(prior or details, repeated=prior is not None)

    def plan(self,operations):
        if not isinstance(operations,list) or not 1<=len(operations)<=50: raise RepoError("INVALID_OPERATION_COUNT")
        touched=set();plans=[]
        for op in operations:
            kind=op.get("kind");paths=[op.get("source"),op.get("destination")] if kind=="move" else [op.get("path")]
            if any(not isinstance(p,str) for p in paths): raise RepoError("INVALID_PATH")
            paths=[relative_path(p) for p in paths]
            if len(set(paths))!=len(paths) or touched.intersection(paths): raise RepoError("CONFLICTING_TARGETS")
            # Check the complete batch prefix together with historical changes.
            touched.update(paths);self.scope(touched)
            before={p:self.store.blob(self.ws.path(p).read_bytes()) if self.ws.path(p).is_file() else None for p in paths}
            if any(self.ws.path(p).is_dir() for p in paths): raise RepoError("NOT_REGULAR_FILE")
            if kind=="create":
                if before[paths[0]] is not None: raise RepoError("FILE_EXISTS")
                after={paths[0]:self.store.blob(op["content"])}
            elif kind in {"edit","delete","move","replace"}:
                p=paths[0]
                if before[p] is None: raise RepoError("FILE_MISSING")
                if op.get("expected_version")!=before[p]: raise RepoError("STALE_VERSION",p)
                if kind=="replace": after={p:self.store.blob(op["content"])}
                elif kind=="delete": after={p:None}
                elif kind=="move":
                    if before[paths[1]] is not None: raise RepoError("DESTINATION_EXISTS")
                    after={p:None,paths[1]:before[p]}
                else:
                    raw=self.store.read_blob(before[p]);text=raw.decode("utf-8-sig");changes=[]
                    # read presents logical lines with LF. Match model snippets
                    # against homogeneous Windows files while retaining CRLF.
                    remainder=text.replace("\r\n","")
                    crlf="\r\n" in text and "\n" not in remainder and "\r" not in remainder
                    replacements=op.get("replacements",[{"old_text":op.get("old_text"),"new_text":op.get("new_text")}])
                    for rep in replacements:
                        old,new=rep.get("old_text"),rep.get("new_text")
                        if not isinstance(old,str) or not old or not isinstance(new,str) or old==new: raise RepoError("INVALID_REPLACEMENT")
                        if crlf:
                            old=old.replace("\r\n","\n").replace("\n","\r\n")
                            new=new.replace("\r\n","\n").replace("\n","\r\n")
                            if old==new:raise RepoError("INVALID_REPLACEMENT")
                        start=self.anchor_start(p, before[p], text, old)
                        changes.append((start,start+len(old),new))
                    changes.sort()
                    if any(a[1]>b[0] for a,b in zip(changes,changes[1:])): raise RepoError("OVERLAPPING_EDITS")
                    for start,end,new in reversed(changes): text=text[:start]+new+text[end:]
                    data=text.encode("utf-8");data=(b"\xef\xbb\xbf"+data) if raw.startswith(b"\xef\xbb\xbf") else data
                    after={p:self.store.blob(data)}
            else: raise RepoError("UNKNOWN_CHANGE_KIND")
            plans.append({"kind":kind,"before":before,"after":after,"state":"PREPARED"})
        return plans
    def matches(self,state):
        for p,expected in state.items():
            q=self.ws.path(p)
            actual=digest(q.read_bytes()) if q.is_file() else None
            if actual!=expected: return False
        return True
    def set_state(self,state):
        for p,key in state.items():
            q=self.ws.path(p,True)
            if key is None:
                if q.exists(): q.unlink()
            else: atomic_write(q,self.store.read_blob(key),0o600)
    def apply(self,operation_id,operations):
        lock=self.store.root/"locks"/(self.run_id+".lock")
        with file_lock(lock):
            plans=self.plan(operations)
            journal={"operation_id":operation_id,"run_id":self.run_id,"plans":plans}
            self.store.put("journal",operation_id,journal,immutable=True)
            self.faults.hit("journal_prepared")
            applied=[]
            try:
                for i,row in enumerate(plans):
                    if not self.matches(row["before"]): raise RepoError("STALE_VERSION")
                    row["state"]="APPLYING";self.store.put("journal",operation_id,journal)
                    self.faults.hit("before_file_effect")
                    # Host admission and this bounded effect share a SQLite writer
                    # ordering. Never keep a stale pre-lock authorization decision.
                    guard = (self.effect_guard(operation_id) if self.effect_guard
                             else contextlib.nullcontext())
                    with guard:
                        if not self.matches(row["before"]):
                            raise RepoError("STALE_VERSION")
                        self.set_state(row["after"])
                        applied.append(row)
                    # Fault callbacks run outside the DB transaction so a test
                    # can admit control from another connection without deadlock.
                    self.faults.hit("after_file_effect")
                    if not self.matches(row["after"]): raise RepoError("POSTSTATE_MISMATCH")
                    row["state"]="APPLIED";self.store.put("journal",operation_id,journal)
                    self.faults.hit("after_file_commit")
            except Exception:
                for row in reversed(applied):
                    if not self.matches(row["after"]):
                        row["state"]="UNCERTAIN";self.store.put("journal",operation_id,journal)
                        raise RepoError("RECOVERY_REQUIRED")
                    self.set_state(row["before"]);row["state"]="ROLLED_BACK"
                self.store.put("journal",operation_id,journal);raise
            paths=list(dict.fromkeys(p for r in plans for p in r["after"]))
            changed=self.store.get("changed",self.run_id,default={"paths":[]})
            self.store.put("changed",self.run_id,{"paths":sorted(set(changed["paths"]+paths))})
            return {"changed_paths":paths,"versions":{p:(digest(self.ws.path(p).read_bytes()) if self.ws.path(p).is_file() else None) for p in paths}}
    def reconcile(self,operation_id):
        journal=self.store.get("journal",operation_id)
        if not journal: raise RepoError("NO_JOURNAL")
        with file_lock(self.store.root/"locks"/(self.run_id+".lock")):
            rows=journal["plans"]
            for row in rows:
                for b in set(row["before"].values())|set(row["after"].values()):
                    if b: self.store.read_blob(b)
            states=["after" if self.matches(r["after"]) else "before" if self.matches(r["before"]) else "unknown" for r in rows]
            if "unknown" in states: raise RepoError("RECOVERY_REQUIRED","external change preserved")
            if all(s=="after" for s in states):
                for r in rows:r["state"]="APPLIED"
                result="APPLIED"
            elif all(s=="before" for s in states): result="NOT_APPLIED"
            else:
                for row,s in reversed(list(zip(rows,states))):
                    if s=="after": self.set_state(row["before"])
                    row["state"]="ROLLED_BACK"
                result="ROLLED_BACK"
            self.store.put("journal",operation_id,journal)
            return result
