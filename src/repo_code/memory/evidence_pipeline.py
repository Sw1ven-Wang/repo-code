"""Versioned, bounded DEV evidence views. Selectors never execute trace commands."""
from __future__ import annotations
import ast
import contextlib
import json
import re
from typing import Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ..util import RepoError, digest, redact, canonical

Digest = Annotated[str, Field(pattern=r'^sha256:[0-9a-f]{64}$')]
Name = Annotated[str, Field(min_length=1, max_length=256)]
Role = Literal['ORIGINAL_FAILURE','ROOT_CAUSE_DIAGNOSIS','IMPLEMENTATION_CHANGE',
               'POSITIVE_VERIFICATION','BOUNDARY_COUNTEREXAMPLE','ENVIRONMENT_BLOCKER',
               'CONTRACT_CONTEXT','BASELINE_COMPARISON','UNCLASSIFIED']
CORE_ROLES = ['ORIGINAL_FAILURE','ROOT_CAUSE_DIAGNOSIS','IMPLEMENTATION_CHANGE',
              'POSITIVE_VERIFICATION','BOUNDARY_COUNTEREXAMPLE']

class Schema(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)

class EvidenceMetadata(Schema):
    evidence_id: Name
    trace_id: Digest
    event_sequence: int = Field(ge=0)
    role: list[Role] = Field(min_length=1,max_length=9)
    evidence_type: Name
    summary: str = Field(max_length=1200)
    content_digest: Digest
    allowed_selectors: list[Name] = Field(min_length=1,max_length=20)
    content_bytes: int = Field(ge=0)
    redaction_status: Literal['REDACTED','NOT_REQUIRED']
    authorization_scope: Name
    operation_id: str | None = None
    tool_call_id: str | None = None
    artifact_refs: list[Digest] = Field(default_factory=list,max_length=100)
    duplicate_source_ids: list[Name] = Field(default_factory=list,max_length=1000)
    quality: int = Field(ge=0,le=100)
    limitations: list[str] = Field(default_factory=list,max_length=20)

class EpisodeMetadata(Schema):
    schema_version: Literal['curator-metadata-v2']
    episode_id: Digest
    trace_id: Digest
    task_family: str | None
    stage_type: Name
    task_status: Name
    event_ids: list[Name] = Field(max_length=2000)
    changed_files: list[str] = Field(max_length=500)
    verification_summary: dict
    environment_summary: dict
    cost_summary: dict
    evidence_catalogue: list[EvidenceMetadata] = Field(max_length=2000)
    missing_roles: list[Role] = Field(max_length=9)
    public_task_context: dict | None = None
    existing_rules: str = Field(max_length=10000)
    condition_keys: list[Name] = Field(max_length=30)
    legacy_summary: dict | None = None

def validate_metadata(value):
    try:return EpisodeMetadata.model_validate(value)
    except (ValidationError,TypeError,ValueError) as exc:raise RepoError('INVALID_METADATA','INVALID_METADATA: '+str(exc)[:500]) from None

def adapt_legacy_metadata(value):
    """Inspection-only migration; never manufactures evidence or verification."""
    if not isinstance(value,dict) or not value.get('episode_id'):raise RepoError('INVALID_METADATA')
    if value.get('schema_version')=='curator-metadata-v2':return validate_metadata(value)
    if value.get('schema_version') not in (None,'curator-metadata-v1'):raise RepoError('INVALID_METADATA')
    return validate_metadata(dict(schema_version='curator-metadata-v2',episode_id=value['episode_id'],
        trace_id=value.get('trace_id',digest(value)),task_family=value.get('task_family'),
        stage_type=value.get('stage_type','UNKNOWN'),task_status=value.get('outcome','UNKNOWN'),
        event_ids=value.get('catalogue',[]),changed_files=value.get('changed_files',[]),
        verification_summary={'authority':'LEGACY_UNVERIFIED'},environment_summary={},
        cost_summary=value.get('cost',{}),evidence_catalogue=[],missing_roles=CORE_ROLES,
        existing_rules='',condition_keys=[],legacy_summary={'read_only':True,'original_digest':digest(value)}))

def clipped(value,limit):
    raw=value.encode('utf-8')
    if len(raw)<=limit:return value
    marker='\n[BOUNDED EXCERPT; NOT CONTIGUOUS]\n'
    if limit<64:return raw[:limit].decode('utf-8','ignore')
    half=(limit-len(marker.encode()))//2
    return raw[:half].decode('utf-8','ignore')+marker+raw[-half:].decode('utf-8','ignore')

def symbols(text):
    # Tokens are links for candidate retrieval, never a root-cause assertion.
    stop={'import','from','return','False','True','None','print','assert','python','self',
          'def','for','with','else','and','not','in','if','new','old','args','value','test'}
    return {s for s in re.findall(r'\b[A-Za-z_][A-Za-z_0-9]{3,}\b',text) if s not in stop}

def operation(store,event,run_id,c=None):
    d=event['data'];body=d.get('result') or {};args={}
    own=c is None;c=c or store.connect()
    try:
        row=c.execute('SELECT request FROM operations WHERE id=? AND run_id=?',(d.get('operation_id'),run_id)).fetchone()
        if row:args=json.loads(row['request'])['arguments']
        if d.get('action_digest') and digest([d.get('tool_name'),args])!=d['action_digest']:
            raise RepoError('EVIDENCE_DIGEST_CHANGED')
        if body.get('truncated') and (body.get('result_artifact') or body.get('artifact')):
            ref=body.get('result_artifact') or body['artifact']
            if not store.get('run_artifact',digest([run_id,ref]),c):raise RepoError('EVIDENCE_ARTIFACT_DENIED')
            body=store.read_json(ref)
        return args,body
    finally:
        if own:c.close()

def mutation_parts(tool,args):
    """Saved operation payloads only, never reconstructed workspace content."""
    entries=args.get('operations',[]) if tool=='patch' else [args]
    out=[]
    for entry in entries:
        if not isinstance(entry,dict):continue
        if tool=='patch' and entry.get('kind') not in {'create','replace','edit'}:continue
        if isinstance(entry.get('content'),str):out.append({'path':entry.get('path',''),'new':entry['content']})
        replacements=entry.get('replacements',[entry])
        if isinstance(replacements,list):
            for rep in replacements:
                if isinstance(rep,dict) and isinstance(rep.get('new_text'),str):
                    out.append({'path':entry.get('path',''),'old':rep.get('old_text',''),'new':rep['new_text']})
    return out

def has_assertions(text):
    try:
        import textwrap
        return any(isinstance(n,ast.Assert) for n in ast.walk(ast.parse(textwrap.dedent(text))))
    except (SyntaxError,ValueError,TypeError):return False

def inline_assertions(args):
    argv=args.get('argv',[])
    if not isinstance(argv,list) or not argv:return False
    exe=str(argv[0]).replace('\\','/').rsplit('/',1)[-1]
    if not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?(?:\.exe)?',exe) or any(x in argv for x in ['-O','-OO']):return False
    if len(argv)==3 and argv[1]=='-c':return has_assertions(argv[2])
    return argv[1:]==['-'] and has_assertions(args.get('stdin',''))

def project_test_link(args,changed):
    """Candidate association, not proof the suite covered any particular file."""
    argv=args.get('argv',[])
    if not isinstance(argv,list) or not argv:return False
    exe=str(argv[0]).replace('\\','/').rsplit('/',1)[-1]
    if exe in {'pytest','pytest.exe','py.test','py.test.exe'}:tail=argv[1:]
    elif re.fullmatch(r'python(?:\d+(?:\.\d+)*)?(?:\.exe)?',exe) and argv[1:3]==['-m','pytest']:tail=argv[3:]
    else:return False
    targets=[];skip=False
    for arg in tail:
        if skip:skip=False;continue
        if arg in {'-k','-m','-c','--rootdir','--confcutdir','--override-ini','-o','--maxfail'}:skip=True;continue
        if not isinstance(arg,str) or arg.startswith('-'):continue
        target=arg.split('::',1)[0].replace('\\','/').removeprefix('./').rstrip('/')
        if target.startswith('/') or ':' in target or '..' in target.split('/'):return False
        targets.append(target)
    if not targets:return any(p.endswith('.py') for p in changed)
    return any(t in {'','.'} or any(p==t or p.startswith(t+'/') for p in changed) for t in targets)

def complete_diff_view(body,changed,baseline):
    progress=body.get('inspection_progress') or {}
    return (bool(baseline) and body.get('baseline')==baseline and bool(body.get('diff')) and body.get('truncated') is False
        and body.get('offset')==0 and 'next_offset' in body and body['next_offset'] is None
        and bool(set(body.get('changed_paths',[]))&changed)
        and all(isinstance(v,str) and re.fullmatch(r'sha256:[0-9a-f]{64}',v)
                for v in [body.get('baseline'),progress.get('source_digest')])
        and not any(body.get(k) for k in ['is_error','timeout','cancelled','running']))

def causal_segments(store,events,run_id,c=None):
    """Link by saved files, symbols and test identities; never by time alone."""
    records=[];changed=set();seed=set();first_write=None
    if c is None:
        with contextlib.closing(store.connect()) as db:run_row=db.execute('SELECT data FROM runs WHERE id=?',(run_id,)).fetchone()
    else:run_row=c.execute('SELECT data FROM runs WHERE id=?',(run_id,)).fetchone()
    baseline=json.loads(run_row[0])['manifest'].get('workspace_baseline_digest') if run_row else None
    for e in events:
        if e['type']!='tool.operation_finished':continue
        args,body=operation(store,e,run_id,c);tool=e['data'].get('tool_name')
        records.append((e,tool,args,body))
        if tool in {'edit','patch','create','write'} and body.get('changed_paths') and not body.get('is_error'):
            changed.update(body['changed_paths']);first_write=first_write or e['sequence']
            seed|=symbols(' '.join(str(args.get(k,'')) for k in ['old_text','new_text','content','patch'])+' '+canonical(mutation_parts(tool,args)).decode())
    if not seed:return {},[],{'rule':'NO_IMPLEMENTATION_LINK','symbols':[]}
    segments={role:[] for role in CORE_ROLES+['ENVIRONMENT_BLOCKER','UNCLASSIFIED']}
    links=[]
    for e,tool,args,body in records:
        text=' '.join(str(args.get(k,'')) for k in ['argv','stdin','path','content','old_text','new_text','patch'])+' '+canonical(mutation_parts(tool,args) if tool in {'edit','patch','create','write'} else []).decode()
        same_file=bool(args.get('path') in changed or set(body.get('changed_paths',[]))&changed or any(path in text for path in changed))
        overlap=sorted(symbols(text)&seed)
        related=same_file or bool(overlap) or (tool=='process.run' and project_test_link(args,changed))
        if not related:continue
        roles=[]
        failed=body.get('exit_code') not in (None,0)
        output=str(body.get('stderr',''))+' '+str(body.get('stdout',''))
        environment=failed and bool(re.search(r'No module named|ModuleNotFoundError|command not found|Permission denied|ConnectionError',output))
        if environment:roles=['ENVIRONMENT_BLOCKER']
        elif tool in {'edit','patch','create','write'}:
            committed=body.get('changed_paths') and not body.get('is_error')
            roles=['IMPLEMENTATION_CHANGE'] if committed else ['ROOT_CAUSE_DIAGNOSIS']
            if committed and any(has_assertions(p['new']) for p in mutation_parts(tool,args)):roles+=['BOUNDARY_COUNTEREXAMPLE']
        elif tool=='process.run':
            if first_write and e['sequence']<first_write:
                roles=['ORIGINAL_FAILURE' if not segments['ORIGINAL_FAILURE'] else 'ROOT_CAUSE_DIAGNOSIS']
            elif body.get('exit_code')==0 and not any(body.get(k) for k in ['timeout','cancelled','running','is_error']):
                roles=['POSITIVE_VERIFICATION']
            elif failed:roles=['ROOT_CAUSE_DIAGNOSIS']
            if inline_assertions(args) and body.get('exit_code') is not None and not any(body.get(k) for k in ['timeout','cancelled','running']):roles+=['BOUNDARY_COUNTEREXAMPLE']
        elif tool in {'read','search','workspace.diff'}:
            roles=['ROOT_CAUSE_DIAGNOSIS']
            if tool=='workspace.diff' and complete_diff_view(body,changed,baseline):roles+=['IMPLEMENTATION_CHANGE']
        if not roles:roles=['UNCLASSIFIED']
        for role in roles:segments[role].append(e['event_id'])
        links.append({'event_id':e['event_id'],'operation_id':e['data'].get('operation_id'),
            'file_link':same_file,'shared_symbols':overlap[:30],'roles':roles,'authority':'CANDIDATE_ROLE_NOT_SEMANTIC_PROOF'})
    ids={ref for refs in segments.values() for ref in refs}
    ordered=[e['event_id'] for e in events if e['event_id'] in ids]
    return segments,ordered,{'rule':'FILES_SYMBOLS_CHECK_IDENTITIES_V2_1','symbols':sorted(seed)[:200],'links':links}

class EvidencePipeline:
    VERSION='evidence-catalogue-v2.2-local-check-windows'
    def __init__(self,owner):
        self.owner=owner;self.store=owner.repo.store;self.repo=owner.repo

    def build(self,episode_id):
        from ..episodes import EpisodeBuilder
        facts=EpisodeBuilder(self.store).evidence(episode_id);episode=facts['episode']
        roles={sid:[r for r,refs in episode.get('segments',{}).items() if sid in refs] for sid in episode['support_refs']}
        if not episode.get('segments'):
            inferred,_,_=causal_segments(self.store,facts['events'],episode['run_id'])
            roles={sid:[r for r,refs in inferred.items() if sid in refs] for sid in episode['support_refs']}
        records={};changed=set();creations={};secrets=self.owner.history.known_secrets(episode['run_id'])
        def scrub(text):
            for secret in secrets:text=text.replace(secret,'[REDACTED]')
            return redact(text)
        for event in facts['events']:
            if event['type']!='tool.operation_finished':continue
            args,body=operation(self.store,event,episode['run_id']);tool=event['data'].get('tool_name') or 'unknown'
            self.repo.ensure_dev([event['event_id']])
            if EpisodeBuilder._protected(args) or EpisodeBuilder._protected(body):raise RepoError('PROTECTED_EVALUATION_CONTENT')
            fields={}
            for key in ['path','old_text','new_text','content','patch']:
                if isinstance(args.get(key),str):fields[{'old_text':'old_hunk','new_text':'new_hunk','content':'created_content','patch':'diff_hunk'}.get(key,key)]=args[key]
            if tool=='patch' and isinstance(args.get('operations'),list):
                # Retain exact per-path mapping as well as quotable saved text.
                # These strings are evidence excerpts, never a synthesized file.
                fields['patch_operations']=canonical(args['operations']).decode()
                parts=mutation_parts(tool,args)
                for name,key in [('old_hunk','old'),('new_hunk','new')]:
                    values=[p[key] for p in parts if isinstance(p.get(key),str)]
                    if values:fields[name]='\n\n'.join(values)
            if tool=='read' and isinstance(body.get('content'),str):fields['read_content']=body['content']
            if tool=='workspace.diff':fields['diff_hunk']=str(body.get('diff',body.get('content',body)))
            if tool=='process.run':
                fields['command_summary']=json.dumps(args.get('argv',[]),ensure_ascii=False)+(('\nSTDIN:\n'+args['stdin']) if args.get('stdin') else '')
                fields['exit_status']=json.dumps({k:body.get(k) for k in ['exit_code','timeout','cancelled','running','is_error','environment_digest','output_snapshot','checked_probe','independent_check']},ensure_ascii=False)
                fields['stdout_excerpt']=str(body.get('stdout',''));fields['stderr_excerpt']=str(body.get('stderr',''))
            if not fields:fields['result_summary']=json.dumps(body,ensure_ascii=False)
            # Definitions are parsed only from recorded data, never from a caller path.
            definitions={};incomplete=[]
            for text in list(fields.values()):
                try:
                    import textwrap
                    code=textwrap.dedent(text)
                    try:tree=ast.parse(code)
                    except SyntaxError:
                        # Parse only exact, independently complete recorded blocks. No synthetic code.
                        lines=code.splitlines();blocks=[]
                        for start,line in enumerate(lines):
                            if not re.match(r'\s*(?:async\s+)?(?:def|class)\s+',line):continue
                            indent=len(line)-len(line.lstrip());end=start+1
                            while end<len(lines) and (not lines[end].strip() or len(lines[end])-len(lines[end].lstrip())>indent):end+=1
                            snippet=textwrap.dedent('\n'.join(lines[start:end]))
                            try:blocks.append((snippet,ast.parse(snippet)))
                            except SyntaxError:incomplete.append(line.strip())
                        for snippet,block in blocks:
                            for node in ast.walk(block):
                                if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                                    definitions[node.name]='\n'.join(snippet.splitlines()[node.lineno-1:node.end_lineno])
                        continue
                    for node in ast.walk(tree):
                        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                            if node.end_lineno>len(code.splitlines()):continue
                            definitions[node.name]='\n'.join(code.splitlines()[node.lineno-1:node.end_lineno])
                except (SyntaxError,ValueError):pass
            if definitions:fields['function_definition']='\n\n'.join(definitions.values())
            if incomplete:fields['definition_status']=json.dumps({'status':'INCOMPLETE_SOURCE','signatures':list(dict.fromkeys(incomplete)),'function_body_synthesized':False},ensure_ascii=False)
            raw=canonical(fields).decode();fields={k:scrub(v) for k,v in fields.items()}
            sid=event['event_id'];r=roles.get(sid) or ['UNCLASSIFIED']
            changed.update(body.get('changed_paths',[]));quality=50
            limitations=['Role classification and zero exit do not establish semantic or official correctness']
            if 'BOUNDARY_COUNTEREXAMPLE' in r:limitations.append('Assertion syntax is a candidate boundary hint, not proof of execution or semantic coverage')
            if 'IMPLEMENTATION_CHANGE' in r:quality=90 if definitions else 65
            if 'POSITIVE_VERIFICATION' in r:
                command=fields.get('command_summary','');stdout=fields.get('stdout_excerpt','')
                quality=85 if re.search(r'\bassert\b|test_\w+|regressions passed',command+' '+stdout) else 65
                if re.search(r'\d+ (?:deselected|skipped)',stdout) or '"-k"' in command:quality=30;limitations.append('PARTIAL_CHECK_SELECTION')
                if body.get('execution_evidence'):quality=min(100,quality+10)
            if 'ROOT_CAUSE_DIAGNOSIS' in r:quality=85 if tool=='process.run' else 55
            if 'ENVIRONMENT_BLOCKER' in r:quality=10
            if 'created_content' in fields:
                limitations.append('Assertions in created source are not proof they executed')
            artifacts=sorted({source[k] for source in (event['data'].get('result') or {},body) for k in ('result_artifact','artifact')
                              if isinstance(source.get(k),str) and re.fullmatch(r'sha256:[0-9a-f]{64}',source[k])})
            for ref in artifacts:
                self.repo.ensure_dev([ref]);self.store.read_blob(ref)
            summary=clipped(fields.get('command_summary',fields.get('path',tool)),650)
            if tool=='process.run':summary+=' | exit='+str(body.get('exit_code'))+' | '+clipped(fields.get('stdout_excerpt','')[-250:] or fields.get('stderr_excerpt','')[-250:],350)
            m=EvidenceMetadata(evidence_id=sid,trace_id=episode['trace_manifest_ref'],event_sequence=event['sequence'],
                role=r,evidence_type=tool,summary=summary,content_digest=digest(fields),allowed_selectors=list(fields),
                content_bytes=len(canonical(fields)),redaction_status='NOT_REQUIRED' if raw==canonical(fields).decode() else 'REDACTED',
                authorization_scope='run:'+episode['run_id'],operation_id=event['data'].get('operation_id'),
                tool_call_id=event['data'].get('tool_call_id'),artifact_refs=artifacts,quality=quality,limitations=limitations)
            records[sid]={'metadata':m.model_dump(),'fields':fields,'definitions':{k:scrub(v) for k,v in definitions.items()}}
            if tool=='create':creations[sid]=body.get('versions',{})
        for sid,versions in creations.items():
            record=records[sid];content=record['fields'].get('created_content','')
            try:assertion_count=sum(isinstance(n,ast.Assert) for n in ast.walk(ast.parse(content)))
            except SyntaxError:assertion_count=None
            path=record['fields'].get('path','');functions=list(record['definitions'])
            executions=[other for other,r in records.items() if r['metadata']['event_sequence']>record['metadata']['event_sequence']
                and 'command_summary' in r['fields'] and (bool(path and path in r['fields']['command_summary']) or any(name in r['fields']['command_summary'] for name in functions))]
            record['fields']['creation_summary']=scrub(canonical({'created_versions':versions,'test_functions':functions,
                'assertion_count_in_source':assertion_count,'subsequent_invocation_source_ids':executions,
                'execution_status':'INVOCATION_OBSERVED' if executions else 'NO_INVOCATION_FOUND',
                'authority':'Recorded invocation is not proof each assertion executed'}).decode())
            record['metadata'].update(content_digest=digest(record['fields']),content_bytes=len(canonical(record['fields'])),allowed_selectors=list(record['fields']))
        # One environment representative per normalized error; originals remain addressable.
        groups={}
        for sid,record in records.items():
            if 'ENVIRONMENT_BLOCKER' in record['metadata']['role']:
                fp=digest(re.sub(r'\s+',' ',record['fields'].get('stderr_excerpt','')).strip())
                if fp in groups:records[groups[fp]]['metadata']['duplicate_source_ids'].append(sid)
                else:groups[fp]=sid
        existing={role for record in records.values() for role in record['metadata']['role']}
        from ..context import SYSTEM
        policy=self.store.get('operator_config','learning_baseline_policy',default={})
        public=None if episode.get('stage_candidate_id') else self.store.get('operator_config','learning_public_task_context')
        if public is not None:public={k:public[k] for k in ['instance_id','repo','base_commit','problem_statement'] if k in public}
        stage_verdict = {}
        if episode.get('stage_candidate_id'):
            stage_verdict = {'stage_outcome': episode['actual']['verified_stage'],
                             'verification_kind': episode['actual']['verification_kind']}
            stage_verdict['local_check_chains']=self._local_chains(episode,facts['events'],records)
        metadata=validate_metadata(dict(schema_version='curator-metadata-v2',episode_id=episode_id,
            trace_id=episode['trace_manifest_ref'],task_family=episode.get('task_family'),stage_type=episode['stage_type'],
            task_status=episode['actual'].get('run_state_at_check', episode['actual']['terminal_state']),event_ids=list(records),changed_files=sorted(changed),
            verification_summary={'failed_processes':episode['actual']['failed_checks'],'trusted_recoveries':episode['actual']['recoveries'],
                **stage_verdict,'authority':'OBSERVATIONS_ONLY_UNLESS_BOUND_CHECK','verification_incomplete':episode.get('verification_incomplete',True)},
            environment_summary={'blocker_groups':len(groups),'environment_errors_do_not_prove_code_failure':True},
            cost_summary=episode['cost'],evidence_catalogue=[r['metadata'] for r in records.values()],
            missing_roles=[r for r in CORE_ROLES if r not in existing],public_task_context=public,
            existing_rules=clipped(SYSTEM+'\n'+json.dumps(policy,ensure_ascii=False),10000),
            condition_keys=['task_family:'+str(episode.get('task_family')),'run_cancelled','source_changed']))
        value={'version':self.VERSION,'episode_id':episode_id,'metadata':metadata.model_dump(),
            'source_digests':{k:v['metadata']['content_digest'] for k,v in records.items()}}
        ref=digest(value)
        with self.store.tx() as c:
            self.repo.ensure_dev([episode_id,*records],c)
            self.store.put('evidence_catalogue',ref,value,c,immutable=True)
            self.repo.derive(ref,[episode_id,*records],c)
        return metadata,records,ref

    def _local_chains(self,episode,events,records):
        """Recheck signed stage links against original operations, not model-visible excerpts."""
        from ..verified_stages import VerifiedStages
        stages=VerifiedStages(self.owner.runtime);key=episode['stage_candidate_id']
        row=self.store.get(stages.KIND,key)
        if not row:raise RepoError('L1_STAGE_MISSING')
        stage=stages.payload(row)
        if (stage['id']!=key or stage['run_id']!=episode['run_id'] or stage['scope']!=episode['scope']
                or stage['source_digest']!=episode['source_version']):raise RepoError('L1_STAGE_SCOPE')
        pairs=stage.get('local_repair_pairs',[])
        if not pairs:return []
        by_op={e['data']['operation_id']:e for e in events if e['event_id'] in records}
        def original(event,c):
            d=event['data'];op=d['operation_id']
            row=c.execute('SELECT request,result,approved_epoch FROM operations WHERE id=? AND run_id=?',
                          (op,stage['run_id'])).fetchone()
            if (not row or row['approved_epoch']!=stage['control_epoch'] or not row['result']
                    or json.loads(row['result'])!=d['result']):raise RepoError('L1_STAGE_SOURCE_CHANGED')
            args,body=operation(self.store,event,stage['run_id'],c)
            if d.get('action_digest')!=digest([d['tool_name'],args]):raise RepoError('L1_STAGE_SOURCE_CHANGED')
            return args,body
        chains=[]
        with contextlib.closing(self.store.connect()) as c:
            for pair in pairs:
                fail=by_op.get(pair['failed']);passed=by_op.get(pair['passed'])
                if not fail or not passed:continue
                if not (fail['sequence']<passed['sequence']<stage['sequence']
                        and {pair['failed'],pair['passed']}<=set(stage['refs'])
                        and pair['after']==stage['source_digest'] and pair['before']!=pair['after']):continue
                def checked(e,source,success):
                    args,body=original(e,c);probe=body.get('checked_probe')
                    return (e['data']['tool_name']=='process.run'
                        and (body.get('output_snapshot') or body.get('relevant_state'))==source
                        and body.get('environment_digest')==episode['environment_digest']
                        and digest(['linked_probe',probe] if probe else [args.get('argv'),args.get('stdin','')])==pair['check']
                        and type(body.get('exit_code')) is int and (body['exit_code']==0)==success
                        and not any(body.get(f) for f in ('timeout','cancelled','running')))
                if not checked(fail,pair['before'],False) or not checked(passed,pair['after'],True):continue
                self.repo.ensure_dev([key,pair['before'],pair['after']],c)
                before=self.store.read_json(pair['before'])['files'];after=self.store.read_json(pair['after'])['files']
                for edit in reversed(events):
                    if not fail['sequence']<edit['sequence']<passed['sequence'] or edit['event_id'] not in records:continue
                    if edit['data']['tool_name'] not in {'create','edit','patch'}:continue
                    _,body=original(edit,c)
                    if body.get('is_error') or not any(v and p in body.get('changed_paths',[]) and
                            before.get(p,{}).get('blob')!=v==after.get(p,{}).get('blob') for p,v in body.get('versions',{}).items()):continue
                    self.repo.ensure_dev([key,pair['before'],pair['after'],*(e['data']['operation_id'] for e in (fail,edit,passed))],c)
                    chains.append({'failed':fail['event_id'],'change':edit['event_id'],'passed':passed['event_id'],
                                   'check_digest':pair['check'],'authority':'LOCAL_SAME_CHECK_ONLY'})
                    break
        return chains

    def select(self,records,max_bytes=24000,chains=()):
        if not 1024<=max_bytes<=48000:raise RepoError('EVIDENCE_BUDGET')
        chosen=[];used=0;covered=set();duplicates={d for r in records.values() for d in r['metadata']['duplicate_source_ids']}
        order=[chain[k] for chain in chains for k in ('failed','change','passed')]
        for role in CORE_ROLES:
            ranked=sorted([k for k,v in records.items() if role in v['metadata']['role'] and k not in duplicates],key=lambda k:(-records[k]['metadata']['quality'],records[k]['metadata']['event_sequence']))
            # Two complementary implementation/verification sources, rather than last exit 0.
            order.extend(ranked[:2] if role in {'IMPLEMENTATION_CHANGE','POSITIVE_VERIFICATION','ROOT_CAUSE_DIAGNOSIS'} else ranked[:1])
        order.extend(sorted(records,key=lambda k:(-records[k]['metadata']['quality'],records[k]['metadata']['event_sequence'])))
        order=list(dict.fromkeys(order))
        # Under pressure give each essential role a quotable selector before spending on context.
        preferred=list(dict.fromkeys([*(chain[k] for chain in chains for k in ('failed','change','passed')),
            *(next((i for i in order if role in records[i]['metadata']['role']),None) for role in CORE_ROLES)]))
        preferred=[i for i in preferred if i is not None]
        def view(sid,fields):
            m=records[sid]['metadata']
            return {'evidence_id':sid,'source_digest':m['content_digest'],'roles':m['role'],'selectors':fields}
        full={sid:view(sid,{k:clipped(v,1600) for k,v in records[sid]['fields'].items()}) for sid in order}
        tight=sum(len(canonical(full[i]))+1 for i in preferred)+2>max_bytes
        order=list(dict.fromkeys(preferred+order))
        for ix,sid in enumerate(order):
            if sid in duplicates:continue
            record=records[sid];source=full[sid]
            available=max_bytes-used-(1 if chosen else 2)
            share=available//max(1,len(preferred)-ix) if tight else available
            if len(canonical(source))+1>share:
                fields=record['fields']
                keys=('new_hunk','created_content','diff_hunk','stderr_excerpt','stdout_excerpt','read_content','exit_status')
                k=next((k for k in (*keys,*fields) if fields.get(k)),None)
                if not k:continue
                # Even when fair share is smaller than the envelope, try a minimal source.
                length=min(1000,max(32,share-len(canonical(view(sid,{k:''})))-1))
                source=view(sid,{k:clipped(fields[k],length)})
                while len(canonical(source))+1>available and length>1:
                    length//=2;source=view(sid,{k:clipped(fields[k],length)})
                if not source['selectors'][k]:continue
            size=len(canonical(source))+(1 if chosen else 2)
            if used+size>max_bytes:continue
            chosen.append(source);used+=size;covered.update(record['metadata']['role'])
        return chosen,{'bytes':used,'budget_bytes':max_bytes,'selected_ids':[x['evidence_id'] for x in chosen],
            'covered_roles':sorted(covered),'missing_roles':[r for r in CORE_ROLES if r not in covered],
            'selection_version':'ROLE_COVERAGE_V3','budget_omissions':[i for i in preferred if i not in {s['evidence_id'] for s in chosen}],'role_labels_are_candidate_annotations':True}

    def expand(self,episode_id,records,request,attempt_id,max_bytes=8000):
        if set(request)-{'evidence_id','selector','source_digest','symbol','max_bytes','offset'}:raise RepoError('EVIDENCE_REQUEST_SCHEMA')
        sid=request.get('evidence_id');selector=request.get('selector');record=records.get(sid)
        if not record or selector not in record['metadata']['allowed_selectors']:raise RepoError('EVIDENCE_SELECTOR_DENIED')
        limit=request.get('max_bytes',2000)
        if type(limit) is not int or not 1<=limit<=max_bytes:raise RepoError('EVIDENCE_EXPANSION_BUDGET')
        if request.get('source_digest',record['metadata']['content_digest'])!=record['metadata']['content_digest']:raise RepoError('EVIDENCE_DIGEST_CHANGED')
        from ..episodes import EpisodeBuilder
        EpisodeBuilder(self.store).get(episode_id);self.repo.ensure_dev([episode_id,sid,*record['metadata']['artifact_refs']])
        # Rebuild from immutable events + saved operation arguments, including on replay.
        _,fresh,_=self.build(episode_id)
        if sid not in fresh or fresh[sid]['metadata']['content_digest']!=record['metadata']['content_digest']:raise RepoError('EVIDENCE_DIGEST_CHANGED')
        record=fresh[sid];text=record['fields'][selector]
        if selector=='function_definition':
            symbol=request.get('symbol')
            if symbol not in record['definitions']:raise RepoError('EVIDENCE_SYMBOL_DENIED')
            text=record['definitions'][symbol]
        elif 'symbol' in request:raise RepoError('EVIDENCE_SELECTOR_DENIED')
        field_start=record['fields'][selector].find(text) if selector=='function_definition' else 0
        if field_start<0:raise RepoError('EVIDENCE_DIGEST_CHANGED')
        offset=request.get('offset',0)
        if type(offset) is not int or offset<0 or offset>len(text):raise RepoError('EVIDENCE_OFFSET')
        content=text[offset:offset+limit].encode()[:limit].decode('utf8','ignore')
        if not content and offset<len(text):raise RepoError('EVIDENCE_EXPANSION_BUDGET')
        end=offset+len(content)
        value={'evidence_id':sid,'selector':selector,'source_digest':record['metadata']['content_digest'],
            'content':content,'offset':offset,'field_offset':field_start+offset,'next_offset':end if end<len(text) else None,
            'content_chars':len(text),'content_bytes':len(text.encode()),'truncated':offset>0 or end<len(text)}
        key=digest([attempt_id,sid,selector,request.get('symbol'),limit,offset])
        with self.store.tx() as c:
            self.repo.ensure_dev([episode_id,sid],c)
            self.store.put('evidence_expansion',key,value,c,immutable=True);self.repo.derive(key,[episode_id,sid],c)
        return key,value
