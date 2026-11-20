"""Evidence protocol v2, using the existing Curator/queue/budget/commit pipeline."""
from __future__ import annotations
import json
from typing import Literal
from pydantic import Field,ValidationError
from .curation import EpisodeCuration,ProcedureContract
from .evidence_pipeline import Schema,Name,Digest,EvidencePipeline,CORE_ROLES,validate_metadata
from ..observation import EvolutionQueue
from ..util import RepoError,digest,canonical,redact

class Citation(Schema):
    evidence_id:Name
    selector:Name
    source_digest:Digest
    quote:str=Field(min_length=1,max_length=4000)
    relation:Literal['supports','implements','verifies','contradicts','context']

class Procedure(ProcedureContract):
    problem_pattern:str=Field(min_length=1,max_length=1000)
    preconditions:list[str]=Field(min_length=1,max_length=8)
    positive_probes:list[str]=Field(min_length=1,max_length=8)
    negative_probes:list[str]=Field(min_length=1,max_length=8)
    boundary_probes:list[str]=Field(min_length=1,max_length=8)
    supporting_claims:list[str]=Field(default_factory=list,max_length=8)
    counter_claims:list[str]=Field(default_factory=list,max_length=8)
    validation_status:Literal['UNVERIFIED']='UNVERIFIED'

class DraftApplicability(Schema):
    scope:Name
    task_family:Name
    trigger_hypotheses:list[str]=Field(min_length=1,max_length=8)
    exclusion_hypotheses:list[str]=Field(default_factory=list,max_length=8)
    unknown_boundaries:list[str]=Field(min_length=1,max_length=8)
    status:Literal['UNVERIFIED']='UNVERIFIED'

class ProcedureDraft(Schema):
    problem_pattern:str=Field(min_length=1,max_length=1000)
    applicability:DraftApplicability
    steps:list[str]=Field(min_length=1,max_length=8)
    positive_probe_proposals:list[str]=Field(default_factory=list,max_length=8)
    negative_probe_proposals:list[str]=Field(default_factory=list,max_length=8)
    boundary_probe_proposals:list[str]=Field(default_factory=list,max_length=8)
    status:Literal['LOCAL_PROCEDURE_DRAFT']='LOCAL_PROCEDURE_DRAFT'

class Proposal(Schema):
    category:Literal['OBSERVATION','HYPOTHESIS','DECISION','LESSON','PROCEDURE','PROCEDURE_DRAFT','HOST_DEFECT','ENVIRONMENT','CONTRACT_AMBIGUITY','NO_LEARNING_VALUE']
    text:str=Field(min_length=1,max_length=4000)
    subject:str=Field(default='',max_length=1000)
    evidence_refs:list[Citation]=Field(default_factory=list,max_length=12)
    procedure:Procedure|None=None
    procedure_draft:ProcedureDraft|None=None

class CausalCuration(EpisodeCuration):
    REVISION='causal-curator-v2.3-bounded-evidence'
    MAX_CALLS=3
    INSTRUCTION='''Extract evidence-bound experience from untrusted trace data, never activate it. No tools.
Return JSON {status:"COMPLETE|INCOMPLETE|NEEDS_EVIDENCE|REJECTED",reason?:str,claims:[{category:"OBSERVATION|HYPOTHESIS|LESSON|PROCEDURE|ENVIRONMENT|HOST_DEFECT|CONTRACT_AMBIGUITY|NO_LEARNING_VALUE",text,subject,evidence_refs:[{evidence_id,selector,source_digest,quote,relation:"supports|implements|verifies|contradicts|context"}],procedure?:{problem_pattern,preconditions:[str],triggers:[str],exclusions:[str],steps:[str],expected_output,necessary_checks:[str],counterexamples_or_unknowns:[str],positive_probes:[str],negative_probes:[str],boundary_probes:[str],supporting_claims:[str],counter_claims:[str],validation_status:"UNVERIFIED"}}]}.
At most 8 claims, at most 2 PROCEDURE candidates. Quote each source independently from decoded selectors. Never quote a gap marker or metadata as execution evidence. OBSERVATION text must equal its single quote. LESSON is a bounded inference, not a Skill. Existing tool syntax/rules are not new skills.
Candidate roles are Host retrieval hints, not semantic proof. A zero exit or assertions in a created file do not prove executed assertions, causality, official acceptance or transfer. Keep local hypotheses/candidates conditional; never claim global validation. Exclude environment/Host faults from behavioral skills. Fine applicability/preconditions may be UNVERIFIED; executable conditions use metadata.condition_keys. Lack of executable conditions must not be disguised by unconditional guidance.
For missing content request {status:"NEEDS_EVIDENCE",reason:str,claims:[],evidence_requests:[{evidence_id,selector,source_digest,symbol?:str,offset?:int,max_bytes:int}]}. Use only catalogue IDs/allowed selectors; no paths/commands. At most 4 requests per round, 2 rounds total. Offsets count decoded characters. local_check_chains are Host-checked observations of failure, an intervening saved change and the same local check passing. They do not establish causality or replace exact quotes. Incomplete evidence is a valid result. Do not force a Procedure.'''

    def __init__(self,owner):
        super().__init__(owner);self.pipeline=EvidencePipeline(owner)
        self.config=self.store.get('operator_config','curator_evidence_pipeline',default={})
        self.expansions=self.config.get('max_expansions',2)
        self.byte_budget=self.config.get('input_evidence_bytes',24000)
        if type(self.expansions) is not int or not 0<=self.expansions<=2:raise RepoError('CURATOR_CONFIG')
        self.MAX_CALLS=1+self.expansions
        self.config_digest=digest(self.config)
        self.INSTRUCTION += '''
ProcedureDraft is an experience-layer hypothesis, not a Runtime capability. You may output category PROCEDURE_DRAFT with procedure_draft:{problem_pattern,applicability:{scope,task_family,trigger_hypotheses:[natural-language str],exclusion_hypotheses:[str],unknown_boundaries:[str],status:"UNVERIFIED"},steps:[str],positive_probe_proposals:[str],negative_probe_proposals:[str],boundary_probe_proposals:[str],status:"LOCAL_PROCEDURE_DRAFT"}.
Use payload.draft_context.scope and task_family exactly. Natural-language triggers need not be Host predicate keys. Exclusion hypotheses and probe proposals may be empty; unknown_boundaries must be explicit. A single local experience can support a conditional draft without transfer proof. A draft grants no runtime eligibility or activation. Keep evidence_refs and limitations; do not force a draft or repeat existing tool rules.'''
        if self.config.get('procedure_draft_stage'):
            self.INSTRUCTION += '\nThis is one optional Procedure Draft formation check. Review seed_lessons as prior untrusted candidate experience alongside original evidence. Decide whether a reusable local action sequence is supported. Return a PROCEDURE_DRAFT or an explicit insufficiency reason. Do not turn natural-language applicability into verified Runtime predicates.'
            self.INSTRUCTION += '\nIn this stage any proposed workflow uses PROCEDURE_DRAFT and procedure_draft, not legacy PROCEDURE. Bind problem/diagnosis evidence as supports and recorded implementation/action evidence as implements. Use current evidence digests for citations. Runtime predicates and registered probes are not required to save a draft; unavailable checks remain proposals and unknown boundaries.'

    def _cancelled(self,episode):
        run=self.owner.runtime.run(episode['run_id'])
        if run.get('state')=='CANCELLED' or run['data'].get('end_reason')=='CANCEL':raise RepoError('CURATOR_CANCELLED')

    def _adapt_sources(self,episode,records):
        events={e['event_id']:e for e in self.builder.evidence(episode['episode_id'])['events']}
        return {sid:{'event_digest':digest(events[sid]),'content_digest':r['metadata']['content_digest'],
            'observed_at':events[sid]['created'],'type':events[sid]['type'],'record':r,
            'text':canonical(r['fields']).decode()} for sid,r in records.items()}

    def _migrate_shared(self,value,records):
        # Explicit legacy protocol adapter; a shared quote must really bind all refs.
        if 'evidence_refs' in value:raise RepoError('CURATOR_CLAIM_SCHEMA')
        if set(value)-{'category','text','subject','source_ids','quote','procedure'}:raise RepoError('CURATOR_CLAIM_SCHEMA')
        refs=[]
        for sid in value.get('source_ids',[]):
            record=records.get(sid);quote=value.get('quote','')
            if not record:raise RepoError('CURATOR_EVIDENCE_DENIED')
            selector=next((k for k,v in record['fields'].items() if quote and quote in v),None)
            if not selector:raise RepoError('CURATOR_QUOTE_UNSUPPORTED')
            refs.append({'evidence_id':sid,'selector':selector,'source_digest':record['metadata']['content_digest'],'quote':quote,'relation':'supports'})
        return {k:v for k,v in value.items() if k not in {'source_ids','quote'}}|{'evidence_refs':refs}

    def _validate(self,value,episode,metadata,sources):
        if not isinstance(value,dict):raise RepoError('CURATOR_CLAIM_SCHEMA')
        records={k:v['record'] for k,v in sources.items()}
        if value.get('category')=='NO_LEARNING_VALUE':return {'category':'NO_LEARNING_VALUE'}
        if self.config.get('citation_mode')=='legacy_shared':value=self._migrate_shared(value,records)
        claim=Proposal.model_validate(value)
        if not claim.evidence_refs:raise RepoError('CURATOR_EVIDENCE_DENIED')
        refs=[];bindings=[];relations=set();roles=set()
        for ref in claim.evidence_refs:
            source=sources.get(ref.evidence_id)
            if not source:raise RepoError('CURATOR_EVIDENCE_DENIED')
            record=source['record'];m=record['metadata']
            self.repo.ensure_dev([ref.evidence_id,*m['artifact_refs']])
            if ref.selector not in record['fields']:raise RepoError('EVIDENCE_SELECTOR_DENIED')
            if ref.source_digest!=m['content_digest']:raise RepoError('EVIDENCE_DIGEST_CHANGED')
            content=record['fields'][ref.selector]
            # Validate only delivered content; unseen whole-record text is not citation authority.
            views=self.delivered.get((ref.evidence_id,ref.selector),[])
            pos=next((start+view.index(ref.quote) for start,view in views if ref.quote in view),None)
            if (not ref.quote.strip() or '[BOUNDED EXCERPT' in ref.quote or pos is None
                    or content[pos:pos+len(ref.quote)]!=ref.quote):raise RepoError('CURATOR_QUOTE_UNSUPPORTED')
            r=set(m['role']);roles|=r;relations.add(ref.relation)
            if ref.relation=='implements' and ('IMPLEMENTATION_CHANGE' not in r or ref.selector not in {'new_hunk','created_content','diff_hunk','function_definition'}):raise RepoError('CLAIM_RELATION_UNSUPPORTED')
            if ref.relation=='verifies' and ('POSITIVE_VERIFICATION' not in r or ref.selector!='stdout_excerpt' or 'PARTIAL_CHECK_SELECTION' in m['limitations']):raise RepoError('CLAIM_RELATION_UNSUPPORTED')
            if ref.relation=='supports' and r=={'ENVIRONMENT_BLOCKER'} and claim.category in {'PROCEDURE','PROCEDURE_DRAFT','LESSON'}:raise RepoError('CLAIM_RELATION_UNSUPPORTED')
            refs.append(ref.evidence_id);bindings.append(ref.model_dump()|{'match_start':pos,'match_end':pos+len(ref.quote),'match_version':'DECODED_EXACT_V2'})
        if len({(b['evidence_id'],b['selector'],b['quote']) for b in bindings})!=len(bindings):raise RepoError('CURATOR_DUPLICATE_SOURCE')
        if claim.category=='OBSERVATION' and (len(bindings)!=1 or claim.text!=bindings[0]['quote']):raise RepoError('CURATOR_OBSERVATION_NOT_EXACT')
        if claim.category=='DECISION':raise RepoError('CURATOR_DECISION_AUTHORITY')
        procedure=None;draft=None
        if claim.category=='PROCEDURE_DRAFT':
            if claim.procedure_draft is None or claim.procedure is not None:raise RepoError('CURATOR_PROCEDURE_SCHEMA')
            if episode['failure_class'] in {'HOST','ENVIRONMENT','EVALUATION','CONTRACT'}:raise RepoError('CURATOR_HOST_NOT_SKILL')
            if not roles&{'ORIGINAL_FAILURE','ROOT_CAUSE_DIAGNOSIS'} or 'implements' not in relations:raise RepoError('PROCEDURE_CAUSAL_CHAIN_INCOMPLETE')
            draft=claim.procedure_draft.model_dump()
            if draft['applicability']['scope']!=episode['scope'] or draft['applicability']['task_family']!=episode['task_family']:raise RepoError('DRAFT_SCOPE_MISMATCH')
            items=[*draft['steps'],*draft['applicability']['trigger_hypotheses'],*draft['applicability']['unknown_boundaries'],*draft['applicability']['exclusion_hypotheses'],*draft['positive_probe_proposals'],*draft['negative_probe_proposals'],*draft['boundary_probe_proposals']]
            if any(not x.strip() or len(x)>1000 for x in items):raise RepoError('CURATOR_PROCEDURE_SCHEMA')
        elif claim.procedure_draft is not None:raise RepoError('CURATOR_PROCEDURE_CATEGORY')
        if claim.category=='PROCEDURE':
            if episode['failure_class'] in {'HOST','ENVIRONMENT','EVALUATION','CONTRACT'}:raise RepoError('CURATOR_HOST_NOT_SKILL')
            if claim.procedure is None:raise RepoError('CURATOR_PROCEDURE_SCHEMA')
            if not roles&{'ORIGINAL_FAILURE','ROOT_CAUSE_DIAGNOSIS'} or 'IMPLEMENTATION_CHANGE' not in roles or 'implements' not in relations:raise RepoError('PROCEDURE_CAUSAL_CHAIN_INCOMPLETE')
            procedure=claim.procedure.model_dump()
            if any(not x.strip() for v in procedure.values() if isinstance(v,list) for x in v):raise RepoError('CURATOR_PROCEDURE_SCHEMA')
            if not set(procedure['triggers']+procedure['exclusions'])<=set(metadata['condition_keys']):raise RepoError('CURATOR_CONDITION_UNOBSERVABLE')
            # Preserve the established downstream ProcedureContract; richer probes stay in proposal.
            procedure=ProcedureContract.model_validate({k:procedure[k] for k in ProcedureContract.model_fields}).model_dump()
        elif claim.procedure is not None:raise RepoError('CURATOR_PROCEDURE_CATEGORY')
        rendered=canonical(value).decode()
        if redact(rendered)!=rendered or any(s in rendered for s in self.owner.history.known_secrets(episode['run_id'])):raise RepoError('SENSITIVE_MEMORY_CONTENT')
        result={'category':claim.category,'text':claim.text,'subject':claim.subject,'source_ids':list(dict.fromkeys(refs)),
            'quote':bindings[0]['quote'],'evidence_refs':bindings,
            'semantic_review':{'status':'PENDING_INDEPENDENT_REVIEW','structural_relations_checked':True,
                'string_match_is_not_semantic_proof':True,'automatic_activation':False}}
        if procedure:result.update(procedure=procedure,procedure_proposal=claim.procedure.model_dump(),
            candidate_validation_plan={'status':'UNREGISTERED_REQUIRES_TRUSTED_CHECK_BINDING','positive':claim.procedure.positive_probes,
                'negative':claim.procedure.negative_probes,'boundary':claim.procedure.boundary_probes,
                'gates':['S1','S2','S3','G0','G1','G2','G3','G4','G4b'],'scope':'SINGLE_TASK'})
        if draft:result.update(procedure_draft=draft,runtime_eligible=False)
        return result

    def _remember(self,state,records):
        self.delivered={}
        for source in state['payload']['evidence']:
            for selector,content in source['selectors'].items():
                sid=source['evidence_id'];raw=records[sid]['fields'][selector]
                views=self.delivered.setdefault((sid,selector),[])
                window=source.get('windows',{}).get(selector)
                if window is not None:parts=[(window['field_offset'],content)]
                elif raw.startswith(content):parts=[(0,content)]
                else:
                    head,gap,tail=content.partition('\n[BOUNDED EXCERPT; NOT CONTIGUOUS]\n')
                    if not gap:raise RepoError('EVIDENCE_DIGEST_CHANGED')
                    parts=[(0,head),(len(raw)-len(tail),tail)]
                for start,part in parts:
                    if raw[start:start+len(part)]!=part:raise RepoError('EVIDENCE_DIGEST_CHANGED')
                    views.append((start,part))

    async def run(self,episode_id,provider):
        if not self.profile.learning_enabled:raise RepoError('CURATOR_LEARNING_DISABLED')
        episode=self.builder.get(episode_id);self._cancelled(episode)
        metadata,records,catalogue=self.pipeline.build(episode_id);metadata=metadata.model_dump()
        sources=self._adapt_sources(episode,records)
        key=digest(['episode-curation',episode_id]);profile_digest=digest([self.profile.model_dump(),self.config_digest])
        state=self.store.get('episode_curation',key)
        if state and (state.get('revision')!=self.REVISION or state.get('profile_digest')!=profile_digest):raise RepoError('CURATOR_PROFILE_CHANGED')
        if state and state.get('phase')=='COMMITTED':return state['report']
        for row in self.repo.values('memory_budget'):
            if row['run_id']==episode['run_id'] and row['state'] in {'RESERVED','USAGE_UNKNOWN'}:raise RepoError('CURATOR_USAGE_UNSETTLED')
        queue=EvolutionQueue(self.store);job=queue.claim(kind='L1_CURATE',lease_seconds=300,problem=episode_id)
        if not job:raise RepoError('CURATOR_JOB_UNAVAILABLE')
        try:
            if state is None:
                chains=metadata['verification_summary'].get('local_check_chains',[])
                selected,selection=self.pipeline.select(records,self.byte_budget,chains)
                if self.config.get('selection')=='position':
                    ids=list(records);fail=[i for i in ids if 'ENVIRONMENT_BLOCKER' in records[i]['metadata']['role'] or '"exit_code": 1' in records[i]['fields'].get('exit_status','')]
                    selected=[{'evidence_id':i,'source_digest':records[i]['metadata']['content_digest'],'roles':records[i]['metadata']['role'],
                        'selectors':{k:__import__('repo_code.memory.evidence_pipeline',fromlist=['clipped']).clipped(v,1600) for k,v in records[i]['fields'].items()}} for i in (fail+[i for i in ids if i not in fail])[:4]]
                    selection={'selection_version':'LEGACY_POSITION_REPLAY','selected_ids':[x['evidence_id'] for x in selected]}
                # The full catalogue stays on the Host; do not replicate every log summary into the prompt.
                prompt_meta=dict(metadata)
                rows=metadata['evidence_catalogue']
                if len(canonical(rows))>self.byte_budget:
                    ids={s['evidence_id'] for s in selected}
                    compact=[];n=2
                    for row in sorted(rows,key=lambda x:x['evidence_id'] not in ids):
                        hint={k:row[k] for k in ('evidence_id','role','content_digest','allowed_selectors','content_bytes')}
                        size=len(canonical(hint))+1
                        if n+size<=self.byte_budget:compact.append(hint);n+=size
                    prompt_meta.update(evidence_catalogue=compact,event_ids=[x['evidence_id'] for x in compact],
                                       catalogue_count=len(rows),catalogue_ref=catalogue)
                state={'episode_id':episode_id,'revision':self.REVISION,'profile_digest':profile_digest,'calls':0,
                    'phase':'EVIDENCE_SELECTED','payload':{'metadata':prompt_meta,'evidence':selected,'claim_schema_version':'claim-v2.1',
                        'draft_context':{'scope':episode['scope'],'task_family':episode['task_family']}},
                    'selection':selection,'catalogue_ref':catalogue,'expansion_rounds':0,'expanded_selectors':[],
                    'expanded_bytes':0,'pending_claims':[],'transitions':['QUEUED','METADATA_READY','EVIDENCE_SELECTED']}
                self.store.put('episode_curation',key,state)
                if self.config.get('procedure_draft_stage') and not episode.get('stage_candidate_id'):
                    seed=self.store.get('operator_config','procedure_draft_seed',default={})
                    if self.builder._protected(seed):raise RepoError('PROTECTED_EVALUATION_CONTENT')
                    if any(sid not in sources for sid in seed.get('source_ids',[])):raise RepoError('CURATOR_EVIDENCE_DENIED')
                    self.repo.ensure_dev(seed.get('source_ids',[]))
                    state['payload']['seed_lessons']=seed.get('lessons',[])
                    state['payload']['seed_provenance']=seed.get('provenance',{})
                    self.store.put('episode_curation',key,state)
            if self.config.get('citation_mode')=='legacy_shared':
                self.INSTRUCTION=self.INSTRUCTION.replace('evidence_refs:[{evidence_id,selector,source_digest,quote,relation:"supports|implements|verifies|contradicts|context"}]','source_ids:[evidence_id],quote:str')+'\nLegacy citation replay: use one shared quote present in every cited source. Other claim/procedure fields remain the same version.'
                self.INSTRUCTION=self.INSTRUCTION.replace('Quote each source independently from decoded selectors.','Quote the shared decoded source text exactly.')
            while True:
                self._cancelled(episode);self._remember(state,records)
                if state.get('phase')=='RESPONSE_RECORDED':answer=self.store.read_json(state['answer_ref'])
                else:
                    state['transitions'].append('CURATOR_RUNNING')
                    answer=await self._call(episode,state['payload'],provider,key,job,queue,state)
                self._cancelled(episode);self.builder.get(episode_id)
                _,fresh,fresh_ref=self.pipeline.build(episode_id)
                if fresh_ref!=state['catalogue_ref']:raise RepoError('EVIDENCE_DIGEST_CHANGED')
                if {k:v['metadata']['content_digest'] for k,v in fresh.items()}!={k:v['metadata']['content_digest'] for k,v in records.items()}:raise RepoError('EVIDENCE_DIGEST_CHANGED')
                status=answer.get('status');claims=answer.get('claims',[])
                if status not in {'COMPLETE','INCOMPLETE','REJECTED','NEEDS_EVIDENCE'} or not isinstance(claims,list) or len(claims)>8:raise RepoError('CURATOR_INVALID_RESPONSE')
                # Previously grounded observations survive an exhausted expansion, never half a Skill.
                prior_claims=list(state['pending_claims'])
                for claim in claims:
                    if isinstance(claim,dict) and claim.get('category') in {'OBSERVATION','HYPOTHESIS','LESSON'}:
                        if claim not in state['pending_claims']:state['pending_claims'].append(claim)
                delivered_roles={role for s in state['payload']['evidence'] for role in s['roles']}
                missing=[r for r in CORE_ROLES if r not in delivered_roles and any(r in v['metadata']['role'] for v in records.values())]
                requests=answer.get('evidence_requests',[])
                draft_only=any(isinstance(x,dict) and x.get('category')=='PROCEDURE_DRAFT' for x in claims) and not any(isinstance(x,dict) and x.get('category')=='PROCEDURE' for x in claims)
                if status=='COMPLETE' and missing and not draft_only and state['expansion_rounds']<self.expansions:
                    requests=[]
                    for role in missing:
                        sid=max((i for i,v in records.items() if role in v['metadata']['role']),key=lambda i:records[i]['metadata']['quality'])
                        selector=next((k for k in ['function_definition','new_hunk','created_content','stdout_excerpt','read_content'] if k in records[sid]['fields']),next(iter(records[sid]['fields'])))
                        req={'evidence_id':sid,'selector':selector,'max_bytes':2000}
                        if selector=='function_definition':req['symbol']=next(iter(records[sid]['definitions']))
                        requests.append(req)
                    requests=requests[:4];status='NEEDS_EVIDENCE';state['transitions'].append('EVIDENCE_COVERAGE_INCOMPLETE')
                if status=='NEEDS_EVIDENCE':
                    if state['expansion_rounds']>=self.expansions:
                        status='INCOMPLETE';state['stop_reason']='EXPANSION_LIMIT'
                    elif not isinstance(requests,list) or not 1<=len(requests)<=4:
                        status='INCOMPLETE';state['stop_reason']='INVALID_EVIDENCE_REQUEST'
                    else:
                        state['transitions'].append('HOST_REQUEST_VALIDATION');expanded=[]
                        try:
                            round_keys=[]
                            for req in requests:
                                if not isinstance(req,dict):raise RepoError('EVIDENCE_REQUEST_SCHEMA')
                                request_key=digest([req.get('evidence_id'),req.get('selector'),req.get('symbol'),req.get('offset',0)])
                                if request_key in state['expanded_selectors'] or request_key in round_keys:raise RepoError('EVIDENCE_ALREADY_EXPANDED')
                                ref,receipt=self.pipeline.expand(episode_id,records,req,key)
                                if state['expanded_bytes']+sum(len(x['content'].encode()) for _,x in expanded)+len(receipt['content'].encode())>self.byte_budget:raise RepoError('EVIDENCE_EXPANSION_BUDGET')
                                expanded.append((ref,receipt));round_keys.append(request_key)
                        except (RepoError,TypeError,KeyError) as exc:
                            status='INCOMPLETE';state['stop_reason']=getattr(exc,'code','INVALID_EVIDENCE_REQUEST')
                            state['transitions'].append('DENIED')
                        else:
                            self._cancelled(episode)
                            for _,x in expanded:
                                sid=x['evidence_id'];existing=next((s for s in state['payload']['evidence'] if s['evidence_id']==sid),None)
                                if existing is None:
                                    existing={'evidence_id':sid,'source_digest':x['source_digest'],'roles':records[sid]['metadata']['role'],'selectors':{}}
                                    state['payload']['evidence'].append(existing)
                                # Keep disjoint windows separate: a join must never create a quotable sentence.
                                if x['selector'] in existing['selectors']:
                                    existing={k:existing[k] for k in ('evidence_id','source_digest','roles')}
                                    existing['selectors']={};state['payload']['evidence'].append(existing)
                                existing['selectors'][x['selector']]=x['content']
                                existing.setdefault('windows',{})[x['selector']]={k:x[k] for k in ('offset','field_offset','next_offset','content_chars')}
                            state['expansion_rounds']+=1;state['expanded_selectors']+=round_keys
                            state['expanded_bytes']+=sum(len(x['content'].encode()) for _,x in expanded)
                            state.setdefault('expansion_receipts',[]).append([ref for ref,_ in expanded])
                            state['payload']['expansion']=True;state['payload']['reason']=answer.get('reason',state.get('stop_reason','EVIDENCE_COVERAGE_INCOMPLETE'))
                            state['phase']='EVIDENCE_EXPANSION';state['transitions']+=['GRANTED','EVIDENCE_EXPANSION','CURATOR_RETRY']
                            self.store.put('episode_curation',key,state)
                            continue
                if status=='COMPLETE' and (missing or metadata['missing_roles']):
                    status='INCOMPLETE';state['stop_reason']='MISSING_CAUSAL_ROLES'
                # Revalidate and deduplicate every retained round; COMPLETE must not erase prior lessons.
                proposals=[];seen=set()
                for proposal in [*prior_claims,*claims]:
                    identity=digest(proposal)
                    if identity not in seen:proposals.append(proposal);seen.add(identity)
                if status=='REJECTED':proposals=prior_claims
                # Episode completeness is not Draft eligibility. Each local Draft
                # still passes _validate (exact delivered quotes, diagnosis and
                # implementation, scope, unknowns, DEV/revocation checks), remains
                # runtime_eligible=False, and cannot bypass promotion/Release.
                if status!='COMPLETE':proposals=[c for c in proposals if not isinstance(c,dict) or c.get('category')!='PROCEDURE']
                state['transitions'].append(status);self._remember(state,records)
                state['completion_metadata']=dict(status=status,stop_reason=state.get('stop_reason'),selection=state['selection'],
                    expansions=state['expansion_rounds'],expanded_bytes=state['expanded_bytes'],catalogue_ref=catalogue,
                    transitions=state['transitions'],semantic_status='REQUIRES_INDEPENDENT_REVIEW',release_activated=False)
                return self.commit(episode,metadata,sources,proposals,key,job,queue,state)
        except BaseException as exc:
            if getattr(exc,'code','')=='CURATOR_CANCELLED' and state:
                state['phase']='CANCELLED';self.store.put('episode_curation',key,state)
            try:queue.finish(job['id'],'QUEUED',owner_token=job['owner_token'])
            except RepoError:pass
            raise
