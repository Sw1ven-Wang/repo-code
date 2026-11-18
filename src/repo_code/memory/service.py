"""Single asynchronous context preparation and memory admission boundary."""
from __future__ import annotations

import asyncio
import json
import time
import hashlib
from pathlib import Path
from ..util import RepoError, canonical, digest, uid
from ..context import SYSTEM, estimate_tokens, groups, task_authority_context
from .models import MemoryProfile, RequestContextManifest, ProviderEnvelope
from .repository import MemoryRepository
from .working_state import History, WorkingStateReducer
from .compaction import SemanticCompactor
from .retrieval import DynamicRetriever, build_bundle
from .jobs import MemoryJobs
from .consolidation import Consolidator
from .encoders import LocalEncoder
from .budget import ContextBudgetCoordinator
from dataclasses import asdict
from ..commands import command_evidence


def memory_implementation_digest():
    package=Path(__file__).resolve().parent
    files=package.parent.rglob('*.py')
    return digest({path.relative_to(package.parent).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)})


class MemoryService:
    def __init__(self, store, runtime, provider, profile, encoder=None):
        self.store, self.runtime, self.provider = store, runtime, provider
        self.profile = profile if isinstance(profile, MemoryProfile) else MemoryProfile.model_validate(profile)
        implementation=memory_implementation_digest()
        if self.profile.implementation_digest not in {None,implementation}:raise RepoError('MEMORY_IMPLEMENTATION_CHANGED','Create a new Run/profile with the current implementation; historical pins remain readable')
        self.profile=self.profile.model_copy(update={'implementation_digest':implementation})
        with store.tx() as c:
            from ..models import TERMINAL
            for row in c.execute('SELECT state,data FROM runs'):
                data=json.loads(row['data'])
                if data['manifest'].get('memory_schema_version')!=2 and row['state'] not in TERMINAL|{'INTERRUPTED','NEEDS_RECONCILIATION','BLOCKED'}:
                    raise RepoError('MEMORY_UPGRADE_REQUIRES_STOPPED_LEGACY_RUNS')
            c.execute('PRAGMA user_version=17')
        self.repo = MemoryRepository(store)
        self.owns_encoder=encoder is None
        self.encoder = encoder if encoder is not None else (LocalEncoder(self.profile.encoder) if self.profile.mode == "hybrid_semantic_strict" else None)
        self.reducer = WorkingStateReducer(self.repo, runtime)
        self.history = History(self.repo, runtime)
        self.compactor = SemanticCompactor(self.repo, runtime, provider, self.profile)
        self.retriever = DynamicRetriever(self.repo, self.profile, self.encoder)
        self.jobs = MemoryJobs(self.repo, self.profile.max_jobs)
        self.consolidator = Consolidator(self.repo, runtime, self.profile)
        self.consolidator.retriever=self.retriever
        self.profile_digest = store.json_blob(self.profile)

    def pin(self, scope, visibility="DEV"):
        self.assert_enabled()
        snapshot, index = build_bundle(self.repo, scope, self.profile, self.encoder, visibility)
        head_key = scope if visibility=='DEV' else scope+":"+visibility
        self.repo.publish(head_key, snapshot, index, self.repo.get("snapshot_heads", head_key))
        return snapshot, index

    def assert_enabled(self):
        if self.store.get('operator_config','disabled-memory-profile:'+self.profile_digest):raise RepoError('MEMORY_PROFILE_DISABLED')

    def disable(self,reason):
        if not reason:raise RepoError('DISABLE_REASON_REQUIRED')
        self.store.put('operator_config','disabled-memory-profile:'+self.profile_digest,{'reason':reason,'default':'legacy_v1','profile':self.profile_digest})

    def assert_run_profile(self,run_id):
        self.assert_enabled()
        if self.runtime.run(run_id)['data']['manifest'].get('memory_profile_digest')!=self.profile_digest:raise RepoError('RUN_MEMORY_CONFIGURATION_CHANGED')

    def serialized(self, system, messages, tools):
        if hasattr(self.provider, "build_request"): return self.provider.build_request(system, messages, tools)[0]
        return {"system": system, "messages": messages, "tools": tools}

    def file_versions(self, run_id):
        return {path:item["blob"] for path,item in self.runtime.workspace(self.runtime.run(run_id)).manifest()["files"].items()}

    async def search_capabilities(self,run_id,query,*,phase='DEBUG',environment=None,facts=None,limit=2,token_budget=None):
        """R4 approved-skill retrieval only; R5 owns offering/application receipts."""
        self.assert_run_profile(run_id)
        return await asyncio.to_thread(self.consolidator.retrieve_experiences,query,
            purpose='runtime',run_id=run_id,phase=phase,environment=environment,facts=facts,
            file_versions=self.file_versions(run_id),limit=limit,token_budget=token_budget)

    async def search(self, run_id, query=None, limit=6, token_budget=None, *, prepared=None):
        self.assert_run_profile(run_id)
        if prepared is None:
            key, state = self.reducer.reduce(run_id)
            records, versions = None, self.file_versions(run_id)
        else:
            key, state, records, versions = prepared
        intent = self.reducer.intent(run_id, key, state, query, records=records)
        return await asyncio.to_thread(self.retriever.retrieve, intent, versions, limit, token_budget)

    def admission(self, manifest):
        self.assert_enabled()
        with self.store.tx() as c:
            run = self.runtime.run(manifest["run_id"], c)
            if run["state"] != "RUNNING" or run["cancel_requested"]: raise RepoError("RUN_NOT_EXECUTABLE")
            if c.execute("SELECT 1 FROM control WHERE run_id=? AND status LIKE 'PENDING%'", (run["id"],)).fetchone(): raise RepoError("CONTROL_PENDING")
            if (run["control_epoch"],run["writer_epoch"],self.repo.epoch(c),digest(run["data"].get("authorization",{}))) != (
                    manifest["control_epoch"],manifest["writer_epoch"],manifest["revocation_epoch"],manifest["authorization_digest"]):
                raise RepoError("MEMORY_REQUEST_STALE")
            self.repo.check([manifest["knowledge_snapshot_digest"],manifest["retrieval_trace_digest"],manifest["protocol_envelope_ref"],*manifest["summary_node_ids"]], c)
            if 'projection_epoch_generation' in manifest:
                pointer=self.store.get('active_projection_epoch',run['id'],c)
                expected=({'id':manifest['projection_epoch_id'],'generation':manifest['projection_epoch_generation']}
                    if manifest.get('projection_epoch_id') else None)
                if pointer!=expected:raise RepoError('EPOCH_STALE')

    def response_current(self, manifest):
        try: self.admission(manifest); return True
        except RepoError: return False

    @staticmethod
    def _working_groups(complete, versions, source_digest, limit, store=None):
        """Choose bounded, current work evidence, never user messages/authority.

        Keep two distinct read ranges so reading the second page does not evict
        the first. A newer file version invalidates old reads and write payloads.
        Returned indices refer to whole immutable protocol groups.
        """
        eligible=[];reads=[];writes=[];processes=[];validations=[];seen={}
        for index in range(len(complete)-1,-1,-1):
            group=complete[index];calls={c['id']:c for m in group for c in m.get('tool_calls',[])}
            if not calls:continue
            current=False;read_spans=[];write=False;process=False;validation=False;stale=False
            for message in group:
                call=calls.get(message.get('tool_call_id'))
                if not call:continue
                try:body=json.loads(message.get('content',''))
                except (ValueError,TypeError):body={}
                if not isinstance(body,dict):body={}
                name=call['name'];args=call.get('arguments',{})
                if body.get('executed') is False or (body.get('is_error') and name!='process.run'):continue
                if name=='read':
                    path=args.get('path');version=body.get('version')
                    if version and versions.get(path)!=version:stale=True;continue
                    if version:
                        start=args.get('start',1);end=min(start+args.get('lines',200)-1,body.get('total_lines',start+args.get('lines',200)-1))
                        read_spans.append((path,version,start,end))
                elif name in {'edit','create','patch','delete','move'}:
                    observed=body.get('versions',{})
                    if observed and any(versions.get(p)!=v for p,v in observed.items()):stale=True;continue
                    write=True
                elif name=='process.run':
                    observed=body.get('output_snapshot') or body.get('relevant_state')
                    if observed and observed!=source_digest:stale=True;continue
                    if body.get('running') or body.get('cancelled') or body.get('timeout'):continue
                    process=True
                    evidence = command_evidence(args.get('argv',[]),body,args.get('stdin',''),strict=True,store=store)
                    if evidence and evidence['verification'] and evidence['outcome'] != 'UNCERTAIN':
                        validation=True
                current=True
            if not current or stale:continue
            eligible.append(index)
            novel=False
            for path,version,start,end in read_spans:
                ranges=seen.setdefault((path,version),[])
                if end>=start and not any(a<=start and b>=end for a,b in ranges):
                    novel=True;ranges.append((start,end))
            if novel:reads.append(index)
            if write:writes.append(index)
            if process:processes.append(index)
            if validation:validations.append(index)
        # A newer git/ls process is not a replacement for the last check of
        # this same candidate. Reserve its slot before ordinary process output.
        ordered=validations[:1]+writes[:1]+reads[:2]+([eligible[0]] if eligible else [])+processes[:1]
        return list(dict.fromkeys(ordered))[:limit]

    async def prepare_context(self, run_id, tools, approved_guidance="", policy=None,memory_policy=None):
        started = previous = time.perf_counter()
        stages = {}
        counts = {}

        def mark(stage):
            nonlocal previous
            current = time.perf_counter()
            stages[stage] = stages.get(stage,0.0) + current - previous
            previous = current

        try:
            return await self._prepare_context(run_id,tools,approved_guidance,policy,memory_policy,mark,counts)
        finally:
            mark("finalize")
            run = self.runtime.run(run_id)
            with self.store.tx() as c:
                self.store.emit(c,run_id,"host.context_prepared",
                                {"seconds":previous-started,"stages_seconds":stages,"counts":counts},
                                run["data"]["visibility"])

    async def _prepare_context(self, run_id, tools, approved_guidance, policy, memory_policy, mark, counts):
        self.assert_enabled()
        run = self.runtime.run(run_id); m = run["data"]["manifest"]
        if m.get("memory_profile_digest") != self.profile_digest: raise RepoError("RUN_MEMORY_CONFIGURATION_CHANGED")
        self.runtime.assert_executable(run)
        self.runtime.check_wall(run_id,"prepare")
        await self.on_correction(run_id)
        run = self.runtime.run(run_id)
        fence = (run["control_epoch"],run["writer_epoch"],self.repo.epoch(),
                 digest(run["data"].get("authorization",{})))
        mark("admission_and_correction")

        def check_fence():
            fresh = self.runtime.run(run_id)
            self.runtime.assert_executable(fresh)
            current = (fresh["control_epoch"],fresh["writer_epoch"],self.repo.epoch(),
                       digest(fresh["data"].get("authorization",{})))
            if current != fence:
                raise RepoError("MEMORY_REQUEST_STALE")

        records = self.history.records(run_id)
        counts["history_rows"] = len(records)
        mark("history")
        from ..workspace import Workspace
        workspace = self.runtime.workspace(run)
        workspace_manifest = workspace.manifest(stats=counts) if isinstance(workspace,Workspace) else workspace.manifest()
        files = workspace_manifest["files"]
        snapshot = self.store.json_blob(workspace_manifest)
        versions = {path:item["blob"] for path,item in files.items()}
        counts["preparation_worktree_files"] = len(files)
        counts["preparation_worktree_scans"] = 1
        mark("worktree")
        from ..work_status import work_status
        ledger = work_status(self.store,self.runtime,run,snapshot=snapshot,files=files)
        state_key, state = self.reducer.reduce(run_id,records=records,snapshot=snapshot,ledger=ledger)
        mark("working_state_and_ledger")
        selection=memory_policy or {}
        count=selection.get('top_k',6);tokens=selection.get('token_budget',self.profile.evidence_tokens)
        if isinstance(count,bool) or not isinstance(count,int) or isinstance(tokens,bool) or not isinstance(tokens,int):raise RepoError('INVALID_MEMORY_POLICY')
        count=min(10,max(0,count));tokens=min(self.profile.evidence_tokens,max(100,tokens))
        self.runtime.check_wall(run_id,"prepare")
        hits, trace_id = await self.search(run_id,limit=count,token_budget=tokens,
                                           prepared=(state_key,state,records,versions))
        check_fence()
        mark("retrieval")
        system = (SYSTEM+"\nTask goal: "+run["data"]["goal"]+task_authority_context(self.runtime,run)
                  +"\nExecution directives: "+json.dumps(run["data"]["directives"],ensure_ascii=False))
        from ..progress import coalesce_process_observations, runtime_feedback, latest_process_group
        runtime_status=runtime_feedback(self.store,self.runtime,run,self.provider,ledger=ledger)
        if not m.get('context_compaction'):
            system+='\nHost runtime status (not task acceptance): '+json.dumps(runtime_status,ensure_ascii=False)
        system+=approved_guidance
        if (m.get('context_compaction') or {}).get('mode')=='live-experimental-v2':
            return self._epoch_context(run,records,state_key,state,hits,trace_id,tools,system,
                runtime_status,approved_guidance,policy or {},fence)
        raw = coalesce_process_observations(self.store.messages(run_id))
        if raw and raw[0].get('role') == 'user' and raw[0].get('content') == run["data"]["goal"]:
            raw[0] = {**raw[0],'content':'Original task goal is provided in the system instructions.'}
        complete = groups(raw)
        latest_process_index=latest_process_group(complete)
        max_tokens = min(self.profile.max_tokens, (policy or {}).get("max_tokens",self.profile.max_tokens))
        reserve = max(self.profile.reserve_tokens,(policy or {}).get("reserve_tokens",0),getattr(self.provider,'max_tokens',None) or 0)
        available = max_tokens-reserve-self.profile.safety_tokens
        coordinator=ContextBudgetCoordinator(self.provider,max_tokens,reserve,self.profile.safety_tokens,m['model_config_digest'])
        coordinator.serialized=self.serialized
        budget_attempts=[]
        external_artifacts=[];registered_expansions=set()
        def externalize(message,record,allowance,preview_cap=1500):
            message=dict(message)
            if message['role']!='tool' or estimate_tokens(message.get('content',''))<=allowance:return message
            content=message['content']
            try:body=json.loads(content)
            except (ValueError,TypeError):body={}
            original=next((body.get(k) for k in ['source_artifact','artifact','result_artifact'] if isinstance(body,dict) and isinstance(body.get(k),str) and self.store.get('run_artifact',digest([run_id,body[k]]))),None)
            ref=original or self.store.blob(content)
            metadata={k:body[k] for k in ['path','version','versions','exit_code','is_error','code','relevant_state'] if isinstance(body,dict) and body.get(k) is not None}
            preview_source=next((body.get(k) for k in ['content','preview'] if isinstance(body,dict) and isinstance(body.get(k),str)),content)
            streams=[(key,body[key]) for key in ['stdout','stderr'] if isinstance(body,dict) and isinstance(body.get(key),str)]
            limit=max(64,allowance*3)
            if preview_cap is not None:limit=min(preview_cap,limit)
            def excerpt(value,chars):
                if len(value)<=chars:return value
                half=max(1,chars//2)
                return value[:half]+'\n[…omitted…]\n'+value[-half:]
            def visible(chars):
                if streams:return '\n'.join(key+':\n'+excerpt(value,max(8,chars//len(streams))) for key,value in streams)
                return excerpt(preview_source,chars)
            preview=visible(limit)
            while limit>16 and estimate_tokens(preview)>max(24,allowance-100):
                limit=max(16,limit//2);preview=visible(limit)
            projected=json.dumps({'bounded_preview':preview,'artifact':ref,'bytes':len(self.store.read_blob(ref)),
                **({k:body[k] for k in ['offset','next_offset','returned_bytes'] if k in body} if isinstance(body,dict) else {}),
                'observed_metadata':metadata,'retrieve_with':'artifact.read'},ensure_ascii=False)
            if estimate_tokens(projected)>=estimate_tokens(content):return message
            registration=(ref,record['ref']['content_digest'])
            if registration not in registered_expansions:
                self.repo.check([ref])
                self.store.put('run_artifact',digest([run_id,ref]),{'run_id':run_id,'artifact':ref})
                with self.store.tx() as c:
                    self.repo.derive(record['ref']['content_digest'],[record['ref']['source_id'],*record['artifact_dependencies']],c)
                    if ref not in record['artifact_dependencies']:self.repo.derive(ref,[record['ref']['content_digest']],c)
                    else:self.repo.derive(ref,[record['ref']['source_id']],c)
                registered_expansions.add(registration)
            external_artifacts.append(ref);message['content']=projected
            return message
        # Allocate a whole tool group, not a full per-tool allowance for every
        # result in a parallel call. The raw group remains immutable in history.
        group_budget=min(self.profile.evidence_tokens,max(512,(available-estimate_tokens(self.serialized(system,[],tools))-estimate_tokens(state))//2))
        latest_group_budget=max(group_budget,min(12000,max(512,(available-estimate_tokens(self.serialized(system,[],tools))-estimate_tokens(state))//2)))
        # Bound a huge output without splitting its complete protocol group.
        bounded = []
        position=0
        for group_index,group in enumerate(complete):
            self.runtime.check_wall(run_id,"prepare")
            projected = []
            tool_count=sum(m['role']=='tool' for m in group)
            allocation=latest_group_budget if group_index in {len(complete)-1,latest_process_index} else group_budget
            allowance=max(192,allocation//max(1,tool_count))
            for message in group:
                projected.append(externalize(message,records[position],allowance));position+=1
            bounded.append(projected)
        retained = list(bounded); covered = 0; summary_ids = []; summary = None
        old_head = self.repo.get("memory_control","summary:"+run_id)
        if old_head:
            self.repo.check([old_head["id"]])
            summary = self.repo.get("summary_nodes",old_head["id"]); summary_ids=[old_head["id"]]
            if summary is None:raise RepoError('SUMMARY_HEAD_MISSING')
            for group in complete:
                if records[covered+len(group)-1]["seq"] <= old_head["cutoff"]: covered += len(group)
                else: break
            retained = bounded[len(groups(raw[:covered])):]

        offsets=[0]
        for group in complete:offsets.append(offsets[-1]+len(group))
        segment_ranges=[];semantic_floor=len(groups(raw[:covered]))-1
        def load_segments():
            nonlocal segment_ranges,semantic_floor,summary_ids
            overlays,segment_ranges=self.compactor.segment_projection(run_id,records,complete,state,versions)
            semantic_floor=max([len(groups(raw[:covered]))-1,*[h['group_end'] for h in segment_ranges]])
            summary_ids=([summary_ids[0]] if summary else [])+[h['id'] for h in segment_ranges]
            for i in range(len(groups(raw[:covered]))):bounded[i]=[]
            for i,view in overlays.items():bounded[i]=view
        if m.get('context_compaction'):
            load_segments();retained=bounded[len(groups(raw[:covered])):]
        mark("initial_projection")
        observation_budget=min(1000,self.profile.evidence_tokens//2)
        # A small observation floor prevents paying for FACT/EPISODE extraction
        # then projecting every such claim away under ordinary pressure. It is
        # bounded, carries provenance/validity, and cannot replace current work.
        claim_floor=min(320,max(100,self.profile.evidence_tokens//8))
        claim_projection={}
        reconciled_claims={}
        evidence_overlay={}
        def compose():
            summary_view=None
            if summary:
                required=[c for c in summary['claims'] if c['kind'] in {'DECISION','USER_PREFERENCE','HYPOTHESIS','PROCEDURE'}]
                optional=[c for c in summary['claims'] if c not in required]
                active=set(state['active_files'])
                ranked=sorted(reversed(optional),key=lambda c:not bool(active & set(c.get('source_version',{}))))
                floor=[];floor_used=0
                for claim in ranked:
                    cost=estimate_tokens([claim['text'],[r['source_id'] for r in claim['support_refs']]])+70
                    if floor_used+cost<=claim_floor:
                        floor.append(claim);floor_used+=cost
                    if len(floor)>=2:break
                selected=[*required,*floor];used=0;omitted=[]
                for claim in reversed(optional):
                    if claim in floor:continue
                    cost=estimate_tokens([claim['text'],[r['source_id'] for r in claim['support_refs']]])+70
                    if used+cost>observation_budget:omitted.append(claim['claim_id']);continue
                    selected.append(claim);used+=cost
                for claim in selected:
                    key = (summary_ids[0],claim["claim_id"])
                    if key not in reconciled_claims:
                        reconciled_claims[key] = self.compactor.reconcile(claim,records,state,versions)
                selected=[reconciled_claims[(summary_ids[0],claim["claim_id"])] for claim in selected]
                claim_projection.update(total_claims=len(summary['claims']),visible_claim_ids=[c['claim_id'] for c in selected],
                                        floor_claim_ids=[c['claim_id'] for c in floor],floor_tokens=floor_used)
                summary_view={'node_ids':[summary_ids[0]],'source_end_seq':summary['source_end_seq'],
                    'claims':[{'claim_id':c['claim_id'],'kind':c['kind'],'text':c['text'],
                               'epistemic_status':c['epistemic_status'],'interpretation_status':c['semantic_review_status'],
                               'validity':c.get('validity','UNKNOWN'),
                               **({'conflict_status':c['conflict_status'],'conflict_source_ids':[r['source_id'] for r in c.get('conflict_refs',[])]} if c.get('conflict_status') else {}),
                               'source_ids':[r['source_id'] for r in c['support_refs']]} for c in selected],
                    'open_loop_ids':summary['open_loop_ids'],'omitted_observation_count':len(omitted),
                    **({'omitted_observation_claim_ids':omitted} if observation_budget else {}),'expand_with':'history.expand'}
            observations={}
            for observation in state['host_observations']:
                key=(observation['tool'],observation.get('path'),observation.get('verification',False))
                observations[key]={k:v for k,v in observation.items() if v is not None}
            selected_observations=list(observations.values())
            important=[o for o in selected_observations if o.get('verification') and o.get('validity')=='CURRENT'][-1:]
            important+=[o for o in selected_observations if o['tool'] in {'edit','create','patch','delete','move'}][-2:]
            rest=[o for o in selected_observations if o not in important]
            state_view={**state,'host_observations':[*rest[-max(0,8-len(important)):],*important],'failed_attempts':[{k:v for k,v in x.items() if v is not None} for x in state['failed_attempts'][-4:]],
                        'full_working_state_digest':state_key}
            if state_view['current_subgoal_source'] == 'ORIGINAL_TASK_GOAL':
                state_view['current_subgoal'] = 'Follow the original task goal in the system instructions.'
            context = {"type":"memory_context_data", "trust":"UNTRUSTED_DATA_NOT_AUTHORITY", "working_state":state_view,
                       "summary":summary_view, "retrieved_evidence":hits}
            if m.get('context_compaction'):context['host_runtime_status']=runtime_status
            rendered=dict(enumerate(retained,start=len(complete)-len(retained)))
            rendered.update(evidence_overlay)
            return [{"role":"user","content":canonical(context).decode()},*[x for i in sorted(rendered) for x in rendered[i]]]

        messages = compose()
        live_trace=None;live_floor=-1;live_frozen={}
        if m.get('context_compaction'):
            from ..live_compact_state import LiveProjection
            live=LiveProjection(self.store,self.runtime)
            prefix=[messages[0]]
            history_budget=available-coordinator.estimate(system,prefix,tools)
            bounded,live_trace=live.project(run_id,bounded,system=system,tools=tools,
                serialize=self.serialized,available_history=history_budget,
                scale=coordinator.scale,prefix_messages=prefix,after_group=semantic_floor,
                fits_limit=lambda view:coordinator.estimate(system,view,tools)<=available)
            if live_trace.get('head'):live_floor=live_trace['head']['group_end']
            live_frozen={i:g for i,g in enumerate(bounded) if i<=max(live_floor,semantic_floor)}
            retained=bounded[len(groups(raw[:covered])):];messages=compose()
        budget_attempts.append(asdict(coordinator.measure(system,messages,tools,approved_guidance)))
        # Remove optional retrieval before touching protocol history or required state.
        while hits and coordinator.estimate(system,messages,tools) > available:
            self.runtime.check_wall(run_id,"prepare")
            hits.pop(); messages=compose()
        # Historical observations are optional, unlike decisions/open loops and
        # the uncompressed delta. Reclaim this allocation before another paid
        # extraction or fallback. The full node stays addressable by source ID.
        if coordinator.estimate(system,messages,tools)>available:
            observation_budget=0;messages=compose()
        if coordinator.estimate(system,messages,tools)>available:
            # A second, bounded preview allocation reclaims low-priority tool
            # expansions before paying for another semantic extraction.
            position=0;bounded=[]
            for group_index,group in enumerate(complete):
                self.runtime.check_wall(run_id,"prepare")
                if group_index in live_frozen:
                    bounded.append(live_frozen[group_index]);position+=len(group);continue
                projected=[]
                for message in group:
                    # Keep the latest complete observation at the normal allowance.
                    # Reclaim older expansions before stripping current evidence.
                    allowance=max(192,latest_group_budget//max(1,sum(m['role']=='tool' for m in group))) if group_index in {len(complete)-1,latest_process_index} else 160
                    projected.append(externalize(message,records[position],allowance));position+=1
                bounded.append(projected)
            retained=bounded[len(groups(raw[:covered])):];messages=compose()
        need = coordinator.estimate(system,messages,tools) > available
        from ..providers import model_configuration
        native=self.provider;seen=set()
        while hasattr(native,'inner') and id(native) not in seen:seen.add(id(native));native=native.inner
        verified_rebase=(self.profile.protocol_rebase_verified and (m['model_config_digest'] in self.profile.protocol_rebase_profile_digests or digest(model_configuration(native)) in self.profile.protocol_rebase_profile_digests)
                         and digest(tools) in self.profile.protocol_rebase_tool_digests)
        if need and live_floor>=0:
            # Semantic extraction owns only the raw tail after the latest live
            # checkpoint. Its own CAS head can advance until the next live cut.
            opaque=any(message.get('reasoning_content') is not None or message.get('native_blocks') for message in raw)
            if opaque and not verified_rebase:raise RepoError('PROTOCOL_REBASE_UNVERIFIED')
            remaining_count=min(self.profile.recent_groups,len(complete)-max(live_floor,semantic_floor)-1)
            fixed=[m for g in bounded[:live_floor+1] for m in g]
            while remaining_count>1:
                tail=[m for g in bounded[-remaining_count:] for m in g]
                if coordinator.estimate(system,[compose()[0],*fixed,*tail],tools)+1200<=available:break
                remaining_count-=1
            target=len(complete)-max(1,remaining_count)-1
            if target<=max(live_floor,semantic_floor):
                raise RepoError('CONTEXT_BUDGET_EXCEEDED','committed prefix, latest group and required state exceed budget')
            live.assert_current(run_id,live_trace)
            try:
                await self.compactor.compact(run_id,records[offsets[target+1]-1]['seq'],state,
                    after_seq=records[offsets[live_floor+1]-1]['seq'])
            finally:
                mark('auxiliary_total')
            check_fence()
            fresh=live.fence(run_id)
            ignored={'revision','summary_heads'}
            if ({k:v for k,v in fresh.items() if k not in ignored}
                    != {k:v for k,v in live_trace['fence'].items() if k not in ignored}):
                raise RepoError('LIVE_COMPACT_STALE')
            live_trace['fence']=fresh
            load_segments();retained=bounded[len(groups(raw[:covered])):]
            messages=compose();need=False
        if need:
            opaque = any(message.get("reasoning_content") is not None or any(b.get("type") not in {"text","tool_use"} for b in message.get("native_blocks",[])) for message in raw)
            if opaque and not verified_rebase: raise RepoError("PROTOCOL_REBASE_UNVERIFIED")
            # Only closed groups are removed; an open group raises before this point.
            remaining_count = max(1,min(self.profile.recent_groups,len(retained)-1))
            # A recent-group preference is a soft allocation. Keep fewer whole
            # groups when necessary, reserving room for the generated summary.
            while remaining_count>1:
                candidate=[x for g in retained[-remaining_count:] for x in g]
                if coordinator.estimate(system,[{'role':'user','content':canonical(state).decode()},*candidate],tools)+1200<=available:break
                remaining_count-=1
            latest=[x for g in retained[-remaining_count:] for x in g]
            if verified_rebase and coordinator.estimate(system,[{'role':'user','content':canonical(state).decode()},*latest],tools)+1600>available:
                remaining_count=0
            folded=retained[:-remaining_count] if remaining_count else retained
            for group in folded:covered+=len(group)
            retained=retained[-remaining_count:] if remaining_count else []
            cutoff = records[covered-1]["seq"] if covered else 0
            if cutoff <= 0:
                coordinator.assert_fits(system,messages,tools,approved_guidance)
                raise RepoError("CONTEXT_BUDGET_EXCEEDED","last complete group and required state exceed budget")
            self.runtime.check_wall(run_id,"auxiliary_prepare")
            mark("initial_packing")
            try:
                node_id, summary = await self.compactor.compact(run_id,cutoff,state)
                summary_ids=[node_id]
            except (RepoError, ValueError, TimeoutError) as exc:
                from ..util import ProviderNotDispatched
                if isinstance(exc,ProviderNotDispatched) or getattr(exc,'code',None) in {'BUDGET_EXHAUSTED','TASK_TOTAL_BUDGET','MEMORY_TOKEN_BUDGET','MEMORY_AUXILIARY_BUDGET'}:raise
                self.repo.put("memory_control",uid("compaction-failure-"),{"run_id":run_id,"code":getattr(exc,"code",type(exc).__name__),"fallback":"PREVIOUS_LEGAL_PROJECTION_OR_STOP"})
                if not old_head: raise RepoError("CONTEXT_COMPACTION_FAILED",getattr(exc,"code",type(exc).__name__)) from None
                # Never drop a delta which the retained old summary does not cover.
                summary = self.repo.get("summary_nodes",old_head["id"]); summary_ids=[old_head["id"]]
                covered = sum(r["seq"]<=old_head["cutoff"] for r in records)
                retained = bounded[len(groups(raw[:covered])):]
            finally:
                mark("auxiliary_total")
            check_fence()
            if live_trace:
                # The prefix-only semantic path is possible before the first
                # live cut. Subsequent local cuts start after its raw cutoff.
                fresh=live.fence(run_id)
                ignored={'revision','summary_heads'}
                if ({k:v for k,v in fresh.items() if k not in ignored}
                        != {k:v for k,v in live_trace['fence'].items() if k not in ignored}):
                    raise RepoError('LIVE_COMPACT_STALE')
                live_trace['fence']=fresh
            messages=compose()
        mark("initial_packing")
        # A summary is smaller only after extraction. Reuse the final budget to
        # restore working evidence, including groups covered by an older head.
        # This is a projection overlay: no summary cutoff/raw row is rewritten.
        working=(self._working_groups(complete,versions,state['source_digest'],self.profile.recent_groups,self.store)
                 if coordinator.estimate(system,messages,tools)<=available else [])
        offsets=[];position=0
        for group in complete:offsets.append(position);position+=len(group)
        working_trace=[];working_fill_attempts=0
        for index in working:
            if m.get('context_compaction') and index<=max(live_floor,semantic_floor):
                working_trace.append({'group_index':index,'status':'LIVE_CHECKPOINT_FROZEN'});continue
            group=complete[index];is_covered=index<len(complete)-len(retained)
            opaque=any(message.get('reasoning_content') is not None or any(b.get('type') not in {'text','tool_use'} for b in message.get('native_blocks',[])) for message in group)
            if is_covered and opaque and not verified_rebase:
                working_trace.append({'group_index':index,'status':'OPAQUE_REBASE_UNVERIFIED'});continue
            previous_overlay=dict(evidence_overlay);previous_retained=list(retained)
            previous_hits=list(hits);previous_observations=observation_budget
            def fits(candidate):
                nonlocal working_fill_attempts
                working_fill_attempts+=1
                counts["fill_attempts"] = working_fill_attempts
                self.runtime.check_wall(run_id,"prepare")
                evidence_overlay[index]=candidate
                return coordinator.estimate(system,compose(),tools)<=available
            accepted=group if fits(group) else None
            if accepted is None:
                # Work evidence takes priority over optional hits/old excerpts.
                hits=[];observation_budget=0
                start=len(complete)-len(retained)
                retained=[g if i<=max(live_floor,semantic_floor) or i in working or i in evidence_overlay else
                    [externalize(message,records[offsets[i]+j],160) for j,message in enumerate(complete[i])]
                    for i,g in enumerate(retained,start=start)]
                if fits(group):accepted=group
            if accepted is None:
                # Maximize the visible body using actual native request costs,
                # rather than capping active reads to a fixed 1,500 characters.
                low=192;high=available;count=max(1,sum(x['role']=='tool' for x in group))
                while low<=high:
                    allocation=(low+high)//2
                    candidate=[externalize(message,records[offsets[index]+j],max(192,allocation//count),preview_cap=None)
                               for j,message in enumerate(group)]
                    if fits(candidate):accepted=candidate;low=allocation+1
                    else:high=allocation-1
            if accepted is None:
                evidence_overlay=previous_overlay;retained=previous_retained
                hits=previous_hits;observation_budget=previous_observations
                working_trace.append({'group_index':index,'status':'NO_LEGAL_SPACE'});continue
            evidence_overlay[index]=accepted
            working_trace.append({'group_index':index,'status':'FULL' if accepted==group else 'BOUNDED',
                                  'covered_by_summary':is_covered,
                                  'tool_call_ids':[c['id'] for x in group for c in x.get('tool_calls',[])]})
        mark("evidence_refill")
        self.runtime.check_wall(run_id,"prepare")
        messages=compose()
        groups(messages)
        request = self.serialized(system,messages,tools)
        breakdown=coordinator.measure(system,messages,tools,approved_guidance)
        estimated=breakdown.estimated_input_total
        budget_attempts.append(asdict(breakdown))
        self.repo.put('memory_control',uid('request-budget-'),{'run_id':run_id,'attempts':budget_attempts,
            'max_compaction_calls':1,'max_repack_attempts':2,'accepted':breakdown.within_limit(),'estimate_only':True,
            'working_evidence':working_trace,'working_fill_attempts':working_fill_attempts,
            'claim_projection':claim_projection,'request_digest':digest(request)})
        if not breakdown.within_limit():coordinator.assert_fits(system,messages,tools,approved_guidance)
        mark("serialize_and_budget")
        self.runtime.check_wall(run_id,"prepare")
        # A provider-native envelope is private protocol storage, never source material for learning.
        envelope = ProviderEnvelope(profile_digest=digest(run["data"].get("model_config",{})),messages=messages,
                                    rebase_verified=self.profile.protocol_rebase_verified)
        envelope_ref=self.store.json_blob(envelope)
        with self.store.tx() as c:
            for r in records:self.repo.derive(r["ref"]["content_digest"],[r["ref"]["source_id"],*r['artifact_dependencies']],c)
            self.repo.derive(envelope_ref,[trace_id,*summary_ids,*external_artifacts,*[r["ref"]["content_digest"] for r in records]],c)
        manifest=RequestContextManifest(run_id=run_id,turn_request_id=uid("request-"),release_digest=m["release_digest"],
            model_config_digest=m["model_config_digest"],provider_protocol_profile_digest=envelope.profile_digest,
            knowledge_snapshot_digest=m["knowledge_snapshot_digest"],knowledge_index_digest=m["knowledge_index_digest"],
            working_state_digest=state_key,summary_node_ids=summary_ids,summary_cutoff=records[covered-1]["seq"] if covered else 0,
            summary_segments=segment_ranges,
            retrieval_trace_digest=trace_id,selected_memory_ids_versions=[{"id":h["id"],"version":h["version"]} for h in hits],
            tool_schema_digest=digest(tools),control_epoch=run["control_epoch"],writer_epoch=run["writer_epoch"],
            revocation_epoch=fence[2],authorization_digest=fence[3],
            visibility_domain=run["data"]["visibility"],estimated_input_tokens=estimated,reserved_output_tokens=reserve,
            serialized_request_digest=digest(request),protocol_envelope_ref=envelope_ref,mode=self.profile.mode,
            budget_breakdown=asdict(breakdown),packing_attempts=len(budget_attempts),estimate_method='native-unicode-weighted/scale-'+str(coordinator.scale)).model_dump()
        self.admission(manifest)
        request_ref=self.store.json_blob(request)
        with self.store.tx() as c:self.repo.derive(request_ref,[envelope_ref],c)
        self.repo.put("context_requests",manifest["turn_request_id"],{"manifest":manifest,"envelope_ref":envelope_ref,"request_ref":request_ref,"usage":{},'usage_status':'PENDING'})
        self.store.put("projection",run_id,{"schema_version":2,"manifest":manifest,"estimated_tokens":estimated,"estimate_only":True,'working_evidence':working_trace})
        if live_trace:
            projection=self.store.get('projection',run_id);projection['live_compact']=live_trace
            self.store.put('projection',run_id,projection)
            live.assert_current(run_id,live_trace)
        mark("commit_projection")
        return system,messages,manifest

    def _epoch_context(self,run,records,state_key,state,hits,trace_id,tools,system,runtime_status,guidance,policy,fence):
        """Memory working state and raw-source Epoch share one exact coverage.

        This path does not reuse old live floors or summarize a summary. It
        preserves required loops, user corrections and failure properties in the
        working state; optional retrieval is removed before history governance.
        """
        from ..projection_epoch import EpochProjection
        rid=run['id'];m=run['data']['manifest']
        maximum=min(self.profile.max_tokens,policy.get('max_tokens',self.profile.max_tokens))
        reserve=max(self.profile.reserve_tokens,policy.get('reserve_tokens',0),getattr(self.provider,'max_tokens',None) or 0)
        coord=ContextBudgetCoordinator(self.provider,maximum,reserve,self.profile.safety_tokens,m['model_config_digest'])
        coord.serialized=self.serialized
        # Dynamic retrieval is optional; the raw working-state version remains
        # independently addressable and is never inferred from epoch cards.
        context={'type':'memory_context_data','trust':'UNTRUSTED_DATA_NOT_AUTHORITY','working_state':state,
            'working_state_digest':state_key,'host_runtime_status':runtime_status,'retrieved_evidence':[]}
        prefix=[{'role':'user','content':canonical(context).decode()}]
        available=maximum-reserve-self.profile.safety_tokens-coord.estimate(system,prefix,tools)
        epoch=EpochProjection(self.store,self.runtime)
        view,trace=epoch.project(rid,groups(self.store.messages(rid)),system=system,tools=tools,serialize=self.serialized,
            available_history=available,scale=coord.scale,prefix_messages=prefix,
            fits_limit=lambda msg:coord.estimate(system,msg,tools)+reserve+self.profile.safety_tokens<=maximum)
        messages=[*prefix,*[msg for group in view for msg in group]];groups(messages)
        breakdown=coord.assert_fits(system,messages,tools,guidance);request=self.serialized(system,messages,tools)
        epoch.assert_current(rid,trace)
        envelope=ProviderEnvelope(profile_digest=digest(run['data'].get('model_config',{})),messages=messages,rebase_verified=False)
        envelope_ref=self.store.json_blob(envelope);request_ref=self.store.json_blob(request)
        with self.store.tx() as c:
            for record in records:
                self.repo.derive(record['ref']['content_digest'],[record['ref']['source_id'],*record['artifact_dependencies']],c)
            self.repo.derive(envelope_ref,[trace_id,*[r['ref']['content_digest'] for r in records]],c)
            self.repo.derive(request_ref,[envelope_ref],c)
        manifest=RequestContextManifest(run_id=rid,turn_request_id=uid('request-'),release_digest=m['release_digest'],
            model_config_digest=m['model_config_digest'],provider_protocol_profile_digest=envelope.profile_digest,
            knowledge_snapshot_digest=m['knowledge_snapshot_digest'],knowledge_index_digest=m['knowledge_index_digest'],
            working_state_digest=state_key,summary_node_ids=[],summary_cutoff=0,retrieval_trace_digest=trace_id,
            selected_memory_ids_versions=[],tool_schema_digest=digest(tools),control_epoch=run['control_epoch'],writer_epoch=run['writer_epoch'],
            revocation_epoch=fence[2],authorization_digest=fence[3],visibility_domain=run['data']['visibility'],
            estimated_input_tokens=breakdown.estimated_input_total,reserved_output_tokens=reserve,
            serialized_request_digest=digest(request),protocol_envelope_ref=envelope_ref,mode=self.profile.mode,
            projection_epoch_id=trace['projection_epoch_id'],projection_epoch_generation=trace['epoch_generation'],
            budget_breakdown=asdict(breakdown)).model_dump()
        self.admission(manifest)
        self.repo.put('context_requests',manifest['turn_request_id'],{'manifest':manifest,'envelope_ref':envelope_ref,
            'request_ref':request_ref,'usage':{},'usage_status':'PENDING'})
        self.store.put('projection',rid,{'schema_version':2,'manifest':manifest,'estimated_tokens':breakdown.estimated_input_total,
            'estimate_only':True,'live_compact':trace})
        return system,messages,manifest

    def reserve_main(self, manifest):
        self.admission(manifest)
        return self.repo.reserve(manifest["run_id"],"main",manifest["estimated_input_tokens"],manifest["reserved_output_tokens"],
                                 self.profile.auxiliary_calls,self.profile.total_token_budget)

    def record_usage(self,manifest,usage,seconds,status=None):
        with self.store.tx() as c:
            row=self.repo.get('context_requests',manifest['turn_request_id'],c)
            row.update(usage=usage,usage_status=status or ('REPORTED' if usage else 'USAGE_UNKNOWN'),seconds=seconds)
            self.repo.put('context_requests',manifest['turn_request_id'],row,c,False)

    async def on_terminal(self, run_id):
        if not self.profile.learning_enabled:return
        self.assert_run_profile(run_id)
        run=self.runtime.run(run_id)
        if run["state"] not in {"SUCCEEDED","FAILED","PARTIAL","BLOCKED","CANCELLED"} or run["data"]["visibility"]!="DEV": return
        records=self.history.records(run_id)
        job=self.jobs.enqueue(run["data"]["manifest"]["scope"],run_id,digest(records),self.profile_digest,"TERMINAL",
                              {'control':run['control_epoch'],'writer':run['writer_epoch'],'revocation':self.repo.epoch()})
        job=self.jobs.claim(job["id"])
        if job is None:return
        head=self.repo.get("snapshot_heads",job["scope"])
        claims=self.consolidator.terminal_claim(run_id)
        claims+=self.consolidator.file_observations(run_id)
        if self.profile.semantic_consolidation and run['state']!='CANCELLED':
            try:
                from ..episodes import EpisodeBuilder
                for episode_id in EpisodeBuilder(self.store).capture_run(run_id):
                    await self.consolidator.curate_episode(episode_id,self.provider)
            except Exception as exc:
                self.repo.put("memory_control",uid("consolidation-failure-"),{"run_id":run_id,"code":getattr(exc,"code",type(exc).__name__),"observation_preserved":True})
        if claims:self.consolidator.commit(claims,job,self.jobs,head)
        else:
            with self.store.tx() as c:self.jobs.finish(job,[],c)

    async def on_correction(self,run_id):
        self.assert_run_profile(run_id)
        import re
        run=self.runtime.run(run_id)
        if run['data']['visibility']!='DEV' or not self.profile.semantic_consolidation or not self.profile.learning_enabled:return
        records=self.history.records(run_id)
        last=next((r for r in reversed(records) if r['message']['role']=='user'),None)
        if not last or not re.match(r'(?i)\s*(correction:|remember:|preference:|更正[:：]|记住[:：]|偏好[:：])',last['message'].get('content','')):return
        job=self.jobs.enqueue(run['data']['manifest']['scope'],run_id,digest(records),self.profile_digest,'CORRECTION:'+last['ref']['source_id'],
                             {'control':run['control_epoch'],'writer':run['writer_epoch'],'revocation':self.repo.epoch()})
        job=self.jobs.claim(job['id'])
        if not job:return
        try:
            claims=await self.consolidator.extract(run_id,self.provider)
            if claims:self.consolidator.commit(claims,job,self.jobs,self.repo.get('snapshot_heads',job['scope']))
            else:
                with self.store.tx() as c:self.jobs.finish(job,[],c)
        except Exception as exc:
            self.repo.put('memory_control',uid('correction-failure-'),{'run_id':run_id,'code':getattr(exc,'code',type(exc).__name__)})

    async def on_verified_stage(self,run_id,decision):
        """A Host check can close a stage without certifying the entire task."""
        if not self.profile.learning_enabled:return
        self.assert_run_profile(run_id)
        run=self.runtime.run(run_id)
        if run['data']['visibility']!='DEV' or run['state']!='RUNNING':return
        data=decision.model_dump(mode='json')
        if self.store.get('decision',decision.intent_digest)!=data or not self.store.verify(
                decision.model_dump(mode='json',exclude={'signature'}),decision.signature):
            raise RepoError('UNTRUSTED_DECISION')
        intent=self.store.get('intent',decision.intent_digest)
        if not intent or intent['run_id']!=run_id or intent['source_digest']!=self.runtime.workspace(run).snapshot():
            raise RepoError('STAGE_SOURCE_CHANGED')
        evidence=[self.store.get('evidence',key) for key in decision.evidence_ids]
        if not any(row and row['status']=='PASSED' for row in evidence):return
        from .models import ClaimRecord,EvidenceRef
        payload={'intent':decision.intent_digest,'source_digest':intent['source_digest'],
                 'checks':[{k:row[k] for k in ('id','check_id','status')} for row in evidence if row],
                 'coverage':'registered_checks_only','task_delivery_verified':False}
        marker='stage:'+run_id+':'+decision.intent_digest
        event=self.repo.get('memory_control',marker)
        if not event:
            with self.store.tx() as c:
                sequence=self.store.emit(c,run_id,'memory.stage_observed',payload,'DEV')
                event=dict(c.execute('SELECT * FROM events WHERE sequence=?',(sequence,)).fetchone())
                event['data']=json.loads(event['data'])
                self.repo.put('memory_control',marker,event,c)
        ref=EvidenceRef(source_kind='verification',source_id=event['event_id'],content_digest=digest(event['data']),
            run_id=run_id,scope_id=run['data']['manifest']['scope'],visibility_domain='DEV',origin_class='HOST_OBSERVATION',
            observed_at=event['created'],selector={'type':'json_pointer','value':''})
        claim=ClaimRecord(claim_id='stage-'+digest([run_id,decision.intent_digest])[7:31],kind='EPISODE',
            text='Stage check observation: '+canonical(payload).decode(),scope_id=ref.scope_id,support_refs=[ref],
            derived_from=[run_id,decision.intent_digest,*decision.evidence_ids,ref.source_id],epistemic_status='OBSERVED',
            validity='HISTORICAL',lifecycle='ACTIVE' if self.profile.auto_activate_observations else 'CANDIDATE',
            independent_support=[run['task_id']])
        job=self.jobs.enqueue(ref.scope_id,run_id,digest(self.history.records(run_id)),self.profile_digest,'STAGE:'+decision.intent_digest,
            {'control':run['control_epoch'],'writer':run['writer_epoch'],'revocation':self.repo.epoch()})
        job=self.jobs.claim(job['id'])
        if job:self.consolidator.commit([claim],job,self.jobs,self.repo.get('snapshot_heads',ref.scope_id))

    def close(self):
        if self.encoder and self.owns_encoder:self.encoder.close()
