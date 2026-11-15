"""Authorized exact/BM25/dense retrieval, RRF and bounded local reranking."""
from __future__ import annotations

from collections import Counter
import json
import math
import re
import time
from ..util import RepoError, digest, now,uid


def terms(text):
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    words = re.findall(r"[a-zA-Z0-9_./:-]+|[\u4e00-\u9fff]+", text.lower())
    result = []
    for word in words:
        result.append(word)
        if re.fullmatch(r"[\u4e00-\u9fff]+", word):
            result.extend(word[i:i+2] for i in range(len(word)-1))
        else: result.extend(x for x in re.split(r"[_/.:\-]", word) if x and x != word)
    return result


def body(item): return item.get("text", item.get("title", "")+" "+item.get("body", ""))


def encoder_batches(texts):
    """Stay below the existing worker's 64-item / 256000-character envelope."""
    batch=[];size=0
    for text in texts:
        cost=len(json.dumps('passage: '+text,ensure_ascii=False))+2
        if batch and (len(batch)>=32 or size+cost>180000):
            yield batch;batch=[];size=0
        batch.append(text);size+=cost
    if batch:yield batch


def sparse_channels(items, query, symbols=(), rows=None):
    """Shared identifier/BM25 recall; callers authorize records before invoking."""
    rows=rows if rows is not None else [{'id':k,'terms':dict(Counter(terms(body(v))))} for k,v in items.items()]
    q=set(terms(query));avg=sum(sum(r['terms'].values()) for r in rows)/max(1,len(rows))
    words=re.findall(r'[A-Za-z_][A-Za-z0-9_./:-]{3,}',query)
    anchors={w for w in words if re.search(r'[_/.:\-]|[a-z][A-Z]',w) or (w.isupper() and len(w)>3)}|set(symbols)
    exact=[];sparse=[]
    for row in rows:
        if any(a in body(items[row['id']]) for a in anchors):exact.append(row['id'])
        score=0;length=sum(row['terms'].values())
        for term in q:
            tf=row['terms'].get(term,0);df=sum(term in r['terms'] for r in rows)
            if tf:score+=math.log(1+(len(rows)-df+.5)/(df+.5))*tf*2.2/(tf+1.2*(.25+.75*length/max(avg,1)))
        if score>0:sparse.append((row['id'],score))
    return {'exact':exact[:8],'bm25':[k for k,_ in sorted(sparse,key=lambda x:(-x[1],x[0]))[:20]]}


def build_bundle(repository, scope, profile, encoder=None, visibility="DEV"):
    started=time.monotonic();encoder_before=getattr(encoder,'seconds',0)
    store = repository.store; eligible = {scope, *(["user"] if visibility == "DEV" else [])}
    items = []
    for row in repository.values("memory_versions"):
        if row["scope_id"] not in eligible or row["visibility_domain"] != "DEV": continue
        head = repository.get("memory_control", "claim:"+row["claim_id"])
        if head != {"version": row["version"], "lifecycle": "ACTIVE"}: continue
        repository.ensure_dev([row["claim_id"], *row["derived_from"]])
        items.append({**row,'lifecycle':'ACTIVE'})
    # Legacy records retain their original scope and review; never gain v2 evidence status.
    for row in store.values("knowledge"):
        if row["scope"] not in eligible or row["status"] != "ACTIVE" or store.get("revocation", row["id"]): continue
        repository.ensure_dev([row["id"]])
        from .working_state import semantic_text
        items.append({**row,'title':semantic_text(row['title']),'body':semantic_text(row['body']),
                      'sources':[semantic_text(value) for value in row['sources']],"claim_id": row["id"], "scope_id": row["scope"], "visibility_domain": "DEV",
                      "origin_grade": "legacy_unverified", "validity": "UNKNOWN"})
    if len(items) > profile.max_items: raise RepoError("MEMORY_GROWTH_BUDGET")
    items.sort(key=lambda x: x["claim_id"])
    entries = []; rows = []
    for item in items:
        key = store.json_blob(item)
        entries.append({"id": item["claim_id"], "digest": key, "version": item["version"], "scope": item["scope_id"]})
        rows.append({"id": item["claim_id"], "terms": dict(Counter(terms(body(item))))})
        with store.tx() as c: repository.derive(key, [item["claim_id"], 'memory-version:'+item['claim_id']+':'+str(item['version']),*item.get("derived_from", [])], c)
    source = store.json_blob({"schema_version": 2, "scope": scope, "visibility": visibility, "items": entries,
                              "activation_policy_digest": digest(profile), "source_codec": "memory-v2"})
    vectors = []
    if profile.mode == "hybrid_semantic_strict":
        if encoder is None: raise RepoError("SEMANTIC_MODELS_UNAVAILABLE")
        if hasattr(encoder,'healthcheck'):
            try:encoder.healthcheck()
            except RepoError as exc:
                repository.put('memory_control',uid('index-build-failed-'),{'index_build':True,'scope':scope,'mode':profile.mode,
                    'error':exc.code,'seconds':time.monotonic()-started,'created_at':now()})
                raise
        for start in range(0, len(items), 32): vectors += encoder.embed([body(x)[:7000] for x in items[start:start+32]])
    dimension=profile.encoder.get('dimension') or (len(vectors[0]) if vectors else 0)
    if any(len(vector)!=dimension for vector in vectors):raise RepoError('MEMORY_INDEX_DIMENSION')
    index = {"schema_version": 2, "source": source, "mode": profile.mode, "health": "READY",
             "builder": digest({"tokenizer": "code-cjk-bigram-v2", "bm25": [1.2, 0.75], "rrf": 60,"exact":"code-identifiers-v2"}),
             "encoder": encoder.identity if encoder else None, "rows": rows, "vectors": vectors,
             "membership": {e["id"]: e["digest"] for e in entries},'dimension':dimension}
    key = store.json_blob(index)
    with store.tx() as c:
        repository.derive(source, [e["digest"] for e in entries], c)
        repository.derive(key, [source], c)
    validate_bundle(repository, source, key)
    repository.put('memory_control',uid('index-build-'),{'index_build':True,'scope':scope,'mode':profile.mode,'snapshot':source,'index':key,
        'seconds':time.monotonic()-started,'encoder_seconds':max(0,getattr(encoder,'seconds',0)-encoder_before),'created_at':now()})
    return source, key


def validate_bundle(repository, snapshot, index):
    source = repository.store.read_json(snapshot); bundle = repository.store.read_json(index)
    if bundle.get("source") != snapshot: raise RepoError("KNOWLEDGE_INDEX_MISMATCH")
    entries = {e["id"]: e["digest"] for e in source["items"]}
    if bundle.get("health") != "READY" or entries != bundle.get("membership"):
        raise RepoError("MEMORY_INDEX_MEMBERSHIP")
    if {r["id"] for r in bundle["rows"]} != set(entries) or len(bundle["rows"]) != len(entries):
        raise RepoError("MEMORY_INDEX_MEMBERSHIP")
    if bundle["mode"] == "hybrid_semantic_strict":
        vectors = bundle["vectors"]
        if len(vectors) != len(entries) or not bundle.get("encoder"): raise RepoError("MEMORY_INDEX_VECTORS")
        for vector in vectors:
            if bundle.get('dimension') is not None and len(vector)!=bundle['dimension']:raise RepoError('MEMORY_INDEX_DIMENSION')
            if not vector or any(not math.isfinite(x) for x in vector) or abs(sum(x*x for x in vector)-1) > .02:
                raise RepoError("MEMORY_INDEX_VECTORS")
    repository.check([snapshot, index, *entries, *entries.values()])
    return source, bundle


def applicability(item, intent, file_versions):
    if item.get("valid_until") is not None and item["valid_until"] < now(): return "FALSE", "EXPIRED"
    if item.get('validity') in {'STALE','CONTRADICTED'}:return 'FALSE',item['validity']
    unknown=None
    for name, value in item.get("preconditions", {}).items():
        actual = intent.environment.get(name)
        if actual is None:unknown='DEPENDENCY_UNOBSERVED';continue
        if actual != value: return "FALSE", "DEPENDENCY_MISMATCH"
    for path, expected in item.get("file_dependencies", {}).items():
        if path not in file_versions:unknown=unknown or 'FILE_UNOBSERVED';continue
        if file_versions[path] != expected: return "FALSE", "HISTORICAL_FILE_VERSION"
    if unknown:return 'UNKNOWN',unknown
    if item.get('validity')=='HISTORICAL':return 'UNKNOWN','HISTORICAL_OBSERVATION'
    if item.get("origin_grade") == "legacy_unverified": return "UNKNOWN", "LEGACY_SOURCE_UNVERIFIED"
    if item.get("epistemic_status") in {"INFERRED", "DISPUTED"}: return "UNKNOWN", "INFERENCE_REQUIRES_EVIDENCE"
    return "TRUE", "APPLICABLE"


class DynamicRetriever:
    def __init__(self, repository, profile, encoder=None):
        self.repo, self.profile, self.encoder = repository, profile, encoder

    def rank_experiences(self, records, query, *, symbols=(), limit=6, token_budget=None, semantic=False):
        """Rank pre-authorized structured records; no Trace text is indexed here."""
        from ..context import estimate_tokens
        if isinstance(limit,bool) or not isinstance(limit,int) or not 0<=limit<=20:raise RepoError('INVALID_MEMORY_POLICY')
        if not isinstance(query,str) or len(query)>8000:raise RepoError('INVALID_MEMORY_QUERY')
        if token_budget is not None and (isinstance(token_budget,bool) or not isinstance(token_budget,int) or token_budget<0):raise RepoError('INVALID_MEMORY_POLICY')
        budget=self.profile.evidence_tokens if token_budget is None else min(token_budget,self.profile.evidence_tokens)
        if not query.strip() or not limit or not budget:
            return [],{'channels':{},'candidate_count':0,'eligible_count':len(records),'dense_calls':0,
                'dense_seconds':0.,'reranker_calls':0,'reranker_items':0,'reranker_seconds':0.,
                'index_build_seconds':0.,'seconds':0.,'selected_tokens':0,'injected_tokens':0,'automatic_merge':False}
        started=time.monotonic();build=time.monotonic()
        items={r['id']:r for r in records}
        if len(items)>self.profile.max_items:raise RepoError('MEMORY_GROWTH_BUDGET')
        rows=[{'id':k,'terms':dict(Counter(terms(body(v))))} for k,v in items.items()]
        index_seconds=time.monotonic()-build
        channels=sparse_channels(items,query,symbols,rows);scores={};dense={};rerank={}
        qt=set(terms(query));top=channels['bm25'][:1]
        reliable=bool(channels['exact']) or bool(top and len(qt)>=2 and len(qt&set(terms(body(items[top[0]]))))/len(qt)>=.6)
        dense_calls=0;reranker_calls=0;dense_seconds=0.;reranker_seconds=0.
        if items and limit and budget and (semantic or not reliable) and self.profile.mode=='hybrid_semantic_strict':
            if self.encoder is None:raise RepoError('SEMANTIC_MODELS_UNAVAILABLE')
            before=time.monotonic();ids=list(items)
            vectors=[]
            for batch in encoder_batches([body(items[k])[:7000] for k in ids]):
                vectors+=self.encoder.embed(batch);dense_calls+=1
            q=self.encoder.embed([query],query=True)[0];dense_calls+=1
            if not q or any(not math.isfinite(x) for x in q) or len(vectors)!=len(ids) or any(len(v)!=len(q) or not v or any(not math.isfinite(x) for x in v) for v in vectors):raise RepoError('MEMORY_INDEX_DIMENSION')
            dense={k:sum(a*b for a,b in zip(q,v)) for k,v in zip(ids,vectors)}
            channels['dense']=[k for k in sorted(dense,key=lambda k:(-dense[k],k))[:20] if dense[k]>=self.profile.dense_min_score]
            dense_seconds=time.monotonic()-before
        for ids in channels.values():
            for rank,k in enumerate(ids,1):scores[k]=scores.get(k,0)+1/(60+rank)
        candidates=sorted(scores,key=lambda k:(-scores[k],k))[:20]
        if self.encoder and self.profile.mode=='hybrid_semantic_strict' and candidates and limit and budget:
            before=time.monotonic();values=[]
            for batch in encoder_batches([body(items[k])[:7000] for k in candidates]):
                values+=self.encoder.rerank(query,batch);reranker_calls+=1
            if len(values)!=len(candidates) or any(not math.isfinite(v) for v in values):raise RepoError('MEMORY_RERANK_INVALID')
            rerank=dict(zip(candidates,values));reranker_seconds=time.monotonic()-before
            candidates.sort(key=lambda k:(-rerank[k],-scores[k],k))
        hits=[];used=0
        for k in candidates if limit and budget else []:
            if k in rerank and rerank[k]<self.profile.rerank_min_score:continue
            hit={**items[k],'rrf_score':scores[k],'rerank_score':rerank.get(k),'authority':'ADVISORY_ONLY'}
            cost=estimate_tokens(hit)
            if used+cost>budget:continue
            hits.append(hit);used+=cost
            if len(hits)>=limit:break
        return hits,{'channels':channels,'candidate_count':len(candidates),'eligible_count':len(items),
            'dense_calls':dense_calls,'dense_seconds':dense_seconds,'reranker_calls':reranker_calls,
            'reranker_items':len(candidates) if reranker_calls else 0,'reranker_seconds':reranker_seconds,
            'index_build_seconds':index_seconds,'seconds':time.monotonic()-started,
            'selected_tokens':used,'injected_tokens':0,'automatic_merge':False}

    def retrieve(self, intent, file_versions, limit=6, token_budget=None):
        from ..context import estimate_tokens
        started = time.monotonic()
        if isinstance(limit,bool) or not isinstance(limit,int) or limit<0:raise RepoError('INVALID_MEMORY_POLICY')
        if token_budget is not None and (isinstance(token_budget,bool) or not isinstance(token_budget,int) or token_budget<0):raise RepoError('INVALID_MEMORY_POLICY')
        evidence_budget=self.profile.evidence_tokens if token_budget is None else min(token_budget,self.profile.evidence_tokens)
        source, index = validate_bundle(self.repo, intent.knowledge_snapshot_digest, intent.index_bundle_digest)
        if source["scope"] not in intent.authorized_scopes: raise RepoError("MEMORY_SCOPE_DENIED")
        if index["mode"] != self.profile.mode: raise RepoError("MEMORY_MODE_CHANGED")
        if self.profile.mode == "hybrid_semantic_strict" and (self.encoder is None or index["encoder"] != self.encoder.identity):
            raise RepoError("SEMANTIC_MODELS_UNAVAILABLE")
        cache_key = digest([intent.model_dump(), file_versions, self.repo.epoch(), digest(self.profile), index.get("encoder"),limit,token_budget])
        cached = self.repo.get("memory_cache", cache_key)
        if cached:
            self.repo.check([cached["trace_id"], *[h["version_digest"] for h in cached["hits"]]])
            return cached["hits"][:limit], cached["trace_id"]
        entries = {e["id"]: e for e in source["items"] if e["scope"] in intent.authorized_scopes}
        # Do not expose excluded objects (or even their counts) to any encoder.
        items = {key: self.repo.store.read_json(e["digest"]) for key, e in entries.items()}
        items = {key: item for key, item in items.items() if item.get("visibility_domain", "DEV") == "DEV"}
        rows = [r for r in index["rows"] if r["id"] in items]
        query = intent.semantic_query; scores = {}
        channels=sparse_channels(items,query,intent.active_symbols,rows)
        exact=channels['exact']
        dense_scores = {}
        if self.encoder and items:
            vector = self.encoder.embed([query], query=True)[0]
            for row, stored in zip(index["rows"], index["vectors"]):
                if row["id"] in items:
                    if len(vector) != len(stored): raise RepoError("ENCODER_DIMENSION_MISMATCH")
                    dense_scores[row["id"]] = sum(a*b for a,b in zip(vector, stored))
            channels["dense"] = [key for key, score in sorted(dense_scores.items(), key=lambda x: (-x[1], x[0]))[:20]
                                 if score >= self.profile.dense_min_score]
        for ids in channels.values():
            for rank, key in enumerate(ids, 1): scores[key] = scores.get(key, 0) + 1/(60+rank)
        candidates = sorted(scores, key=lambda key: (-scores[key], key))[:24]
        rerank = {}
        if self.encoder and candidates:
            rerank = dict(zip(candidates, self.encoder.rerank(query, [body(items[k])[:7000] for k in candidates])))
            candidates.sort(key=lambda key: (-rerank[key], -scores[key], key))
        hits = []; filters = []; used = 0
        for key in (candidates if limit else []):
            status, reason = applicability(items[key], intent, file_versions)
            if status == "FALSE": filters.append({"id": key, "reason": reason}); continue
            if self.encoder and rerank[key] < self.profile.rerank_min_score and key not in exact:
                filters.append({"id": key, "reason": "NO_RELIABLE_MATCH"}); continue
            if not self.profile.calibration_digest and key not in exact:
                status, reason = "UNKNOWN", "UNCALIBRATED_RELEVANCE"
            text = body(items[key])
            hit = {"id": key, "version": entries[key].get("version", 1), "version_digest": entries[key]["digest"],
                   "text": text, "kind": items[key].get("kind", "FACT"), "applicability": status, "reason": reason,
                   'validity':items[key].get('validity','UNKNOWN'),
                   "rrf_score": scores[key], "rerank_score": rerank.get(key), "sources": items[key].get("support_refs", items[key].get("sources", [])),
                   "authority": "UNTRUSTED_DATA", "grants_edit_authority": False}
            cost=estimate_tokens(hit)
            if used+cost>evidence_budget:
                filters.append({'id':key,'reason':'TOKEN_BUDGET'});continue
            hits.append(hit); used += cost
            if len(hits) >= min(limit, 10): break
        trace = {"run_id": intent.run_id, "intent": intent.model_dump(), "channels": channels, "filters": filters,
                 "selected": [h["id"] for h in hits], "mode": self.profile.mode, "encoder": index.get("encoder"),
                 "seconds": time.monotonic()-started, "revocation_epoch": self.repo.epoch()}
        trace_id = digest(trace)
        with self.repo.store.tx() as c:
            self.repo.check([intent.knowledge_snapshot_digest, *[h["version_digest"] for h in hits]], c)
            self.repo.put("retrieval_traces", trace_id, trace, c)
            self.repo.derive(trace_id, [intent.knowledge_snapshot_digest, *[h["version_digest"] for h in hits]], c)
            self.repo.put("memory_cache", cache_key, {"trace_id": trace_id, "hits": hits}, c)
            self.repo.derive(cache_key, [trace_id], c)
        return hits, trace_id
