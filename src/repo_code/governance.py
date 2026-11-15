"""Trusted complexity bounds, lineage, one-shot hidden cohorts and whole-release CAS."""
from __future__ import annotations
import ast
import contextlib
import json
from pathlib import Path
from .models import Release, Candidate
from .util import RepoError, digest, canonical, uid, now, release_content_identity
from .context import estimate_tokens

COMPONENTS={"memory_policy","delegation_policy","context_policy","retry_policy","planning_policy","tools_schema","skills_index","l1_optimizer","l2_optimizer"}
DEFAULTS={"memory_policy":{"kind":"config","top_k":5},
"delegation_policy":{"kind":"config","max_turns":3,"instructions":"Read-only exploration with evidence"},
"context_policy":{"kind":"config","max_tokens":24000,"reserve_tokens":4096},
"retry_policy":{"kind":"config","max_identical_failures":3,"require_new_evidence":False},
"planning_policy":{"kind":"config","verify_before_finish":True},
"tools_schema":{"kind":"config","description_suffix":""},
"skills_index":{"kind":"config","skills":[]},
"l1_optimizer":{"kind":"config","max_candidates":3,"max_rounds":2,"order":"evidence_first"},
"l2_optimizer":{"kind":"config","max_candidates":2,"meta_self_modify_enabled":False}}
DEFAULT_GATE={"version":2,"g4_protocol":"candidate-bound-regression-v1","max_active_modules":16,"max_dependency_edges":64,"max_dependency_cycles":0,"max_control_flow":200,"max_prompt_tokens":12000,"max_schema_tokens":12000,"max_knowledge_tokens":16000,"max_roles":4,"max_candidates":5,"max_regressions":0,"require_net_gain":True,"max_cost_ratio":2.0,"max_archive_bytes":256*1024*1024}

class ComplexityGovernor:
    def __init__(self,policy=None):self.policy=policy or DEFAULT_GATE
    @property
    def digest(self):return digest(self.policy)
    def measure(self,components):
        v={"active_modules":0,"dependency_edges":0,"dependency_cycles":0,"control_flow":0,"prompt_tokens":0,"schema_tokens":0,"knowledge_tokens":0,"roles":0,"bytes":0}
        graph={}
        for name,obj in components.items():
            raw=canonical(obj);v["bytes"]+=len(raw)
            if obj.get("kind")=="python":
                source=obj["source"]
                if len(source)>100000:raise RepoError("CODE_CANDIDATE_TOO_LARGE")
                tree=ast.parse(source);v["active_modules"]+=1;imports=set()
                for n in ast.walk(tree):
                    if isinstance(n,(ast.Import,ast.ImportFrom)):
                        imports.update(x.name.split('.')[0] for x in n.names) if isinstance(n,ast.Import) else imports.add((n.module or "").split('.')[0])
                    if isinstance(n,(ast.If,ast.For,ast.While,ast.Try,ast.BoolOp,ast.comprehension,ast.IfExp)):v["control_flow"]+=1
                graph[name]=imports;v["dependency_edges"]+=len(imports)
                if not any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=="handle" for n in tree.body):raise RepoError("WORKER_ABI_MISSING")
            elif obj.get("kind")!="config":raise RepoError("UNKNOWN_COMPONENT_KIND")
            if name=="skills_index":v["knowledge_tokens"]+=estimate_tokens(obj)
            elif name=="tools_schema":v["schema_tokens"]+=estimate_tokens(obj)
            else:v["prompt_tokens"]+=estimate_tokens({k:x for k,x in obj.items() if k!="source"})
            v["roles"]+=len(obj.get("roles",[]))
        def cycle(n,path):
            if n in path:return 1
            return sum(cycle(x,path|{n}) for x in graph.get(n,[]) if x in graph)
        v["dependency_cycles"]=sum(cycle(n,set()) for n in graph)
        return v
    def audit(self,baseline,candidate):
        """Whole-release structural delta; suggestions never authorize deletion."""
        before=self.measure(baseline);after=self.measure(candidate);fingerprints={};duplicates=[]
        for name,obj in candidate.items():
            if obj.get("kind")=="python":
                structural=ast.dump(ast.parse(obj["source"]),include_attributes=False)
                key=digest(structural)
                if key in fingerprints:duplicates.append([fingerprints[key],name])
                else:fingerprints[key]=name
        return {"baseline":before,"candidate":after,"delta":{k:after[k]-before[k] for k in before},
            "duplicate_implementations":duplicates,"suggestions_only":True,
            "rule":"Prefer replacing existing behavior; semantic compatibility requires trials"}

    def check(self,components,operation="REPLACE",justification=""):
        v=self.measure(components)
        for k in ("active_modules","dependency_edges","dependency_cycles","control_flow","prompt_tokens","schema_tokens","knowledge_tokens","roles"):
            if v[k]>self.policy["max_"+k]:raise RepoError("COMPLEXITY_LIMIT",k)
        if operation=="ADD" and len(justification.strip())<20:raise RepoError("GROWTH_JUSTIFICATION_REQUIRED")
        return v

class Lineage:
    def __init__(self,store):self.store=store
    def derive(self,child,parents):
        with self.store.tx() as c:
            for p in parents:
                if p==child or child in self.ancestors(p,c):raise RepoError("LINEAGE_CYCLE")
                c.execute("INSERT OR IGNORE INTO lineage VALUES(?,?)",(child,p))
    def ancestors(self,item,c=None):
        own=c is None;c=c or self.store.connect();seen=set();todo=[item]
        try:
            while todo:
                x=todo.pop()
                for row in c.execute("SELECT parent FROM lineage WHERE child=?",(x,)):
                    if row[0] not in seen:seen.add(row[0]);todo.append(row[0])
            return seen
        finally:
            if own:c.close()
    def expose(self,artifact,cohort,family,kind="gate_verdict",c=None):
        if c is not None:
            c.execute("INSERT OR IGNORE INTO exposures VALUES(?,?,?,?)",(artifact,cohort,family,kind))
        else:
            with self.store.tx() as db:
                self.expose(artifact,cohort,family,kind,db)
    def is_exposed(self,artifact,cohort,family,c=None):
        own=c is None;c=c or self.store.connect()
        try:
            for a in {artifact,*self.ancestors(artifact,c)}:
                if c.execute("SELECT 1 FROM exposures WHERE artifact=? AND (cohort=? OR family=?)",(a,cohort,family)).fetchone():return True
            return False
        finally:
            if own:c.close()

class HoldoutVault:
    def __init__(self,store):self.store=store;self._capability=object()
    def evaluator_capability(self):return self._capability  # Trusted construction only, never serialized.
    def register(self,name,role,tasks,family=None,synthetic=False,*,applicability_family=None,contamination_groups=None):
        if role not in {"dev","promotion","final","meta_dev","meta_promotion","meta_final"} or not tasks or len(tasks)>64:raise RepoError("INVALID_COHORT")
        obj={"name":name,"role":role,"tasks":tasks,"family":family or name,"synthetic_integration":synthetic}
        if applicability_family is not None or contamination_groups is not None:
            if (not isinstance(applicability_family,str) or not applicability_family
                    or not isinstance(contamination_groups,list) or not contamination_groups
                    or any(not isinstance(g,str) or not g.strip() for g in contamination_groups)):
                raise RepoError('COHORT_PROVENANCE_REQUIRED')
            # Trusted evaluator declaration of source/near-duplicate groups, not
            # a task/model-provided instruction. Shared mechanisms alone are not leakage.
            obj.update(provenance_version=2,applicability_family=applicability_family,
                       contamination_groups=sorted(set(contamination_groups)))
        fingerprints=[digest({k:v for k,v in t.items() if k not in {'id','name'}}) for t in tasks]
        if obj.get('provenance_version')==2:
            fingerprints+=['implementation:'+digest(t['files']) for t in tasks if t.get('files')]
        key=self.store.json_blob(obj)
        meta={k:v for k,v in obj.items() if k not in {'tasks','name'}}
        self.store.put('cohort',key,{**meta,'digest':key,'task_fingerprints':fingerprints},immutable=True)
        return key

    @staticmethod
    def _contamination_keys(meta):
        # Legacy rows retain conservative family isolation. No silent migration.
        if meta.get('provenance_version')==2:
            return ['contamination:'+g for g in meta['contamination_groups']]
        return [meta['family']]
    def open(self,key,capability=None):
        meta=self.store.get("cohort",key)
        if not meta:raise RepoError("COHORT_NOT_FOUND")
        if meta["role"] not in {"dev","meta_dev"} and capability is not self._capability:raise RepoError("HIDDEN_DATA_DENIED")
        return self.store.read_json(key)
    def _subjects(self, candidate_id, c):
        candidate = self.store.get("candidate", candidate_id, c)
        if candidate is None: raise RepoError("CANDIDATE_NOT_FOUND")
        release_id = candidate["candidate_release_digest"]
        release = self.store.get("release", release_id, c)
        if release is None: raise RepoError("RELEASE_NOT_FOUND")
        if Release.model_validate(release).digest != release_id: raise RepoError("RELEASE_CORRUPT")
        identity = release_content_identity(release)
        if candidate.get("content_identity") not in (None, identity):
            raise RepoError("CANDIDATE_IDENTITY_MISMATCH")
        return identity, {candidate_id, release_id, identity}

    def claim(self,candidate,key,gate):
        # Checking exposure and reserving one-time use is ONE transaction. Two
        # evaluators cannot both pass a check-then-insert race using fresh IDs.
        with self.store.tx() as c:
            meta=self.store.get("cohort",key,c)
            if not meta:raise RepoError("COHORT_NOT_FOUND")
            identity, subjects = self._subjects(candidate, c)
            lineage = Lineage(self.store)
            exposure_keys=self._contamination_keys(meta)
            legacy_keys=[meta['applicability_family']] if meta.get('provenance_version')==2 else []
            if any(lineage.is_exposed(x,key,g,c) for x in subjects for g in exposure_keys+legacy_keys):
                raise RepoError("HOLDOUT_CONTAMINATED")
            if not meta.get("synthetic_integration") and any(
                    lineage.is_exposed(x,key,"task:"+f,c)
                    for x in subjects for f in meta.get("task_fingerprints",[])):
                raise RepoError("HOLDOUT_TASK_REUSED")
            scopes = ["cohort:"+key,*['family:'+g for g in self._contamination_keys(meta)]]
            if not meta.get("synthetic_integration"):
                scopes.extend("task:"+f for f in meta.get("task_fingerprints",[]))
            related = set(subjects)
            for subject in subjects: related.update(lineage.ancestors(subject,c))
            for subject in related:
                for scope in scopes+['family:'+g for g in legacy_keys]:
                    if c.execute("SELECT 1 FROM cohort_reservations WHERE subject=? AND scope=?",
                                 (subject,scope)).fetchone():
                        raise RepoError("HOLDOUT_ALREADY_USED")
            for scope in set(scopes):
                c.execute("INSERT INTO cohort_reservations VALUES(?,?,?,?,?)",
                          (identity,scope,candidate,key,gate))
            c.execute("INSERT INTO cohort_claims VALUES(?,?,?)",(candidate,key,gate))

    def mark_exposure(self,candidate,key,kind="development_feedback"):
        with self.store.tx() as c:
            meta=self.store.get("cohort",key,c)
            if not meta:raise RepoError("COHORT_NOT_FOUND")
            _, subjects = self._subjects(candidate,c)
            lineage=Lineage(self.store)
            families = self._contamination_keys(meta)
            if not meta.get("synthetic_integration"):
                families.extend("task:"+f for f in meta.get("task_fingerprints",[]))
            for subject in subjects:
                for family in families:lineage.expose(subject,key,family,kind,c)
    def notice(self,candidate,key,passed,reason):
        meta=self.store.get("cohort",key);notice={"candidate":candidate,"status":"PASS" if passed else "REJECT","reason":reason}
        nd=self.store.json_blob(notice);self.mark_exposure(candidate,key,"gate_verdict")
        Lineage(self.store).derive(nd,[candidate]);return notice

class Releases:
    def __init__(self,store,sandbox):self.store,self.sandbox=store,sandbox;self.governor=ComplexityGovernor()
    def bootstrap(self):
        with contextlib.closing(self.store.connect()) as c:
            row=c.execute("SELECT * FROM active_release WHERE scope='default'").fetchone()
            if row:return row["digest"]
        components={k:self.store.json_blob(v) for k,v in DEFAULTS.items()}
        att=self.store.json_blob({"type":"operator_baseline","version":"1.5.1","components":components})
        release=Release(components=components,base_digest=None,worker_build_digest=self.sandbox.environment_digest,dependencies_digest=digest({"python":1,"protocol":1}),gate_policy_digest=self.governor.digest,build_attestation_digest=att)
        self.store.put("release",release.digest,release,immutable=True)
        with self.store.tx() as c:
            c.execute("INSERT OR IGNORE INTO active_release VALUES('default',0,?)",(release.digest,))
        return self.active()["digest"]
    def active(self):
        with contextlib.closing(self.store.connect()) as c:
            row=c.execute("SELECT * FROM active_release WHERE scope='default'").fetchone()
            if not row:raise RepoError("NO_ACTIVE_RELEASE")
            return dict(row)
    def get(self,key):
        obj=self.store.get("release",key)
        if not obj:raise RepoError("RELEASE_NOT_FOUND")
        release=Release.model_validate(obj)
        if release.digest!=key:raise RepoError("RELEASE_CORRUPT")
        if release.gate_policy_digest!=self.governor.digest:raise RepoError("GATE_POLICY_CHANGED")
        if release.worker_build_digest!=self.sandbox.environment_digest:raise RepoError("WORKER_ENVIRONMENT_CHANGED")
        for d in release.components.values():self.store.read_blob(d)
        return release
    def components(self,key):return {k:self.store.read_json(v) for k,v in self.get(key).components.items()}
    def candidate(self,pattern_id,changes,hypothesis,sources,operation="REPLACE",parents=None,level=1,justification="",base=None):
        size=sum(p.stat().st_size for p in self.store.artifact_root.iterdir() if p.is_file())
        if size>self.governor.policy["max_archive_bytes"]:raise RepoError("ARCHIVE_BUDGET_EXHAUSTED","protected evidence is not automatically deleted")
        base=base or self.active()["digest"];old=self.get(base)
        if not changes or not set(changes)<=COMPONENTS:raise RepoError("EVOLUTION_SCOPE_DENIED")
        if level==1 and set(changes)&{"l1_optimizer","l2_optimizer"}:raise RepoError("META_REQUIRED")
        if level==2 and not set(changes)<={"l1_optimizer","l2_optimizer"}:raise RepoError("META_SCOPE_DENIED")
        comps=self.components(base)
        # Removing an extension restores a known Host-owned baseline slot; it
        # never removes authorization, grading or mandatory lifecycle behavior.
        if operation=="REMOVE":changes={k:DEFAULTS[k] for k in changes}
        comps.update(changes)
        vector=self.governor.check(comps,operation,justification)
        refs={k:self.store.json_blob(v) for k,v in comps.items()}
        att=self.store.json_blob({"type":"candidate_build","base":base,"components":refs,"scope":sorted(changes),"protocol":1})
        new=Release(components=refs,base_digest=base,worker_build_digest=old.worker_build_digest,dependencies_digest=old.dependencies_digest,gate_policy_digest=old.gate_policy_digest,build_attestation_digest=att,generation=old.generation+1)
        self.store.put("release",new.digest,new,immutable=True)
        candidate=Candidate(id=uid("cand-"),level=level,pattern_id=pattern_id,base_release_digest=base,candidate_release_digest=new.digest,content_identity=release_content_identity(new),targets=sorted(changes),operation=operation,hypothesis=hypothesis,source_evidence=sources,growth_justification=justification,parents=parents or [])
        self.store.put("candidate",candidate.id,candidate,immutable=True)
        self.store.put("candidate_state",candidate.id,{"state":"PROPOSED","complexity":vector,"structural_audit":self.governor.audit(self.components(base),comps)})
        Lineage(self.store).derive(candidate.id,[base,candidate.content_identity,*candidate.parents,*sources]);Lineage(self.store).derive(new.digest,[candidate.id])
        return candidate
    def record_gate(self,candidate,gate,report):
        if gate not in {"G0","G1","G2","G3","G4","G4b","META"}:raise RepoError("UNKNOWN_GATE")
        if set(report)&{"candidate","release","base","gate","policy"}:raise RepoError("GATE_IDENTITY_OVERRIDE")
        payload={"candidate":candidate.id,"release":candidate.candidate_release_digest,"base":candidate.base_release_digest,"gate":gate,"policy":self.governor.digest,**report}
        # Called only from trusted evaluators, never exposed in worker IPC.
        signature=self.store.sign(payload)
        self.store.put("gate",digest([candidate.id,gate]),{"payload":payload,"signature":signature},immutable=True)
    def approve(self,candidate_id,reviewer):
        if not reviewer:raise RepoError("REVIEWER_REQUIRED")
        c=Candidate.model_validate(self.store.get("candidate",candidate_id));self.store.put("release_approval",candidate_id,{"release":c.candidate_release_digest,"reviewer":reviewer,"at":now()})
    def activate(self,candidate_id,expected_revision=None):
        candidate=Candidate.model_validate(self.store.get("candidate",candidate_id))
        if not self.sandbox.isolated:raise RepoError("ISOLATION_REQUIRED")
        if candidate.level==2 and not self.store.get("operator_config","meta",default={}).get("enabled"):raise RepoError("META_SELF_MODIFY_DISABLED")
        self.get(candidate.candidate_release_digest);self.governor.check(self.components(candidate.candidate_release_digest),candidate.operation,candidate.growth_justification)
        skills=self.components(candidate.candidate_release_digest)['skills_index'].get('skills',[])
        if any(isinstance(e,dict) and e.get('experience_id') for e in skills):
            qualification=self.store.get('skill_qualification',candidate.id)
            if not qualification or not self.store.verify(qualification['payload'],qualification['signature']):raise RepoError('SKILL_QUALIFICATION_REQUIRED')
            payload=qualification['payload']
            if payload['release']!=candidate.candidate_release_digest or payload['candidate']!=candidate.id or payload['evidence_class']!='MODEL_TASK_EVALUATION':raise RepoError('SKILL_QUALIFICATION_BINDING')
            for stage in ['s1','s2','s3']:
                report=self.store.get('skill_'+stage,digest([payload['contract_ref'],candidate.id]))
                if not report or not report['passed'] or digest(report)!=payload[stage]:raise RepoError('SKILL_QUALIFICATION_STALE')
                for lesson_id,review_digest in report.get('semantic_review_digests',{}).items():
                    review=self.store.get('claim_semantic_review',lesson_id)
                    if not review or digest(review)!=review_digest:raise RepoError('SKILL_SEMANTIC_REVIEW_STALE')
        required=["G0","G1","G2","G3","G4","G4b"]+(["META"] if candidate.level==2 else [])
        reports = {}
        for g in required:
            row=self.store.get("gate",digest([candidate.id,g]))
            if not row or not self.store.verify(row["payload"],row["signature"]):raise RepoError("MISSING_TRUSTED_GATE",g)
            reports[g] = row["payload"]
        for g, p in reports.items():
            if not p.get("passed") or p["release"]!=candidate.candidate_release_digest or p["policy"]!=self.governor.digest:raise RepoError("GATE_FAILED",g)
            if p.get("evidence_class")=="INTEGRATION_ONLY" and not self.store.get("operator_config","integration",default={}).get("enabled"):raise RepoError("INTEGRATION_ONLY_EVIDENCE")
        from .regression import validate_g4_report
        candidate_release=self.get(candidate.candidate_release_digest)
        if not validate_g4_report(reports["G4"],candidate.candidate_release_digest,
                                  candidate_release.components,self.sandbox.environment_digest):
            raise RepoError("G4_INCOMPLETE_OR_WRONG_RELEASE")
        approval=self.store.get("release_approval",candidate_id)
        if not approval or approval["release"]!=candidate.candidate_release_digest:raise RepoError("RELEASE_APPROVAL_REQUIRED")
        with self.store.tx() as c:
            active=c.execute("SELECT * FROM active_release WHERE scope='default'").fetchone()
            if active["digest"]!=candidate.base_release_digest or expected_revision is not None and active["revision"]!=expected_revision:raise RepoError("RELEASE_CAS_CONFLICT")
            c.execute("UPDATE active_release SET revision=revision+1,digest=? WHERE scope='default' AND revision=?",(candidate.candidate_release_digest,active["revision"]))
            self.store.put("activated",candidate.candidate_release_digest,{"candidate":candidate.id,"revision":active["revision"]+1},c,immutable=True)
            self.store.put("candidate_state",candidate.id,{"state":"ACTIVE"},c)
            self.store.emit(c,None,"release.activated",{"release":candidate.candidate_release_digest,"candidate":candidate.id})
        return self.active()
    def retire(self,candidate_id,reviewer):
        """Remove from active search, retaining pinned releases and audit evidence."""
        if not reviewer:raise RepoError("REVIEWER_REQUIRED")
        candidate=Candidate.model_validate(self.store.get("candidate",candidate_id))
        if self.active()["digest"]==candidate.candidate_release_digest:raise RepoError("ACTIVE_RELEASE_PROTECTED")
        with contextlib.closing(self.store.connect()) as conn:
            for row in conn.execute("SELECT state,data FROM runs"):
                if row[0] not in {"SUCCEEDED","FAILED","CANCELLED","PARTIAL","ANSWERED","SUPERSEDED"} and json.loads(row[1])["manifest"]["release_digest"]==candidate.candidate_release_digest:
                    raise RepoError("PINNED_RELEASE_PROTECTED")
        self.store.put("candidate_state",candidate_id,{"state":"ARCHIVED","reviewer":reviewer,"bytes_preserved":True})
        return {"archived":candidate_id,"physical_deletion":False}

    def rollback(self,target,expected_digest,reviewer):
        if not reviewer:raise RepoError("REVIEW_REQUIRED")
        self.get(target)
        target_release=self.get(target)
        if target_release.base_digest is not None and not self.store.get("activated",target):raise RepoError("UNVERIFIED_ROLLBACK_TARGET")
        with self.store.tx() as c:
            row=c.execute("SELECT * FROM active_release WHERE scope='default'").fetchone()
            if row["digest"]!=expected_digest:raise RepoError("RELEASE_CAS_CONFLICT")
            c.execute("UPDATE active_release SET digest=?,revision=revision+1 WHERE scope='default'",(target,))
            self.store.emit(c,None,"release.rolled_back",{"target":target,"reviewer":reviewer})
        return self.active()
