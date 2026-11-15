"""Host-owned contracts and external verdicts; never imports DUT code."""
from __future__ import annotations
import ast
import json
import tempfile
from pathlib import Path
from .models import Contract, Evidence, Authority, Subject, PreparedDecision, VerificationIntent
from .util import RepoError, digest, canonical, uid, atomic_write
from .workspace import Workspace, relative_path

GRADER_VERSION=digest({"grader":"repo-code-external","version":2})


def validate_contract(contract: Contract, require_frozen=False):
    if require_frozen and (contract.status!="FROZEN" or not contract.reviewer):raise RepoError("CONTRACT_NOT_FROZEN")
    required=set(contract.required_checks);globals_=set(contract.global_required_checks)
    checks={c.id:c for c in contract.checks};reqs={r.id:r for r in contract.requirements}
    if not required or not reqs:raise RepoError("EMPTY_ACCEPTANCE")
    if len(required)!=len(contract.required_checks) or len(checks)!=len(contract.checks) or len(reqs)!=len(contract.requirements):raise RepoError("DUPLICATE_ID")
    if not globals_<=required or not required<=checks.keys():raise RepoError("UNREGISTERED_CHECK")
    if set(contract.coverage)!=reqs.keys():raise RepoError("INVALID_REQUIREMENT_COVERAGE")
    mapped=set()
    for req in contract.requirements:
        refs=contract.coverage.get(req.id,[])
        if not refs or len(set(refs))!=len(refs) or not set(refs)<=required:raise RepoError("INSUFFICIENT_COVERAGE")
        mapped.update(refs)
        kinds={checks[c].subject for c in refs}
        if not set(req.subjects)<=kinds:raise RepoError("SUBJECT_COVERAGE_MISSING")
        if req.critical and not any(checks[c].kind not in {"diagnostic","build"} for c in refs):raise RepoError("WEAK_ORACLE")
    if required!=mapped|globals_:raise RepoError("UNMAPPED_REQUIRED_CHECK")
    for c in required:
        case_list=checks[c].definition.get("cases")
        if case_list is not None:
            if not isinstance(case_list,list) or not 1<=len(case_list)<=100:raise RepoError("INVALID_CHECK_CASES")
            identifiers=[str(x.get("id",i)) for i,x in enumerate(case_list)]
            if len(identifiers)!=len(set(identifiers)):raise RepoError("DUPLICATE_TEST_ID")
        if not checks[c].active or not checks[c].protected:raise RepoError("UNPROTECTED_CHECK")
        allowed = set(checks[c].permitted_authorities)
        possible = {"human": {Authority.HUMAN}, "build": {Authority.BUILD},
                    "diagnostic": {Authority.IN_PROCESS}}.get(checks[c].kind, {Authority.EXTERNAL})
        # In-process diagnostics remain useful during development, but cannot
        # certify a mandatory delivery check, including an unmapped global one.
        if allowed & {Authority.IN_PROCESS, Authority.SELF} or not allowed & possible:
            raise RepoError("WEAK_REQUIRED_AUTHORITY", c)
        if checks[c].allowed_skips:raise RepoError("REQUIRED_SKIP_UNSUPPORTED","register each optional check separately")
    if contract.deliverable=="artifact" and not {Subject.BUILD,Subject.ARTIFACT}<={checks[c].subject for c in required}:raise RepoError("ARTIFACT_EVIDENCE_REQUIRED")
    for p in [*contract.protected_paths,*contract.forbidden_paths,*contract.allowed_paths]:
        if not p or ".." in p.split("/") or p.startswith("/") or "\\" in p or ":" in p:raise RepoError("INVALID_SCOPE")
    return True


def freeze_contract(contract: Contract, reviewer: str):
    if not reviewer.strip():raise RepoError("REVIEWER_REQUIRED")
    validate_contract(contract)
    return contract.model_copy(update={"status":"FROZEN","reviewer":reviewer})


def evidence_matches(e, intent, contract, artifact, store):
    check=next((c for c in contract.checks if c.id==e.check_id),None)
    if not check or not store.verify(e.model_dump(mode="json",exclude={"signature"}),e.signature):return False
    pairs=[(e.run_id,intent.run_id),(e.task_spec_revision,intent.task_spec_revision),(e.contract_digest,intent.contract_digest),
           (e.release_digest,intent.release_digest),(e.knowledge_snapshot_digest,intent.knowledge_snapshot_digest),
           (e.knowledge_index_digest,intent.knowledge_index_digest),(e.environment_digest,intent.environment_digest),
           (e.definition_digest,check.digest),(e.subject,check.subject),(e.grader_digest,GRADER_VERSION),(e.config_digest,digest(check.definition))]
    if any(a!=b for a,b in pairs) or e.status!="PASSED":return False
    if e.authority not in check.permitted_authorities:return False
    if e.authority in {Authority.IN_PROCESS, Authority.SELF}:return False
    if e.expected_ids!=e.executed_ids or not e.executed_ids or e.skipped_ids:return False
    if e.subject==Subject.SOURCE:return e.source_before==e.source_after==intent.source_digest
    if e.subject==Subject.BUILD:return artifact is not None and e.build_input_source==intent.source_digest and e.built_artifact==artifact
    if e.subject==Subject.ARTIFACT:return artifact is not None and e.tested_artifact==artifact
    return False


def delivery_verified(intent,contract,evidence,artifact,store):
    try:validate_contract(contract,True)
    except RepoError:return False,"INVALID_CONTRACT"
    if digest(sorted(contract.required_checks))!=intent.required_check_set_digest:return False,"REQUIRED_SET_MISMATCH"
    # Independent full required set: a green coverage matrix cannot hide a failing global check.
    for cid in contract.required_checks:
        e=evidence.get(cid)
        if e is None or e.check_id!=cid or not evidence_matches(e,intent,contract,artifact,store):return False,"REQUIRED_CHECK_FAILED_OR_INVALID"
    for req in contract.requirements:
        valid=[evidence[c] for c in contract.coverage[req.id]]
        if not set(req.subjects)<={e.subject for e in valid}:return False,"INSUFFICIENT_EVIDENCE"
        if req.critical and not any(e.authority in req.authorities for e in valid):return False,"INSUFFICIENT_EVIDENCE"
    return True,"REGISTERED_CHECKS_PASSED"

# This adapter is copied into the untrusted domain. It receives INPUTS, never expected answers.
FUNCTION_ADAPTER='''import importlib,sys,json,asyncio,inspect,zipfile,pathlib
p=json.loads(sys.stdin.read())
if p.get("artifact"):
    z=zipfile.ZipFile(p["artifact"])
    root=pathlib.Path("/tmp/installed");root.mkdir(exist_ok=True)
    for n in z.namelist():
        q=pathlib.PurePosixPath(n)
        if q.is_absolute() or ".." in q.parts:raise ValueError("unsafe wheel")
    z.extractall(root);sys.path.insert(0,str(root))
else:sys.path.insert(0,"/workspace")
m=importlib.import_module(p["module"])
if p.get("artifact") and (not getattr(m,"__file__",None) or not pathlib.Path(m.__file__).resolve().is_relative_to(root)):
    raise ValueError("module did not originate from artifact")
f=getattr(m,p["function"])
result=f(*p.get("args",[]),**p.get("kwargs",{}))
if inspect.isawaitable(result):result=asyncio.run(result)
print(json.dumps({"result":result},ensure_ascii=False))
'''
ASGI_ADAPTER='''import sys,json,asyncio,importlib
sys.path.insert(0,"/workspace")
p=json.loads(sys.stdin.read());app=getattr(importlib.import_module(p["module"]),p.get("app","app"))
async def call():
    sent=False;out=[]
    body=json.dumps(p.get("json",{})).encode() if "json" in p else p.get("body","").encode()
    async def receive():
        nonlocal sent
        if sent:return {"type":"http.disconnect"}
        sent=True;return {"type":"http.request","body":body,"more_body":False}
    async def send(m):out.append(m)
    headers=[(k.lower().encode(),v.encode()) for k,v in p.get("headers",{}).items()]
    if "json" in p:headers.append((b"content-type",b"application/json"))
    scope={"type":"http","asgi":{"version":"3.0"},"http_version":"1.1","method":p.get("method","GET"),"scheme":"http","path":p["path"],"raw_path":p["path"].encode(),"query_string":p.get("query","").encode(),"headers":headers,"client":("127.0.0.1",1234),"server":("test",80),"root_path":""}
    await app(scope,receive,send)
    status=next(m["status"] for m in out if m["type"]=="http.response.start")
    text=b"".join(m.get("body",b"") for m in out if m["type"]=="http.response.body").decode()
    try:body=json.loads(text)
    except ValueError:body=text
    print(json.dumps({"status":status,"body":body}))
asyncio.run(call())
'''

class Grader:
    def __init__(self,store,sandbox):self.store,self.sandbox=store,sandbox
    async def evaluate(self,intent: VerificationIntent,contract: Contract):
        evidence={};artifact=None
        ordered=sorted((c for c in contract.checks if c.id in contract.required_checks),key=lambda c:0 if c.subject==Subject.SOURCE else 1 if c.subject==Subject.BUILD else 2)
        for check in ordered:
            ev,newartifact=await self._check(intent,check,artifact)
            evidence[check.id]=ev
            if newartifact:artifact=newartifact
            self.store.put("evidence",ev.id,ev,immutable=True)
        valid,reason=delivery_verified(intent,contract,evidence,artifact,self.store)
        decision=PreparedDecision(intent_digest=intent.digest,verified=valid,reason=reason,evidence_ids=[e.id for e in evidence.values()],artifact_digest=artifact)
        decision=decision.model_copy(update={"signature":self.store.sign(decision.model_dump(mode="json",exclude={"signature"}))})
        self.store.put("decision",intent.digest,decision,immutable=False)
        return decision
    async def _check(self,intent,check,artifact):
        definition=check.definition;reports=[];executed=[];expected=[];passed=True;newartifact=None
        authority=Authority.EXTERNAL if self.sandbox.isolated else Authority.IN_PROCESS
        fields={"source_before":intent.source_digest,"source_after":intent.source_digest} if check.subject==Subject.SOURCE else {}
        cases=[{**{k:v for k,v in definition.items() if k!="cases"},**case} for case in definition.get("cases",[definition])];expected=[f"{check.id}::{case.get('id',i)}" for i,case in enumerate(cases)]
        with tempfile.TemporaryDirectory(prefix="grader-",dir=self.store.root) as temp:
            work=Path(temp)/"work";work.mkdir();ws=Workspace(work,self.store);ws.restore(intent.source_digest)
            try:
                if check.kind=="static_python":
                    for rel in ws.manifest()["files"]:
                        if rel.endswith(".py"):ast.parse(ws.path(rel).read_bytes(),filename=rel)
                    executed=expected;reports=[{"parsed":True}]
                elif check.kind=="human":
                    approval=self.store.get("human_acceptance",digest([intent.run_id,check.id,intent.source_digest]))
                    passed=bool(approval and approval.get("approved") and approval.get("contract")==intent.contract_digest);authority=Authority.HUMAN;executed=expected;reports=[{"confirmed":passed}]
                elif check.kind=="file_json":
                    for i,case in enumerate(cases):
                        actual=json.loads(ws.read(case["path"])["content"]);ok=actual==case["expected"];passed &= ok
                        reports.append({"actual":actual,"passed":ok});executed.append(expected[i])
                elif check.kind=="build":
                    result=await self.sandbox.run(intent.run_id,work,definition["argv"],timeout=check.timeout,export=True)
                    file=ws.path(definition["output"])
                    passed=result["exit_code"]==0 and file.is_file() and self.sandbox.isolated
                    if passed:
                        newartifact=self.store.json_blob({"files":{file.name:{"blob":self.store.blob(file.read_bytes()),"executable":False}}})
                    authority=Authority.BUILD if self.sandbox.isolated else Authority.IN_PROCESS
                    fields={"build_input_source":intent.source_digest,"built_artifact":newartifact};reports=[result];executed=expected
                else:
                    artifact_name=None
                    if check.subject==Subject.ARTIFACT:
                        if not artifact:raise RepoError("ARTIFACT_MISSING")
                        am=self.store.read_json(artifact)["files"]
                        if len(am)!=1:raise RepoError("ONE_ARTIFACT_REQUIRED")
                        artifact_name=next(iter(am))
                        # Test the artifact ALONE: source checkout cannot satisfy a missing wheel module.
                        work=Path(temp)/"artifact-only";work.mkdir()
                        atomic_write(work/artifact_name,self.store.read_blob(am[artifact_name]["blob"]))
                        fields={"tested_artifact":artifact}
                    for i,case in enumerate(cases):
                        if check.kind in {"function","artifact_function","asgi"}:
                            args={k:v for k,v in case.items() if k not in {"expected","expected_status","expected_body","expected_json","id"}}
                            if artifact_name:args["artifact"]="/workspace/"+artifact_name
                            script=ASGI_ADAPTER if check.kind=="asgi" else FUNCTION_ADAPTER
                            # Adapter code is fixed by Host, not overwritten by a candidate file.
                            result=await self.sandbox.run(intent.run_id,work,["python","-c",script],stdin=json.dumps(args),timeout=check.timeout,export=False,readonly=True)
                            try:
                                actual=json.loads(result["stdout"])
                                wanted={"status":case["expected_status"],"body":case["expected_body"]} if check.kind=="asgi" else {"result":case["expected"]}
                                ok=actual==wanted
                            except (ValueError,KeyError):ok=False
                        else:
                            result=await self.sandbox.run(intent.run_id,work,case["argv"],stdin=case.get("stdin",""),timeout=check.timeout,export=False,readonly=True)
                            if check.kind=="diagnostic":
                                authority=Authority.IN_PROCESS;ok=result["exit_code"]==case.get("exit_code",0)
                            else:ok=result["stdout"]==case.get("stdout","") and result["exit_code"]==case.get("exit_code",0)
                        ok=ok and not result["timeout"] and not result["cancelled"] and result["exit_code"]==case.get("exit_code",0)
                        passed &= ok;reports.append({"observation":result,"passed":bool(ok)});executed.append(expected[i])
            except Exception as exc:
                passed=False;reports.append({"error_type":type(exc).__name__,"error":str(exc)[:1000]})
            # The sealed source lives in the CA store; each DUT runs its own copy.
            if check.subject==Subject.SOURCE and ws.snapshot()!=intent.source_digest:
                fields["source_after"]=ws.snapshot();passed=False
        ev=Evidence(id=uid("evidence-"),run_id=intent.run_id,task_spec_revision=intent.task_spec_revision,contract_digest=intent.contract_digest,
            release_digest=intent.release_digest,knowledge_snapshot_digest=intent.knowledge_snapshot_digest,knowledge_index_digest=intent.knowledge_index_digest,
            environment_digest=intent.environment_digest,check_id=check.id,definition_digest=check.digest,subject=check.subject,authority=authority,
            status="PASSED" if passed else "FAILED",expected_ids=expected,executed_ids=executed,report_digest=self.store.json_blob(reports),grader_digest=GRADER_VERSION,config_digest=digest(check.definition),**fields)
        ev=ev.model_copy(update={"signature":self.store.sign(ev.model_dump(mode="json",exclude={"signature"}))})
        return ev,newartifact
