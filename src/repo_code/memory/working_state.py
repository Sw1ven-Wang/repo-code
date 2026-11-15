"""Deterministic Host facts and authorized history, never inferred completion."""
from __future__ import annotations

import contextlib
import json
import re
from ..util import RepoError, digest, redact_data
from .models import EvidenceRef, IntentFrame
from ..commands import command_evidence, no_match, verification_summary


_SECRET_LABEL=r'(?:api[_-]?key|authorization|password|secret|access[_-]?token|refresh[_-]?token|client[_-]?secret|bearer[_-]?token)'
_QUOTED_SECRET=re.compile(r'''(?i)(["']?'''+_SECRET_LABEL+r'''["']?\s*[:=]\s*)(["'])(.*?)\2''')
_SECRET_NAMES={'apikey','password','authorization','secret','accesstoken','refreshtoken','clientsecret','bearertoken','token'}


def semantic_text(text):
    text=_QUOTED_SECRET.sub(lambda m:m.group(1)+m.group(2)+'[REDACTED]'+m.group(2),text)
    text=re.sub(r'(?i)(authorization\s*[:=]\s*)Bearer\s+[^\s,;]+',r'\1[REDACTED]',text)
    return redact_data(text)


def clean_memory_data(value):
    if isinstance(value,str):return semantic_text(value)
    if isinstance(value,list):return [clean_memory_data(v) for v in value]
    if isinstance(value,dict):
        return {k:'[REDACTED]' if re.sub(r'[_-]','',k.lower()) in _SECRET_NAMES else clean_memory_data(v) for k,v in value.items()}
    return value


def sensitive_values(value,depth=0):
    if depth>8:return set()
    found=set()
    if isinstance(value,dict):
        for key,child in value.items():
            if re.sub(r'[_-]','',key.lower()) in _SECRET_NAMES and isinstance(child,str) and len(child)>=4:found.add(child)
            found.update(sensitive_values(child,depth+1))
    elif isinstance(value,list):
        for child in value:found.update(sensitive_values(child,depth+1))
    elif isinstance(value,str):
        found.update(m.group(3) for m in _QUOTED_SECRET.finditer(value) if len(m.group(3))>=4)
        found.update(re.findall(r'\bsk-[A-Za-z0-9_-]{12,}',value))
        try:decoded=json.loads(value)
        except (ValueError,TypeError):decoded=None
        if isinstance(decoded,(dict,list)):found.update(sensitive_values(decoded,depth+1))
    return found


def semantic_message(message):
    # Native reasoning/signatures are protocol, not learning material.
    result={k:message[k] for k in ("role","content","tool_calls","tool_call_id","is_error") if k in message}
    if result.get('tool_calls'):result['tool_calls']=[{k:v for k,v in call.items() if k!='raw_arguments'} for call in result['tool_calls']]
    result=clean_memory_data(result)
    if message.get('role')=='tool' and isinstance(message.get('content'),str):
        try:decoded=json.loads(message['content'])
        except ValueError:decoded=None
        if isinstance(decoded,(dict,list)):
            cleaned=clean_memory_data(decoded)
            if cleaned!=decoded:result['content']=json.dumps(cleaned,ensure_ascii=False)
    return result


def evidence_texts(record):
    message=record['message'];result=[(str(message.get('content','')),{'type':'json_pointer','value':'/content'})]
    if message['role']=='tool':
        try:value=json.loads(message.get('content',''))
        except (ValueError,TypeError):value=None
        if isinstance(value,dict):
            for field in ['content','stdout','stderr','preview']:
                if isinstance(value.get(field),str):
                    result.append((value[field],{'type':'embedded_json_pointer','container':'/content','value':'/'+field}))
    return result


class History:
    def __init__(self, repository, runtime):
        self.repo, self.store, self.runtime = repository, repository.store, runtime

    def records(self, run_id):
        run = self.runtime.run(run_id); m = run["data"]["manifest"]
        self.repo.check([m["knowledge_snapshot_digest"], m["knowledge_index_digest"], run_id])
        with contextlib.closing(self.store.connect()) as c:
            rows = c.execute("SELECT id,data FROM messages WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        result = [];tool_paths={}
        for row in rows:
            raw = json.loads(row["data"]); message = semantic_message(raw)
            role = raw["role"]
            for call in raw.get('tool_calls',[]):tool_paths[call['id']]=call.get('arguments',{}).get('path')
            ref = EvidenceRef(source_kind={"user": "user_message", "tool": "tool_result"}.get(role, "model_message"),
                              source_id=f"message:{row['id']}", content_digest=digest(message), run_id=run_id,
                              scope_id=m["scope"], visibility_domain=run["data"]["visibility"],
                              origin_class={"user": "USER_STATED", "tool": "HOST_OBSERVATION"}.get(role, "MODEL_PROPOSED"),
                              observed_at=run["data"]["created"])
            dependencies=[]
            if role=='tool':
                try:output=json.loads(raw.get('content',''))
                except (ValueError,TypeError):output={}
                if isinstance(output,dict):
                    path=tool_paths.get(raw.get('tool_call_id'))
                    if path and isinstance(output.get('version'),str):ref=ref.model_copy(update={'source_version':{path:output['version']}})
                    for name in ['artifact','source_artifact','result_artifact','output_snapshot']:
                        candidate=output.get(name)
                        if isinstance(candidate,str) and self.store.get('run_artifact',digest([run_id,candidate])):dependencies.append(candidate)
            self.repo.check([ref.source_id,ref.content_digest,*dependencies])
            record={"ref": ref.model_dump(), "message": redact_data(message), "seq": row["id"]}
            record['artifact_dependencies']=dependencies
            record['quote_fields']=[{'text':text,'selector':selector} for text,selector in evidence_texts(record)]
            result.append(record)
        return result

    def known_secrets(self,run_id):
        found=set()
        for message in self.store.messages(run_id):found.update(sensitive_values(message))
        return found

    def resolve(self, run_id, reference):
        source = next((r for r in self.records(run_id) if r["ref"]["source_id"] == reference), None)
        if source is None: raise RepoError("HISTORY_SCOPE_DENIED")
        self.repo.check([reference, source["ref"]["content_digest"]])
        return source

    def search(self, run_id, query, limit=10):
        query = query.casefold()
        return [{"reference": r["ref"]["source_id"], "digest": r["ref"]["content_digest"],
                 "preview": str(r["message"].get("content", ""))[:500]}
                for r in self.records(run_id) if query in str(r["message"]).casefold()][:min(limit, 20)]

    def expand(self, run_id, reference, start=0, length=12000):
        try:row = self.resolve(run_id, reference)
        except RepoError as exc:
            if exc.code!='HISTORY_SCOPE_DENIED':raise
            return self._expand_pinned(run_id,reference,start,length)
        text = json.dumps(row["message"], ensure_ascii=False)
        return {"reference": reference, "evidence_ref": row["ref"], "content": text[start:start+min(length, 16000)],
                "total_chars": len(text), "historical": True, "grants_edit_authority": False}

    def _expand_pinned(self,run_id,reference,start,length):
        run=self.runtime.run(run_id);m=run['data']['manifest'];source=self.store.read_json(m['knowledge_snapshot_digest'])
        self.repo.check([m['knowledge_snapshot_digest']])
        allowed={m['scope'],*(['user'] if run['data']['visibility']=='DEV' else [])}
        for entry in source['items']:
            if entry['scope'] not in allowed:continue
            item=self.store.read_json(entry['digest'])
            for ref in item.get('support_refs',[]):
                if ref['source_id']!=reference or ref['scope_id'] not in allowed or ref['visibility_domain']!='DEV':continue
                self.repo.check([entry['digest'],reference,ref['content_digest'],ref['run_id']])
                origin=self.runtime.run(ref['run_id'])
                if origin['data']['visibility']!='DEV' or origin['data']['manifest']['scope']!=ref['scope_id']:raise RepoError('HISTORY_SCOPE_DENIED')
                with contextlib.closing(self.store.connect()) as c:
                    if ref['source_kind'] in {'user_message','tool_result','model_message'} and reference.startswith('message:'):
                        row=c.execute('SELECT data FROM messages WHERE id=? AND run_id=?',(reference.split(':',1)[1],ref['run_id'])).fetchone()
                        value=semantic_message(json.loads(row[0])) if row else None
                    elif ref['source_kind']=='file_observation':
                        row=c.execute("SELECT result FROM operations WHERE id=? AND run_id=? AND state='SUCCEEDED'",(reference,ref['run_id'])).fetchone()
                        value=json.loads(row[0]) if row and row[0] else None
                    elif ref['source_kind']=='verification':
                        row=c.execute("SELECT data FROM events WHERE event_id=? AND run_id=? AND visibility='DEV'",(reference,ref['run_id'])).fetchone()
                        value=json.loads(row[0]) if row else None
                    else:value=None
                if value is None or digest(value)!=ref['content_digest']:raise RepoError('HISTORY_SOURCE_MISMATCH')
                selector=ref['selector']
                if selector['type']=='embedded_json_pointer':
                    value=json.loads(self._pointer(value,selector['container']))
                elif selector['type']!='json_pointer':raise RepoError('HISTORY_SELECTOR_INVALID')
                selected=self._pointer(value,selector['value'])
                text=json.dumps(clean_memory_data(selected),ensure_ascii=False)
                return {'reference':reference,'evidence_ref':ref,'content':text[start:start+min(length,16000)],'total_chars':len(text),
                        'historical':True,'grants_edit_authority':False,'authorization':'PINNED_EVIDENCE_SELECTOR_ONLY'}
        raise RepoError('HISTORY_SCOPE_DENIED')

    @staticmethod
    def _pointer(value,path):
        if path=='':return value
        if not path.startswith('/'):raise RepoError('HISTORY_SELECTOR_INVALID')
        try:
            for part in path[1:].split('/'):
                part=part.replace('~1','/').replace('~0','~')
                value=value[int(part)] if isinstance(value,list) and part.isdigit() else value[part]
            return value
        except (KeyError,IndexError,TypeError):raise RepoError('HISTORY_SELECTOR_INVALID') from None


class WorkingStateReducer:
    def __init__(self, repository, runtime):
        self.repo, self.runtime, self.store = repository, runtime, repository.store
        self.history = History(repository, runtime)

    def reduce(self, run_id, *, records=None, snapshot=None, ledger=None):
        run = self.runtime.run(run_id); m = run["data"]["manifest"]
        if records is None:
            records = self.history.records(run_id)
        current_source = snapshot if snapshot is not None else self.runtime.workspace(run).snapshot()
        plan = self.store.get("plan", run_id, default={"steps": []})
        turn=run['data'].get('active_turn') if m.get('conversation_mode') else None
        if turn and plan.get('turn_id')!=turn['turn_id']:plan={'steps':[]}
        observations = []; failures = []; files = []
        with contextlib.closing(self.store.connect()) as c:
            operations = c.execute("SELECT id,request,result FROM operations WHERE run_id=? AND result IS NOT NULL ORDER BY created", (run_id,)).fetchall()
        for row in operations:
            from ..work_status import full_result
            req = json.loads(row["request"]); result = full_result(self.store,run_id,json.loads(row["result"]))
            tool = req["tool_name"]; path = req["arguments"].get("path")
            if path: files.append(path)
            if tool in {"read", "edit", "create", "patch", "process.run", "process.status"}:
                observation = {"operation": row["id"], "tool": tool, "path": path,
                               "is_error": bool(result.get("is_error")), "exit_code": result.get("exit_code"),
                               "source": result.get("relevant_state"), "version": result.get("version"),
                               "versions":result.get('versions')}
                if tool.startswith("process"):
                    observed=result.get('output_snapshot') or result.get('relevant_state')
                    observation["validity"] = "CURRENT" if observed == current_source else "HISTORICAL"
                    observation["needs_reverification"] = observation["validity"] != "CURRENT"
                    observation['argv_summary']=str(redact_data(req['arguments'].get('argv',[])))[:240]
                    evidence = command_evidence(req['arguments'].get('argv',[]),result,req['arguments'].get('stdin',''),strict=True,store=self.store)
                    observation['verification'] = bool(evidence and evidence['verification'])
                    if evidence:
                        observation['evidence_kind'] = evidence['evidence_kind']
                        observation['outcome'] = evidence['outcome']
                        observation['scope'] = evidence['scope']
                    observation['result_summary']=redact_data(verification_summary(result))
                    if no_match(req['arguments'].get('argv',[]),result):
                        observation.update(is_error=False,outcome='NO_MATCH_NOT_PROCESS_FAILURE')
                observations.append(observation)
                if observation["is_error"]: failures.append({**observation, "causal_status": "UNCONFIRMED"})
        constraints = [{"id": r["ref"]["source_id"], "digest": r["ref"]["content_digest"]}
                       for r in records if r["message"]["role"] == "user"]
        loops = [{"id": digest([run_id, step]), "text": step, "status": "PROPOSED"} for step in plan["steps"]]
        if not loops:
            from ..work_status import work_status
            if ledger is None:
                ledger = work_status(self.store,self.runtime,run,snapshot=current_source)
            check=ledger['verification']
            if ledger['changed_path_count'] and (not check or check['validity']!='CURRENT' or check['outcome']!='PASSED' or not check['same_control_epoch']):
                probes = ledger['behavior_probes']
                if check and check['validity'] == 'CURRENT' and check['same_control_epoch']:
                    detail = 'Latest check of the current candidate: '+check['outcome'].lower()+'.'
                elif probes:
                    detail = 'Behavior was observed; a current assertion or test result has not been recorded.'
                else:
                    detail = 'No current assertion or test result is available for the candidate.'
                loops=[{'id':digest([run_id,current_source,'check-current-candidate']),
                        'text':detail+' Reuse the available evidence, then verify a missing behavior or state the limitation when handing off.',
                        'status':'PROPOSED','origin':'HOST_OBSERVATION_NOT_ACCEPTANCE_REQUIREMENT'}]
        secrets=self.history.known_secrets(run_id)
        def safe(text):
            text=semantic_text(text)
            for secret in secrets:text=text.replace(secret,'[REDACTED]')
            return text
        for loop in loops:loop['text']=safe(loop['text'])
        important=[o for o in observations if o.get('verification') and o.get('validity')=='CURRENT'][-1:]
        important+=[o for o in observations if o['tool'] in {'edit','create','patch'} and not o['is_error']][-2:]
        retained_observations={o['operation']:o for o in [*observations[-16:],*important]}
        view = {"schema_version": 2, "run_id": run_id, "covered_event_seq": records[-1]["seq"] if records else 0,
                "contract_ref": m["contract_digest"], "control_epoch": run["control_epoch"], "writer_epoch": run["writer_epoch"],
                "source_digest": current_source, "current_subgoal": safe(plan["steps"][0] if plan["steps"] else self.runtime.current_goal(run)[:1000]),
                'current_subgoal_source':'MODEL_PLAN_PROPOSAL' if plan['steps'] else 'ACTIVE_USER_REQUEST' if turn else 'ORIGINAL_TASK_GOAL',
                "host_observations": list(retained_observations.values()), "user_constraints": constraints,
                "open_loops": loops, "failed_attempts": failures[-8:], "active_files": list(dict.fromkeys(files))[-8:],
                "formal_run_state": run["state"], "completion_from_model": False}
        if turn:view.update(current_turn_id=turn['turn_id'],current_request=safe(turn['text']))
        # These originals are Host-owned input, not text reconstructed by a
        # summarizer. The initial goal and accepted directives are already in
        # the authoritative system section, so do not duplicate them here.
        view['accepted_user_material']=[{'source_id':r['ref']['source_id'],'text':safe(r['message']['content'])}
            for r in records if r['message']['role']=='user' and r['message'].get('content') not in [run['data']['goal'],*run['data']['directives'],*([turn['text']] if turn else [])]]
        key = digest(view)
        self.repo.put("working_state_versions", key, view)
        return key, view

    def intent(self, run_id, state_digest, state, query=None, *, records=None):
        run = self.runtime.run(run_id); m = run["data"]["manifest"]
        recent = (self.history.records(run_id) if records is None else records)[-8:]
        errors = [str(r["message"].get("content", ""))[:600] for r in recent if r["message"].get("is_error")]
        events=self.store.events(run_id,trusted=True,limit=10000)
        last_resume=max((e['sequence'] for e in events if e['type']=='run.resumed'),default=-1)
        last_model=max((e['sequence'] for e in events if e['type']=='model.completed'),default=-1)
        phase = ('DEBUG' if errors else 'RESUME' if last_resume>last_model else
                 'VERIFY' if state.get('current_subgoal_source')=='MODEL_PLAN_PROPOSAL' and re.search(r'(?i)\b(verify|verification|validate|test)\b|验收|验证|测试',state['current_subgoal']) else
                 'EDIT' if state['active_files'] else 'PLAN')
        semantic_query = query if query is not None else " ".join(dict.fromkeys([state.get('current_request',''),state["current_subgoal"], *state["active_files"], *errors]))[:2500]
        semantic_query=semantic_text(semantic_query)
        for secret in self.history.known_secrets(run_id):semantic_query=semantic_query.replace(secret,'[REDACTED]')
        return IntentFrame(run_id=run_id, task_revision=m["task_spec_revision"], contract_digest=m["contract_digest"],
                           phase=phase, current_subgoal=state["current_subgoal"], active_files=state["active_files"],
                           recent_error_codes=errors, authorized_scopes=[m["scope"], *(["user"] if run["data"]["visibility"] == "DEV" else [])],
                           visibility_domain=run["data"]["visibility"], knowledge_snapshot_digest=m["knowledge_snapshot_digest"],
                           index_bundle_digest=m["knowledge_index_digest"], working_state_revision=state_digest,
                           event_cutoff=state["covered_event_seq"], semantic_query=semantic_query,
                           environment={"environment_digest": m["environment_digest"]})
