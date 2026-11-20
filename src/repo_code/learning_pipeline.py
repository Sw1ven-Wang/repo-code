"""Host-owned collection Draft -> existing promotion gates -> advisory reuse.

Validation data/providers are supplied by the trusted embedding. No dataset,
implicit approval, worker tool, Release activation or alternate validator.
"""
from .util import RepoError,digest,execution_lock


class LearningPipeline:
    REVISION='collection-learning-link-v1'

    def __init__(self,service,runner):
        self.service,self.store=service,service.store
        self.promotion=service.procedure_promotion(runner)

    async def curate_stages(self,collection_id,provider_factory,*,max_stages=4):
        """Process completed Host observations in order using the native Curator.

        This is an explicit batch boundary. Call it between task stages, then
        pass any resulting Drafts through advance() and its existing gates.
        """
        from .evolution import EvolutionEngine
        from .verified_stages import VerifiedStages
        if type(max_stages) is not int or not 1<=max_stages<=16:raise RepoError('LEARNING_BATCH_LIMIT')
        session=self.service.session_experiences().get(collection_id)
        stages=VerifiedStages(self.service.runtime)
        records=[]
        lock=self.store.root/'executor_locks'/('stage-curation-'+digest(collection_id)[7:]+'.lock')
        with execution_lock(lock):
            candidates=[stages.payload(row) for row in self.store.values(stages.KIND)
                        if row['payload']['collection']==collection_id]
            for stage in sorted(candidates,key=lambda x:x['sequence']):
                if self.store.get('l1_stage_curation',stage['id']):continue
                if stage['scope']!=session['scope']:raise RepoError('L1_STAGE_SCOPE')
                result={'candidate_id':stage['id'],'episode_ids':[],'reports':[]}
                episode_ids=stages.materialize(stage['id'],collection=collection_id)
                if episode_ids:
                    curator=EvolutionEngine(self.service,provider_factory(stage['id']))
                    for eid in episode_ids:
                        result['reports'].append(await curator.curate_episode(eid))
                reports=result['reports']
                status=('BLOCKED' if not episode_ids else 'CURATED' if all(r.get('status')=='COMPLETE' for r in reports)
                        else 'NEEDS_EVIDENCE' if any(r.get('status')=='INCOMPLETE' for r in reports) else 'NOT_CURATED')
                result.update(status=status,episode_ids=episode_ids)
                self.store.put('l1_stage_curation',stage['id'],result,immutable=True)
                records.append(result)
                if len(records)>=max_stages:break
        return {'collection':collection_id,'results':records,'reuse_authorized':False}

    async def advance(self,collection_id,planner_factory,reviewer_factory,validation_for,
                      *,draft_ids=None,max_drafts=1,base=None):
        """One durable attempt per Draft/base/collection; unknown dispatch stops.

        Factories receive only the draft ID (never private validation data).
        validation_for is a Host callback returning frozen support_tasks/probes.
        S2 may perform its required baseline/candidate trials. These are internal
        qualification, not an implicit formal benchmark A run.
        """
        if type(max_drafts) is not int or not 1<=max_drafts<=16:raise RepoError('LEARNING_BATCH_LIMIT')
        session=self.service.session_experiences().get(collection_id)
        base=base or self.service.releases.active()['digest']
        wanted=set(draft_ids) if draft_ids is not None else None
        drafts=[d for d in self.store.values('procedure_draft')
                if d['scope']==session['scope'] and (wanted is None or d['id'] in wanted)]
        if wanted is not None and wanted!={d['id'] for d in drafts}:raise RepoError('LEARNING_DRAFT_SCOPE_OR_SOURCE')
        from .verified_stages import VerifiedStages
        stages = VerifiedStages(self.service.runtime)
        def source_stage(draft):
            lesson = self.store.get('lesson', draft['lesson_ref'])
            episode = self.store.get('l1_episode', lesson['episode_id'])
            stage_id = episode.get('stage_candidate_id')
            if stage_id is None:
                return episode, None
            row = self.store.get(stages.KIND, stage_id)
            if not row:
                raise RepoError('LEARNING_DRAFT_SCOPE_OR_SOURCE')
            return episode, stages.payload(row)

        selected = []
        for draft in drafts:
            episode, stage = source_stage(draft)
            if stage is not None and stage['collection'] != collection_id:
                if wanted is not None:
                    raise RepoError('LEARNING_DRAFT_SCOPE_OR_SOURCE')
                continue
            if wanted is None:
                # Implicit learning only considers this collection's own completed curation.
                if stage is None or not self.store.get('l1_stage_curation', stage['id']):
                    continue
                result_key = digest([self.REVISION, collection_id, draft['id'], base])
                if self.store.get('learning_promotion_result', result_key):
                    continue
            selected.append((episode['event_range'][-1], draft['id'], draft))
        drafts = [draft for _, _, draft in sorted(selected)]
        results=[]
        for draft in drafts[:max_drafts]:
            # Native source validation precedes factories and every replay.
            self.promotion._source(draft['id'])
            plan=validation_for(draft)
            if not isinstance(plan,dict) or set(plan)!={'support_tasks','probes'} or not plan['support_tasks'] or not plan['probes']:
                raise RepoError('LEARNING_VALIDATION_INPUT_REQUIRED')
            key=digest([self.REVISION,collection_id,draft['id'],base])
            frozen={'revision':self.REVISION,'collection':collection_id,'draft_id':draft['id'],
                    'base':base,'validation_digest':digest(plan)}
            with execution_lock(self.store.root/'executor_locks'/('learning-'+key[7:]+'.lock')):
                previous=self.store.get('learning_promotion_input',key)
                if previous and previous!=frozen:raise RepoError('LEARNING_VALIDATION_PLAN_CHANGED')
                self.store.put('learning_promotion_input',key,frozen,immutable=True)
                done=self.store.get('learning_promotion_result',key)
                if done:
                    results.append(done);continue
                if self.store.get('learning_promotion_dispatch',key):
                    raise RepoError('LEARNING_PROMOTION_RECOVERY_REQUIRED')
                planner=planner_factory(draft['id']);reviewer=reviewer_factory(draft['id'])
                if planner is reviewer:raise RepoError('PROMOTION_INDEPENDENT_REVIEW_REQUIRED')
                self.store.put('learning_promotion_dispatch',key,{'state':'DISPATCHING','draft_id':draft['id']},immutable=True)
                # All semantic review, source/quote checking, executable probes,
                # isolated S1/S2 and session grant issuance remain native.
                result=await self.promotion.run(draft['id'],planner,reviewer,
                    support_tasks=plan['support_tasks'],probes=plan['probes'],
                    session_id=collection_id,base=base)
                record={'id':key,'draft_id':draft['id'],'collection':collection_id,
                        'result':result,'release_authorized':False}
                self.store.put('learning_promotion_result',key,record,immutable=True)
                results.append(record)
        return {'collection':collection_id,'attempted':len(results),'results':results,
                'session_ready':sum(r['result']['status']=='SESSION_READY' for r in results),
                'release_authorized':False}
