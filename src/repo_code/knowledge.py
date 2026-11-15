"""Run-pinned source snapshots, matching BM25 indexes, lifecycle and revocation."""
from __future__ import annotations
import math
import re
from collections import Counter
from .util import digest, RepoError, now, uid
from pathlib import Path

def project_scope(path):
    return "project:"+digest(str(Path(path).resolve()))[7:23]

def terms(s):return re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]",s.lower())

class Knowledge:
    def __init__(self,store):self.store=store
    def draft(self,kind,title,body,scope,sources,preconditions=None):
        if kind not in {"memory","skill"} or not sources:raise RepoError("KNOWLEDGE_PROVENANCE_REQUIRED")
        item={"id":uid("know-"),"kind":kind,"title":title,"body":body,"scope":scope,"sources":sources,"preconditions":preconditions or {},"status":"CANDIDATE","version":1,"created":now()}
        self.store.put("knowledge",item["id"],item)
        from .governance import Lineage
        Lineage(self.store).derive(item["id"],sources)
        return item
    def activate(self,item_id,reviewer):
        if not reviewer:raise RepoError("REVIEW_REQUIRED")
        item=self.store.get("knowledge",item_id)
        if not item or item["status"]!="CANDIDATE":raise RepoError("INVALID_KNOWLEDGE_STATE")
        if item.get("origin")=="evolution":raise RepoError("RELEASE_GATE_REQUIRED","Evolved skills must be evaluated as a full skills_index release")
        item.update(status="ACTIVE",reviewer=reviewer);self.store.put("knowledge",item_id,item);return item
    def merge(self,ids,title,body,reviewer):
        items=[self.store.get("knowledge",i) for i in ids]
        if not items or any(x is None for x in items):raise RepoError("NOT_FOUND")
        if len({digest([x["scope"],x["preconditions"],x["kind"]]) for x in items})!=1:raise RepoError("INCOMPATIBLE_SKILL_SCOPE")
        item=self.draft(items[0]["kind"],title,body,items[0]["scope"],list({s for x in items for s in x["sources"]}),items[0]["preconditions"])
        item["parents"]=ids;self.store.put("knowledge",item["id"],item)
        return item  # Candidate requires fresh review/evaluation; not automatic deployment.
    def archive(self,item_id,reviewer):
        if not reviewer:raise RepoError("REVIEW_REQUIRED")
        item=self.store.get("knowledge",item_id)
        if not item:raise RepoError("NOT_FOUND")
        item["status"]="ARCHIVED";self.store.put("knowledge",item_id,item)
    def revoke(self,item_id):
        from .memory.repository import MemoryRepository
        MemoryRepository(self.store).revoke(item_id)
        item=self.store.get("knowledge",item_id)
        if item:item.update(status="REVOKED",body="");self.store.put("knowledge",item_id,item)
    def pin(self,scope,include_user=True):
        items=[x for x in self.store.values("knowledge") if x["scope"] in ({scope,"user"} if include_user else {scope}) and x["status"]=="ACTIVE" and not self.store.get("revocation",x["id"])]
        entries=[]
        for item in sorted(items,key=lambda x:x["id"]):
            blob=self.store.json_blob(item)
            from .governance import Lineage
            Lineage(self.store).derive(blob,[item["id"]])
            entries.append({"id":item["id"],"digest":blob,"scope":item["scope"]})
        source=self.store.json_blob({"scope":scope,"items":entries,"revision":len(self.store.values("knowledge"))})
        from .governance import Lineage
        Lineage(self.store).derive(source,[e["digest"] for e in entries])
        rows=[{"id":x["id"],"terms":dict(Counter(terms(x["title"]+" "+x["body"])))} for x in items]
        index=self.store.json_blob({"source":source,"builder":digest({"bm25":1}),"rows":rows})
        return source,index
    def validate(self,snapshot,index):
        src=self.store.read_json(snapshot);idx=self.store.read_json(index)
        if src.get("schema_version")==2:
            from .memory.repository import MemoryRepository
            from .memory.retrieval import validate_bundle
            return validate_bundle(MemoryRepository(self.store),snapshot,index)
        if idx["source"]!=snapshot:raise RepoError("KNOWLEDGE_INDEX_MISMATCH")
        if any(self.store.get("revocation",r["id"]) for r in src["items"]):raise RepoError("KNOWLEDGE_REVOKED")
        import contextlib
        with contextlib.closing(self.store.connect()) as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_control'").fetchone() and c.execute("SELECT 1 FROM memory_control WHERE id='restore_fence'").fetchone():
                raise RepoError('REVOCATION_SYNC_REQUIRED')
        return src,idx
    def search(self,snapshot,index,query,top_k=5,kind=None):
        src,idx=self.validate(snapshot,index)
        if src.get('schema_version')==2:raise RepoError('ASYNC_MEMORY_SEARCH_REQUIRED','Use await MemoryService.search for the pinned v2 retrieval mode')
        rows=idx["rows"];q=terms(query);N=len(rows)
        avg=sum(sum(r["terms"].values()) for r in rows)/max(N,1);scores=[]
        entries={x["id"]:x for x in src["items"]}
        for row in rows:
            length=sum(row["terms"].values());score=0
            for t in set(q):
                df=sum(t in r["terms"] for r in rows);tf=row["terms"].get(t,0)
                if tf:score+=math.log(1+(N-df+.5)/(df+.5))*tf*2.2/(tf+1.2*(.25+.75*length/max(avg,1)))
            item=self.store.read_json(entries[row["id"]]["digest"])
            if score>0 and (not kind or item["kind"]==kind):scores.append({**item,"score":score})
        return sorted(scores,key=lambda x:(-x["score"],x["id"]))[:min(max(top_k,0),20)]
