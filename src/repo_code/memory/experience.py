"""Deterministic consolidation over existing Lesson facts; no semantic auto-merge."""
from __future__ import annotations
import contextlib
import json
import time
from types import SimpleNamespace
from ..util import RepoError,digest,now,canonical
from ..episodes import EpisodeBuilder
from .models import ClaimRecord
from .retrieval import DynamicRetriever,applicability


class ExperienceConsolidation:
    def __init__(self,owner):
        self.owner=owner;self.repo=owner.repo;self.store=self.repo.store;self.profile=owner.profile

    def _source(self,lesson_id,c):
        lesson=self.store.get('lesson',lesson_id,c)
        if not lesson:raise RepoError('LESSON_NOT_FOUND')
        episode=EpisodeBuilder(self.store).get(lesson['episode_id'],c)
        self.repo.ensure_dev([lesson_id],c)
        claim=self.repo.get('memory_versions',lesson['claim_ref'],c)
        if claim is None or claim['lifecycle']=='REVOKED':raise RepoError('LESSON_CLAIM_MISSING')
        try:ClaimRecord.model_validate(claim)
        except ValueError:raise RepoError('EXPERIENCE_SOURCE_SCHEMA') from None
        return lesson,episode,claim

    def _current(self,entity_id,c):
        head=self.store.get('experience_head',entity_id,c)
        if not head:raise RepoError('EXPERIENCE_NOT_FOUND')
        return self.get(entity_id,head['revision'],c)

    def get(self,entity_id,revision=None,c=None):
        if c is None:
            with self.store.tx() as conn:return self.get(entity_id,revision,conn)
        if revision is None:return self._current(entity_id,c)
        ref=self.store.get('experience_version',digest([entity_id,revision]),c)
        if not ref:raise RepoError('EXPERIENCE_VERSION_MISSING')
        value=self.store.get('experience_revision',ref['digest'],c)
        if not value or digest({k:v for k,v in value.items() if k!='revision_digest'})!=ref['digest']:raise RepoError('EXPERIENCE_CORRUPT')
        self.repo.ensure_dev([ref['digest']],c)
        for source in value['support']:self._source(source['lesson_id'],c)
        return value

    @staticmethod
    def _description(lesson,episode,claim):
        procedure=lesson.get('procedure')
        conditions={'scope':lesson['scope'],'task_family':lesson['task_family'],
            'preconditions':claim['preconditions'],'file_dependencies':claim['file_dependencies'],
            'triggers':procedure.get('triggers',[]) if procedure else [],
            'exclusions':procedure.get('exclusions',[]) if procedure else []}
        content={'category':lesson['category'],'text':lesson['text'],'procedure':procedure,'subject':lesson.get('subject','')}
        if lesson.get('procedure_draft'):content['procedure_draft']=lesson['procedure_draft']
        return content,conditions

    def consolidate(self,lesson_id,*,target_id=None,expected_revision=None):
        started=time.monotonic();input_id=digest([lesson_id,target_id,expected_revision])
        with self.store.tx() as c:
            lesson,episode,claim=self._source(lesson_id,c)
            previous=self.store.get('experience_input',input_id,c)
            if previous:
                self.get(previous['entity_id'],previous['revision'],c)
                return previous
            content,conditions=self._description(lesson,episode,claim)
            existing=[]
            for row in c.execute("SELECT id FROM objects WHERE kind='experience_head' ORDER BY id"):
                try:value=self._current(row[0],c)
                except (RepoError,FileNotFoundError):continue
                if value['conditions']['scope']==lesson['scope'] and value['content']['category']==lesson['category']:existing.append(value)
            if target_id:
                target=self._current(target_id,c)
                if target['conditions']['scope']!=lesson['scope'] or target['content']['category']!=lesson['category']:raise RepoError('EXPERIENCE_SCOPE_DENIED')
                if expected_revision is None or target['revision']!=expected_revision:raise RepoError('EXPERIENCE_HEAD_CAS')
                if target['content']['subject']!=content['subject']:raise RepoError('EXPERIENCE_SUBJECT_DENIED')
            else:target=None
            exact=next((v for v in existing if v['content']==content and v['conditions']==conditions),None)
            topic=[v for v in existing if content['subject'] and v['content']['subject']==content['subject']]
            related=target or next((v for v in topic if v['conditions']==conditions),None) or next(iter(topic),None)
            source={'lesson_id':lesson_id,'episode_id':episode['episode_id'],'task_id':episode['task_id'],
                'task_family':episode['task_family'],'quote_ids':lesson['quote_ids']}
            if claim.get('evidence_schema_version')==2:
                source['evidence_digest']=digest({k:claim.get(k,[]) for k in ['support_refs','implementation_evidence','verification_evidence','contradiction_refs','context_refs']})
                source['positive_support']=bool(claim['support_refs'])
            manifest=self.owner.runtime.run(episode['run_id'],c)['data']['manifest']
            spec=self.store.read_json(manifest['task_spec_digest']) if manifest.get('task_spec_digest') else None
            source['task_identity']=digest([spec['goal'],manifest['contract_digest'],manifest['workspace_baseline_digest']]) if spec else None
            if exact and claim.get('contradiction_refs'):
                action='CONFLICT_REVIEW';old=None;related=exact
            elif exact:
                entity=exact['id'];old=exact
                same_evidence=not source['task_identity'] or any(s['task_id']==source['task_id'] or
                    s.get('task_identity')==source['task_identity'] for s in old['support'])
                evidence_changed=claim.get('evidence_schema_version')==2 and source.get('evidence_digest') not in {s.get('evidence_digest') for s in old['support']}
                action='EVIDENCE_REVISION' if same_evidence and evidence_changed else 'NOOP' if same_evidence else 'ADD_SUPPORT'
            elif related and related['conditions']!=conditions:
                action='SPLIT';old=None
            elif target:
                action='REVISE';old=target
            elif related:
                action='CONFLICT_REVIEW';old=None
            else:action='NEW';old=None
            if action=='NOOP':
                revision=old['revision'];entity=old['id'];revision_digest=old['revision_digest']
            else:
                entity=old['id'] if old else 'exp-'+digest([content,conditions,claim.get('contradiction_refs',[])])[7:31]
                revision=old['revision']+1 if old else 1
                if old:
                    accumulating=action in {'ADD_SUPPORT','EVIDENCE_REVISION'}
                    support=old['support']+[source] if accumulating else [source]
                    base=self.repo.get('memory_versions',old['claim_ref'],c)
                    merged_refs=list({digest(r):r for r in base['support_refs']+claim['support_refs']}.values()) if accumulating else claim['support_refs']
                    independent=sorted({s['task_id'] for s in support if s.get('positive_support',True)})
                    next_claim={**claim,'claim_id':base['claim_id'],'version':base['version']+1,
                        'support_refs':merged_refs,'independent_support':independent,
                        'derived_from':sorted(set(base['derived_from']+claim['derived_from'])) if accumulating else claim['derived_from'],
                        'lifecycle':'CANDIDATE'}
                    if claim.get('evidence_schema_version')==2 and accumulating:
                        # Curator evidence revisions and canonical experience revisions own distinct streams.
                        next_claim.update(claim_id='experience-claim-'+entity,version=old['revision']+1)
                        for field in ['implementation_evidence','verification_evidence','contradiction_refs','context_refs']:
                            next_claim[field]=list({digest(r):r for r in base.get(field,[])+claim.get(field,[])}.values())
                    ClaimRecord.model_validate(next_claim)
                    count=c.execute('SELECT count(*) FROM memory_versions').fetchone()[0]
                    if count>=self.profile.max_items:raise RepoError('MEMORY_GROWTH_BUDGET')
                    claim_ref=next_claim['claim_id']+':'+str(next_claim['version'])
                    self.repo.put('memory_versions',claim_ref,next_claim,c)
                    published_head=self.repo.get('memory_control','claim:'+next_claim['claim_id'],c)
                    if not published_head or published_head['lifecycle']!='ACTIVE':
                        self.repo.put('memory_control','claim:'+next_claim['claim_id'],{'version':next_claim['version'],'lifecycle':'CANDIDATE'},c,False)
                    self.repo.derive('memory-version:'+claim_ref,[*next_claim['derived_from']],c)
                else:support=[source];claim_ref=lesson['claim_ref']
                known_families={s['task_family'] for s in support if s.get('positive_support',True) and s['task_family'] not in {'UNSPECIFIED','UNKNOWN','',None}}
                value={'id':entity,'revision':revision,'content':content,'conditions':conditions,
                    'support':support,'task_ids':sorted({s['task_id'] for s in support}),
                    'independent_family_count':len(known_families),'independent_validation_count':0,
                    'applicability_limit':'SINGLE_TASK','claim_ref':claim_ref,'status':'CANDIDATE',
                    'counter_refs':sorted(set(claim.get('contradiction_refs',[]) and [lesson_id] or [])),
                    'previous_revision':old['revision_digest'] if old else None,
                    'source_release_digests':sorted({EpisodeBuilder(self.store).get(s['episode_id'],c)['release_digest'] for s in support}),
                    'automatic_merge':False,'activation_authority':'NONE'}
                revision_digest=digest(value);value['revision_digest']=revision_digest
                self.store.put('experience_revision',revision_digest,value,c,immutable=True)
                self.store.put('experience_version',digest([entity,revision]),{'digest':revision_digest},c,immutable=True)
                self.store.put('experience_head',entity,{'revision':revision,'digest':revision_digest},c)
                self.repo.derive(revision_digest,[*[s['lesson_id'] for s in support],*([old['revision_digest']] if old else [])],c)
                if related and action in {'SPLIT','CONFLICT_REVIEW'}:
                    relation={'from':revision_digest,'to':related['revision_digest'],'relation':action,
                        'counter_evidence_refs':[lesson_id] if action=='CONFLICT_REVIEW' else [],'automatic_merge':False}
                    self.repo.put('memory_relations',digest(relation),relation,c)
                    if action=='CONFLICT_REVIEW':
                        for affected in [entity,related['id']]:
                            self.store.put('experience_conflict',digest([affected,revision_digest]),
                                {'entity_id':affected,'sources':[revision_digest,related['revision_digest']],'status':'NEEDS_REVIEW'},c,immutable=True)
            receipt={'lesson_id':lesson_id,'action':action,'entity_id':entity,'revision':revision,
                'revision_digest':revision_digest,'related_id':related['id'] if related else None,
                'seconds':time.monotonic()-started,'automatic_merge':False,'release_activated':False}
            self.store.put('experience_input',input_id,receipt,c,immutable=True)
            self.repo.derive(input_id,[lesson_id,revision_digest],c)
            return receipt

    def _eligible(self,scope,c):
        records=[];filters=[]
        for row in c.execute("SELECT id FROM objects WHERE kind='experience_head' ORDER BY id"):
            head=self.store.get('experience_head',row[0],c)
            raw=self.store.get('experience_revision',head['digest'],c)
            if raw and raw['conditions']['scope']!=scope:continue
            try:value=self._current(row[0],c)
            except (RepoError,FileNotFoundError) as exc:
                filters.append({'id':row[0],'reason':getattr(exc,'code','SOURCE_MISSING')});continue
            if value['conditions']['scope']!=scope:continue
            if value['status'] not in {'CANDIDATE','HISTORY','ACTIVE'}:continue
            records.append(value)
        return records,filters

    def search(self,query,*,purpose,scope=None,run_id=None,phase='DEBUG',environment=None,
               file_versions=None,facts=None,symbols=(),limit=6,token_budget=None,semantic=False):
        if purpose not in {'runtime','consolidation'}:raise RepoError('EXPERIENCE_PURPOSE')
        if purpose=='runtime' and not run_id:raise RepoError('EXPERIENCE_RUN_REQUIRED')
        started=time.monotonic();environment=environment or {};facts=facts or {};file_versions=file_versions or {}
        run=self.owner.runtime.run(run_id) if run_id else None
        if run:
            actual_scope=run['data']['manifest']['scope']
            if scope is not None and scope!=actual_scope:raise RepoError('EXPERIENCE_SCOPE_DENIED')
            scope=actual_scope
        if not scope:raise RepoError('EXPERIENCE_SCOPE_REQUIRED')
        with self.store.tx() as c:
            records=[];filters=[]
            entries={}
            release_digest=run['data']['manifest']['release_digest'] if run else None
            trial=False
            if purpose=='runtime':
                binding=self.store.get('capability_trial',run_id,c)
                if binding:
                    payload=binding['payload']
                    trial=self.store.verify(payload,binding['signature']) and payload['run_id']==run_id and payload['release']==release_digest and payload['scope']==scope and payload['environment']==self.owner.runtime.sandbox.environment_digest and payload['family']==run['data']['manifest'].get('task_family')
                from ..governance import Releases
                release=Releases(self.store,self.owner.runtime.sandbox).get(release_digest)
                self.repo.check([release_digest],c)
                published=trial or release.base_digest is None or bool(self.store.get('activated',release_digest,c))
                cfg=self.store.read_json(release.components['skills_index'])
                if published and cfg.get('kind')=='config':
                    entries={s['experience_id']:s for s in cfg.get('skills',[]) if isinstance(s,dict) and isinstance(s.get('experience_id'),str)}
                from ..session_experience import SessionExperiences
                session_entries=SessionExperiences(self.owner.runtime).entries(run_id,c)
                for entity,spec in session_entries.items():entries.setdefault(entity,spec)
                for entity,spec in entries.items():
                    revision=spec.get('revision')
                    if isinstance(revision,bool) or not isinstance(revision,int) or revision<1:
                        filters.append({'id':entity,'reason':'RELEASE_VERSION_MISMATCH'});continue
                    try:record=self.get(entity,revision,c)
                    except (RepoError,FileNotFoundError) as exc:
                        filters.append({'id':entity,'reason':getattr(exc,'code','SOURCE_MISSING')});continue
                    if record['conditions']['scope']==scope:records.append(record)
            else:records,filters=self._eligible(scope,c)
            selected=[]
            for record in records:
                cond=record['conditions'];spec=entries.get(record['id'])
                reason=None
                if purpose=='runtime':
                    if not spec or spec.get('status')!='ACTIVE':reason='UNAPPROVED_RELEASE_SKILL'
                    elif record['content']['category']!='PROCEDURE':reason='NOT_SKILL'
                    elif spec.get('revision')!=record['revision'] or spec.get('revision_digest')!=record['revision_digest']:reason='RELEASE_VERSION_MISMATCH'
                    elif spec.get('scope')!=scope or spec.get('task_family')!=cond['task_family']:reason='RELEASE_SCOPE_MISMATCH'
                    elif cond['task_family'] in {'UNSPECIFIED','UNKNOWN','',None} or run['data']['manifest'].get('task_family')!=cond['task_family']:reason='TASK_FAMILY_UNKNOWN_OR_MISMATCH'
                    elif record['applicability_limit']=='SINGLE_TASK' and run['task_id'] not in record['task_ids'] and not trial and not spec.get('session_grant_id'):reason='TASK_NOT_SUPPORTED'
                    elif spec.get('valid_until') is not None and spec['valid_until']<now():reason='EXPIRED'
                    elif not isinstance(spec.get('phases'),list) or phase not in spec['phases']:reason='PHASE_MISMATCH'
                    elif any(facts.get(t) is not True for t in cond['triggers']):reason='TRIGGER_FALSE_OR_UNKNOWN'
                    elif any(facts.get(t) is not False for t in cond['exclusions']):reason='EXCLUSION_TRUE_OR_UNKNOWN'
                    elif c.execute("SELECT 1 FROM objects WHERE kind='experience_conflict' AND json_extract(data,'$.entity_id')=?",(record['id'],)).fetchone():reason='CONFLICT_REVIEW'
                    else:
                        claim=self.repo.get('memory_versions',record['claim_ref'],c)
                        if claim['validity'] in {'STALE','CONTRADICTED'}:reason=claim['validity']
                        else:
                            status,why=applicability({'preconditions':cond['preconditions'],'file_dependencies':cond['file_dependencies'],
                                'valid_until':claim.get('valid_until')},SimpleNamespace(environment=environment),file_versions)
                            if status!='TRUE':reason=why
                if reason:filters.append({'id':record['id'],'reason':reason});continue
                selected.append({'id':record['id'],'revision':record['revision'],'revision_digest':record['revision_digest'],
                    'text':record['content']['text']+' '+canonical(record['content'].get('procedure') or {}).decode(),
                    'category':record['content']['category'],'scope':scope,'status':'ACTIVE' if purpose=='runtime' else record['status'],
                    'conditions':cond,'sources':[s['lesson_id'] for s in record['support']],
                    'applicability':'TRUE' if purpose=='runtime' else 'REVIEW_ONLY','grants_edit_authority':False})
        retriever=getattr(self.owner,'retriever',None) or DynamicRetriever(self.repo,self.profile)
        try:hits,trace=retriever.rank_experiences(selected,query,symbols=symbols,limit=limit,token_budget=token_budget,semantic=semantic)
        except RepoError as exc:
            audit={'purpose':purpose,'scope':scope,'run_id':run_id,'error':exc.code,
                'seconds':time.monotonic()-started,'selected':[],'injected_tokens':0,
                'dense_calls':None,'reranker_calls':None,'cost_status':'PARTIAL_FAILURE'}
            self.repo.put('retrieval_traces',digest(audit),audit)
            raise
        with self.store.tx() as c:
            # Rankers are outside the DB transaction. Revalidate after encoder work.
            fresh=[]
            for hit in hits:
                try:current=self.get(hit['id'],hit['revision'] if purpose=='runtime' else None,c)
                except (RepoError,FileNotFoundError) as exc:
                    filters.append({'id':hit['id'],'reason':getattr(exc,'code','SOURCE_MISSING')});continue
                if current['revision_digest']!=hit['revision_digest']:
                    filters.append({'id':hit['id'],'reason':'STALE_EXPERIENCE_REVISION'});continue
                if purpose=='runtime':
                    spec=entries[hit['id']]
                    if spec.get('session_grant_id'):
                        current_specs=SessionExperiences(self.owner.runtime).entries(run_id,c)
                        if current_specs.get(hit['id'])!=spec:
                            filters.append({'id':hit['id'],'reason':'SESSION_AUTHORIZATION_CHANGED'});continue
                    claim=self.repo.get('memory_versions',current['claim_ref'],c)
                    if any(t is not None and t<now() for t in [entries[hit['id']].get('valid_until'),claim.get('valid_until')]):
                        filters.append({'id':hit['id'],'reason':'EXPIRED'});continue
                    if c.execute("SELECT 1 FROM objects WHERE kind='experience_conflict' AND json_extract(data,'$.entity_id')=?",(hit['id'],)).fetchone():
                        filters.append({'id':hit['id'],'reason':'CONFLICT_REVIEW'});continue
                fresh.append(hit)
            hits=fresh
            if purpose=='runtime':
                current_run=self.owner.runtime.run(run_id,c)
                try:self.repo.check([release_digest],c)
                except RepoError:
                    hits=[];filters.append({'reason':'RELEASE_REVOKED'})
                if current_run['revision']!=run['revision']:
                    hits=[];filters.append({'reason':'STALE_RUNTIME_CONTEXT'})
            from ..context import estimate_tokens
            trace['ranked_tokens']=trace['selected_tokens']
            trace['selected_tokens']=sum(estimate_tokens(hit) for hit in hits)
            trace.update(purpose=purpose,scope=scope,run_id=run_id,release_digest=release_digest,
                filters=filters,selected=[h['id'] for h in hits],seconds=time.monotonic()-started)
            trace_id=digest(trace)
            self.repo.put('retrieval_traces',trace_id,trace,c)
            self.repo.derive(trace_id,[h['revision_digest'] for h in hits],c)
        return hits,trace_id
