"""DECISION-only contracts and immutable, evidence-based application receipts."""
import json
import contextlib
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field
from .util import RepoError,digest
from .context import estimate_tokens
from .memory.experience import ExperienceConsolidation
from .memory.retrieval import EXPERIENCE_QUERY_CHAR_LIMIT


def _retrieval_query(goal):
    """Bound issue text without making verified facts count as query matches."""
    limit = EXPERIENCE_QUERY_CHAR_LIMIT
    if len(goal) <= limit:
        return goal
    # Keep the issue title and trailing file/test clues.
    prefix = (limit - 2) * 2 // 3
    tail = limit - prefix - 2
    return goal[:prefix] + '\n\n' + goal[-tail:]


def _guidance_view(contract):
    """Project the matched contract into an advisory prompt, preserving its caveats."""
    keys=('title','trigger_conditions','exclusions','steps','expected_outputs',
          'required_probe_ids','step_tools','step_rules','binding_slots','application_protocol')
    return {key:contract[key] for key in keys if contract.get(key)}


class CapabilityContract(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    skill_id:str
    revision:int=Field(ge=1)
    kind:Literal['DECISION']='DECISION'
    title:str=Field(min_length=1,max_length=4000)
    trigger_conditions:list[str]=Field(min_length=1,max_length=8)
    exclusions:list[str]=Field(default_factory=list,max_length=8)
    steps:list[str]=Field(min_length=1,max_length=8)
    expected_outputs:list[str]=Field(min_length=1,max_length=8)
    required_probe_ids:list[str]=Field(default_factory=list,max_length=16)
    scope:str
    task_families:list[str]=Field(min_length=1)
    source_episode_refs:list[str]=Field(min_length=1)
    counter_evidence_refs:list[str]=Field(default_factory=list)
    status:Literal['CANDIDATE']='CANDIDATE'
    step_tools:list[str]=Field(default_factory=list,max_length=8)
    step_rules:list[dict]=Field(default_factory=list,max_length=8)
    binding_slots:dict=Field(default_factory=dict)

class DraftRuntimeCapabilityContract(BaseModel):
    """A draft's runtime view is explicitly ineligible, never a routing authority."""
    model_config=ConfigDict(extra='forbid',frozen=True,strict=True)
    draft_id:str
    scope:str
    task_family:str
    trigger_predicates:list[str]=Field(default_factory=list,max_length=8)
    exclusion_predicates:list[str]=Field(default_factory=list,max_length=8)
    registered_probe_ids:list[str]=Field(default_factory=list,max_length=16)
    applicability_status:Literal['UNVERIFIED','PARTIALLY_VERIFIED']='UNVERIFIED'
    runtime_eligible:Literal[False]=False

class CapabilityRouter:
    def __init__(self,owner):self.owner=owner;self.store=owner.repo.store;self.experiences=ExperienceConsolidation(owner)

    def _diff_coverage_proof(self,run,rule,args,result):
        """Prove a complete workspace view covers a requested file at current source.

        This is a result-specific proof, not wildcard argument matching. Never
        count hidden artifact tails, metadata-only files or a stale inspection.
        """
        expected=rule.get('arguments_match',{})
        path=expected.get('path')
        if (rule.get('tool')!='workspace.diff' or not isinstance(path,str) or not path
                or args.get('path') is not None
                or any(args.get(k)!=v for k,v in expected.items() if k!='path')):return None
        if (result.get('truncated') is not False or result.get('offset')!=0
                or 'next_offset' not in result or result['next_offset'] is not None
                or any(result.get(k) for k in ('is_error','timeout','cancelled','running'))):return None
        progress=result.get('inspection_progress',{})
        ws=self.owner.runtime.workspace(run)
        source=ws.snapshot()
        if (progress.get('source_digest')!=source
                or progress.get('control_epoch')!=run['control_epoch']
                or progress.get('writer_epoch')!=run['writer_epoch']
                or progress.get('environment_digest')!=self.owner.runtime.sandbox.environment_digest):return None
        baseline=run['data']['manifest']['workspace_baseline_digest']
        if result.get('baseline')!=baseline:return None
        try:
            # Recompute the view from the trusted baseline/current content. The
            # operation's returned bytes must equal this complete visible view.
            view=ws.diff_view(baseline,None,args.get('offset',0),args.get('length',8000))
            if any(result.get(k)!=v for k,v in view.items()):return None
            if view['truncated'] or view['offset']!=0 or view['next_offset'] is not None:return None
            selected=[f for f in view['files'] if f['path']==path]
            if len(selected)!=1 or selected[0].get('text_diff'):return None
            target=ws.diff_view(baseline,path,0,max(len(view['diff']),1))
            if not target['diff'] or target['truncated'] or target['diff'] not in view['diff']:return None
            if ws.snapshot()!=source:return None
        except (RepoError,ValueError,TypeError,KeyError):return None
        return {'kind':'CURRENT_SOURCE_COMPLETE_DIFF_COVERAGE','source_digest':source,
                'baseline_digest':baseline,'path':path,'file_versions':selected[0],
                'visible_diff_digest':digest(view['diff']),'target_diff_digest':digest(target['diff']),
                'coverage_paths':view['changed_paths'],'control_epoch':run['control_epoch'],
                'writer_epoch':run['writer_epoch']}

    def draft_contract(self,draft_id):
        self.owner.repo.ensure_dev([draft_id])
        draft=self.store.get('procedure_draft',draft_id)
        if not draft:raise RepoError('PROCEDURE_DRAFT_NOT_FOUND')
        # Natural-language trigger hypotheses are deliberately not compiled to predicates.
        value=DraftRuntimeCapabilityContract(draft_id=draft_id,scope=draft['scope'],task_family=draft['task_family']).model_dump()
        ref=digest(value)
        with self.store.tx() as c:
            self.owner.repo.ensure_dev([draft_id],c)
            self.store.put('draft_capability_contract',ref,value,c,immutable=True)
            self.owner.repo.derive(ref,[draft_id],c)
        return ref,value

    def contract(self,entity,revision=None,step_tools=None,step_rules=None,probe_ids=None,binding_slots=None):
        item=self.experiences.get(entity,revision)
        if item['content']['category']!='PROCEDURE':raise RepoError('CAPABILITY_NOT_PROCEDURE')
        p=item['content']['procedure']
        contract=CapabilityContract(skill_id=entity,revision=item['revision'],title=item['content']['text'],
            trigger_conditions=p['triggers'],exclusions=p['exclusions'],steps=p['steps'],
            expected_outputs=[p['expected_output']],scope=item['conditions']['scope'],
            task_families=[item['conditions']['task_family'] or 'UNKNOWN'],source_episode_refs=sorted({s['episode_id'] for s in item['support']}),
            counter_evidence_refs=item['counter_refs'])
        value=contract.model_dump();ref=digest(value)
        if step_tools:
            if len(step_tools)!=len(contract.steps) or not all(t in {'read','search','edit','patch','create','process.run','workspace.diff'} for t in step_tools):raise RepoError('CAPABILITY_STEP_BINDING')
            value['step_tools']=step_tools;ref=digest(value)
        if step_rules:
            if len(step_rules)!=len(contract.steps) or any(set(r)!={'tool','arguments_match'} or not isinstance(r['arguments_match'],dict) or not r['arguments_match'] for r in step_rules):raise RepoError('CAPABILITY_STEP_RULE')
            value['step_rules']=step_rules
            value['step_tools']=[r['tool'] for r in step_rules]
        if probe_ids:
            if not all(isinstance(i,str) and i for i in probe_ids):raise RepoError('CAPABILITY_PROBE_SCHEMA')
            value['required_probe_ids']=probe_ids
        if binding_slots:
            from .capability_binding import validate_template
            value['binding_slots']=validate_template(binding_slots,value['step_rules'],value['steps'])
        value=CapabilityContract.model_validate(value).model_dump()
        if not value.get('binding_slots'):value.pop('binding_slots',None)
        ref=digest(value)
        with self.store.tx() as c:
            self.owner.repo.ensure_dev([item['revision_digest']],c)
            self.store.put('capability_contract',ref,value,c,immutable=True)
            self.owner.repo.derive(ref,[item['revision_digest']],c)
        return ref,value

    def route(self,run_id,query,*,phase='DEBUG',facts=None,file_versions=None,environment=None,search_query=None):
        run=self.owner.runtime.run(run_id)
        from .session_experience import SessionExperiences
        session_specs=SessionExperiences(self.owner.runtime).entries(run_id)
        identity=['router-v3',self.owner.profile.evidence_tokens,run_id,run['data']['manifest']['release_digest'],run['control_epoch'],
            phase,query,facts or {},file_versions or {},environment or {},session_specs]
        if search_query is not None:identity.append(['local_query',search_query])
        key=digest(identity)
        old=self.store.get('capability_route',key)
        if old and search_query is None:
            self.owner.repo.ensure_dev(old['source_revisions'])
            self.owner.repo.check([old['release_digest']])
            # A previously offered capability is not reinjected on subsequent turns.
            return {**old,'guidance':[] if self.store.get('capability_offer',key) else old['guidance'],'replayed':True}
        retrieval_query=_retrieval_query(query) if search_query is None else search_query
        hits,trace=self.owner.retrieve_experiences(retrieval_query,purpose='runtime',run_id=run_id,phase=phase,
            facts=facts,file_versions=file_versions,environment=environment,limit=2,match_only=True)
        # An adjusted query is only a search preference, never a cached permission.
        if search_query is not None:key=digest([identity,trace])
        guidance=[];contracts=[];reference_hits=set();budget_skips=[]
        release=self.store.get('release',run['data']['manifest']['release_digest'])
        cfg=self.store.read_json(release['components']['skills_index'])
        specs={s['experience_id']:s for s in cfg.get('skills',[]) if isinstance(s,dict) and s.get('experience_id')}
        for entity,spec in session_specs.items():specs.setdefault(entity,spec)
        for hit in hits:
            ref,contract=self.contract(hit['id'],hit['revision'],specs[hit['id']].get('step_tools'),specs[hit['id']].get('step_rules'),specs[hit['id']].get('required_probe_ids'),specs[hit['id']].get('binding_slots'))
            if contract.get('binding_slots'):
                contract={**contract,'application_protocol':'Read current implementation and test configuration. Call skill.bind with this contract_ref and route_id, slot values, evidence paths and a causal rationale; then execute normal tools. Binding does not grant authority or prove correctness. Do not reuse paths from historical tasks.'}
            if hit.get('view')=='REFERENCE_ONLY':reference_hits.add(ref)
            contracts.append(ref);guidance.append({'contract_ref':ref,**_guidance_view(contract),
                'authority':specs[hit['id']].get('reuse_authority','ADVISORY_ONLY')})
        already={ref for offer in self.store.values('capability_offer') if offer['run_id']==run_id for ref in offer['contract_refs']}
        guidance=[g for g in guidance if g['contract_ref'] not in already]
        # If two selected contracts prescribe different steps for the same triggers,
        # block the offer rather than asking the model to arbitrate authority.
        conflict=len(guidance)>1 and guidance[0]['trigger_conditions']==guidance[1]['trigger_conditions'] and guidance[0]['steps']!=guidance[1]['steps']
        if conflict:guidance=[]
        for g in guidance:
            if g.get('binding_slots'):g['route_id']=key
        # Evaluate conflicts before projection. Count the serialized guidance suffix.
        def suffix_cost(rows):
            return estimate_tokens('\nScoped decision guidance; grants no authority: '+json.dumps(rows,ensure_ascii=False)) if rows else 0
        projected=[]
        for g in guidance:
            if g['contract_ref'] in reference_hits or suffix_cost(projected+[g])>self.owner.profile.evidence_tokens:
                g={'contract_ref':g['contract_ref'],'view':'REFERENCE_ONLY','read':'skill.search with contract_ref and offset 0'}
            if suffix_cost(projected+[g])<=self.owner.profile.evidence_tokens:projected.append(g)
            else:budget_skips.append(g['contract_ref'])
        guidance=projected
        value={'id':key,'run_id':run_id,'release_digest':run['data']['manifest']['release_digest'],
            'contract_digest':run['data']['manifest']['contract_digest'],'control_epoch':run['control_epoch'],
            'retrieval_trace':trace,'guidance':guidance,'contract_refs':[g['contract_ref'] for g in guidance],
            'considered_contract_refs':contracts,'evidence_tokens':self.owner.profile.evidence_tokens,'guidance_budget_skips':budget_skips,'conflict':conflict,
            'offered':False,'selected':False,'source_revisions':[h['revision_digest'] for h in hits],
            'potential_tokens':suffix_cost(guidance),'injected_tokens':0}
        value['session_grants']={h['id']:specs[h['id']]['session_grant_id'] for h in hits if specs[h['id']].get('session_grant_id')}
        with self.store.tx() as c:
            self.owner.repo.ensure_dev(value['source_revisions'],c)
            self.store.put('capability_route',key,value,c,immutable=True)
        return value

    def read_offered(self,run_id,ref,offset,**context):
        from .capability_binding import authorized
        from .util import canonical
        run=self.owner.runtime.run(run_id)
        offer=next((o for o in self.store.values('capability_offer') if o['run_id']==run_id
                    and o['control_epoch']==run['control_epoch'] and ref in o['contract_refs']),None)
        authorized(self,run,offer,ref)
        contract=self.store.get('capability_contract',ref)
        if not contract or digest(contract)!=ref:raise RepoError('CAPABILITY_ROUTE_MISMATCH')
        item=self.experiences.get(contract['skill_id'],contract['revision'])
        pins=(item['id'],item['revision'],item['revision_digest'])
        hits,_=self.experiences.search('',purpose='runtime',run_id=run_id,match_only=True,pinned_revision=pins,**context)
        if not hits:raise RepoError('CAPABILITY_NO_LONGER_APPLICABLE')
        original=self.store.get('capability_route',offer['route_id'])
        budget=min(self.owner.profile.evidence_tokens,original.get('evidence_tokens',self.owner.profile.evidence_tokens))
        content=canonical(_guidance_view(contract)).decode()
        if type(offset) is not int or not 0<=offset<len(content):raise RepoError('CAPABILITY_PAGE_OFFSET')
        end=min(len(content),offset+1600)
        while end>offset:
            page={'contract_ref':ref,'route_id':offer['route_id'],'offset':offset,
                  'next_offset':end if end<len(content) else None,'content':content[offset:end],'authority':'ADVISORY_ONLY'}
            if estimate_tokens(page)<=budget:return page
            end=offset+(end-offset)//2
        raise RepoError('CAPABILITY_PAGE_BUDGET')

    def pending_bindings(self,run_id,route):
        """Small continuation handles for still-applicable, unbound offers.

        Full guidance remains one-shot. Stateless requests must still be able
        to bind after obtaining required file observations on a later turn.
        The current native route supplies applicability, not an old offer.
        """
        from .capability_binding import authorized,resolve
        run=self.owner.runtime.run(run_id)
        if (route['run_id']!=run_id or route['control_epoch']!=run['control_epoch']
                or route['release_digest']!=run['data']['manifest']['release_digest']):
            raise RepoError('CAPABILITY_ROUTE_MISMATCH')
        eligible=set(route.get('considered_contract_refs',[]))
        seen=set();pending=[]
        for offer in self.store.values('capability_offer'):
            if offer['run_id']!=run_id or not offer.get('offered'):continue
            for ref in offer['contract_refs']:
                if ref not in eligible or ref in seen:continue
                contract=self.store.get('capability_contract',ref)
                if not contract:continue
                original=self.store.get('capability_route',offer['route_id'])
                handle=next((g for g in original['guidance'] if g.get('view')=='REFERENCE_ONLY' and g['contract_ref']==ref),None)
                if not handle and not contract.get('binding_slots'):continue
                try:
                    authorized(self,run,offer,ref)
                    if contract.get('binding_slots') and resolve(self,run,offer,ref,contract):continue
                except RepoError:continue # Withdrawn evidence never becomes guidance.
                seen.add(ref)
                if handle:
                    if estimate_tokens(pending+[handle])<=self.owner.profile.evidence_tokens:pending.append(handle)
                    if len(pending)==2:return pending
                    continue
                pending.append({'contract_ref':ref,'route_id':offer['route_id'],
                    'title':contract['title'][:200],
                    'binding_slots':{k:{'kind':v['kind'],'purpose':v['purpose'][:180]}
                                     for k,v in contract['binding_slots'].items()},
                    'next':'If applicable, call skill.bind using current files actually read and a justified validation command. Binding grants no authority.'})
                if len(pending)==2:return pending
        return pending

    def mark_offered(self,route):
        """Called only after Host has built/admitted the actual model request."""
        current=self.store.get('capability_route',route['id'])
        previous=self.store.get('capability_offer',route['id'])
        if previous:return previous
        if current!= {k:v for k,v in route.items() if k!='replayed'}:raise RepoError('CAPABILITY_ROUTE_MISMATCH')
        from .session_experience import SessionExperiences
        active=SessionExperiences(self.owner.runtime).entries(route['run_id'])
        if any(active.get(entity,{}).get('session_grant_id')!=gid for entity,gid in route.get('session_grants',{}).items()):
            raise RepoError('SESSION_AUTHORIZATION_CHANGED')
        self.owner.repo.ensure_dev(route['source_revisions'])
        receipt={'route_id':route['id'],'run_id':route['run_id'],'release_digest':route['release_digest'],
            'contract_digest':route['contract_digest'],'offered':bool(route['guidance']),
            'selected':bool(route['guidance']),'control_epoch':route['control_epoch'],'selection_authority':'HOST_ROUTER',
            'applied':False,'verified':False,'injected_tokens':route['potential_tokens'],
            'token_accounting':'LOCAL_ESTIMATE_OF_GUIDANCE_SUFFIX','contract_refs':route['contract_refs'],'evidence':[],'grants_authority':False}
        with self.store.tx() as c:
            receipt['offer_sequence']=self.store.emit(c,route['run_id'],'capability.offered',{'route_id':route['id']},self.owner.runtime.run(route['run_id'])['data']['visibility'])
            self.store.put('capability_offer',route['id'],receipt,c,immutable=True)
        return receipt

    def receipt(self,run_id,contract_ref,*,route_id=None,historical=False):
        self.owner.repo.ensure_dev([contract_ref])
        contract=self.store.get('capability_contract',contract_ref)
        if not contract:raise RepoError('CAPABILITY_CONTRACT_MISSING')
        self.experiences.get(contract['skill_id'],contract['revision'])
        run=self.owner.runtime.run(run_id);events=self.store.events(run_id,trusted=True,limit=10000)
        if run['data']['visibility']!='DEV':raise RepoError('CAPABILITY_TRACE_DENIED')
        offer=self.store.get('capability_offer',route_id) if route_id else None
        if offer and (offer['run_id']!=run_id or contract_ref not in offer['contract_refs']):raise RepoError('CAPABILITY_OFFER_MISMATCH')
        binding_ref=None;binding_sequence=0;portable_missing=False
        if contract.get('binding_slots'):
            from .capability_binding import resolve
            try:bound=resolve(self,run,offer,contract_ref,contract)
            except RepoError:bound=None
            if bound:contract,binding_ref,binding_sequence=bound
            else:portable_missing=True
        # Natural-language steps do not prove execution. Record observed operations
        # for review; R6 may supply executable probe evidence, not model testimony.
        operations=[{'event_id':e['event_id'],'sequence':e['sequence'],'tool':e['data'].get('tool_name'),
            'operation_id':e['data'].get('operation_id'),
            'exit_code':(e['data'].get('result') or {}).get('exit_code')}
            for e in events if e['type']=='tool.operation_finished']
        bindings=[];used_operations=set();cursor=max(offer['offer_sequence'] if offer else 0,binding_sequence)
        if offer and contract.get('step_tools') and not portable_missing:
            with contextlib.closing(self.store.connect()) as c:
                for step_index,tool in enumerate(contract['step_tools']):
                    found=None
                    for op in operations:
                        if op['sequence']<=cursor or op['tool']!=tool or op['operation_id'] in used_operations:continue
                        row=c.execute("SELECT request,result FROM operations WHERE id=? AND run_id=? AND state='SUCCEEDED'",(op['operation_id'],run_id)).fetchone()
                        rule=(contract.get('step_rules') or [{}]*len(contract['steps']))[step_index]
                        args=json.loads(row['request'])['arguments'] if row else {}
                        matches=all(args.get(k)==v for k,v in rule.get('arguments_match',{}).items())
                        result=json.loads(row['result']) if row else {}
                        proof=self._diff_coverage_proof(run,rule,args,result) if row and not matches and tool=='workspace.diff' else None
                        if proof:matches=True
                        if result.get('truncated') and result.get('result_artifact'):
                            artifact=result['result_artifact']
                            result=self.store.read_json(artifact) if self.store.get('run_artifact',digest([run_id,artifact])) else {'is_error':True}
                        complete=not any(result.get(k) for k in ['is_error','timeout','cancelled','running'])
                        if tool=='process.run':complete=complete and result.get('exit_code')==0
                        if row and matches and complete:
                            found={**op,'equivalence_proof':proof} if proof else op;break
                    if not found:break
                    bindings.append(found);used_operations.add(found['operation_id']);cursor=found['sequence']
        applied=bool(offer and offer.get('control_epoch')==run['control_epoch'] and contract.get('step_rules') and len(bindings)==len(contract['steps']))
        verified=False;verification_refs=[]
        required=set(contract['required_probe_ids'])
        if applied and required:
            source=self.owner.runtime.workspace(run).snapshot()
            for decision in self.store.values('decision'):
                intent=self.store.get('intent',decision.get('intent_digest'))
                if not intent or intent.get('run_id')!=run_id or intent.get('source_digest')!=source or not decision.get('verified'):continue
                signature=decision.get('signature','')
                if not self.store.verify({k:v for k,v in decision.items() if k!='signature'},signature):continue
                evidence=[self.store.get('evidence',i) for i in decision.get('evidence_ids',[])]
                passed={e.get('check_id') for e in evidence if e and e.get('status')=='PASSED'}
                if required<=passed:
                    verified=True;verification_refs.append(decision['intent_digest'])
        result={'run_id':run_id,'skill_id':contract['skill_id'],'revision':contract['revision'],
            'release_digest':run['data']['manifest']['release_digest'],'contract_digest':run['data']['manifest']['contract_digest'],
            'capability_contract_ref':contract_ref,'route_id':route_id,'historical':historical,
            'offered':bool(offer and offer['offered']),'selected':bool(offer and offer['selected']),
            'applied':applied,'verified':verified,'verification_refs':verification_refs,'application_status':'OBSERVED_STEP_SEQUENCE' if applied else 'UNKNOWN_STEPS_NOT_BOUND_TO_PROBES',
            'step_evidence':bindings,'observed_operations':operations,'cost':run['data']['budget_used'],'cost_scope':'TOTAL_RUN_NOT_MARGINAL_SKILL_COST',
            'injected_tokens':offer['injected_tokens'] if offer else 0,'misfire':'UNKNOWN',
            'interpretation':'Observed tool calls are not proof that every natural-language step was executed.'}
        result['binding_ref']=binding_ref
        key=digest(result)
        with self.store.tx() as c:
            self.store.put('application_receipt',key,result,c,immutable=True)
            self.owner.repo.derive(key,[contract_ref,*[r['event_id'] for r in operations]],c)
        return key,result
