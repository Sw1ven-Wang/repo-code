"""Native-stage linkage contracts; scripted validation never authorizes reuse."""
import copy
import pytest
from repo_code.util import RepoError,digest
from repo_code.learning_pipeline import LearningPipeline
from test_procedure_promotion import fixture,Runner,model


@pytest.mark.asyncio
async def test_collection_invokes_native_review_s1_s2_and_replays_without_calls(tmp_path):
    app,d,t,probes,plan,review=await fixture(tmp_path)
    try:
        sid=app.session_experiences().create_collection(d['scope'],'native-link')
        runner=Runner();flow=app.learning_pipeline(runner);created=[]
        def factory(value):
            def make(did):created.append(did);return model(value)
            return make
        inputs={'support_tasks':{t['id']:t},'probes':probes}
        result=await flow.advance(sid,factory(plan),factory(review),lambda _:inputs,draft_ids=[d['id']])
        assert runner.calls==2 and len(created)==2
        assert app.store.values('procedure_promotion_input') and app.store.values('skill_s1') and app.store.values('skill_s2')
        assert result['results'][0]['result']['reason']=='SESSION_REAL_MODEL_EVIDENCE_REQUIRED'
        assert not app.store.values('session_grant') and not result['release_authorized']
        assert await flow.advance(sid,factory(plan),factory(review),lambda _:inputs,draft_ids=[d['id']])==result
        assert runner.calls==2 and len(created)==2
        changed=copy.deepcopy(inputs);changed['probes'][0]['expected_behavior']='Changed after dispatch'
        with pytest.raises(RepoError,match='PLAN_CHANGED'):
            await flow.advance(sid,factory(plan),factory(review),lambda _:changed,draft_ids=[d['id']])
    finally:app.close()


@pytest.mark.asyncio
async def test_batch_boundaries_and_independent_reviewer_are_enforced(tmp_path):
    app,d,t,probes,plan,review=await fixture(tmp_path)
    try:
        sid=app.session_experiences().create_collection(d['scope'],'bounds')
        flow=app.learning_pipeline(Runner())
        def forbidden(_):raise AssertionError('Unexpected factory')
        for limit in [0,17,True,1.5]:
            with pytest.raises(RepoError,match='BATCH_LIMIT'):
                await flow.advance(sid,forbidden,forbidden,lambda _:None,max_drafts=limit)
        shared=model(plan)
        with pytest.raises(RepoError,match='INDEPENDENT_REVIEW_REQUIRED'):
            await flow.advance(sid,lambda _:shared,lambda _:shared,
                lambda _:{'support_tasks':{t['id']:t},'probes':probes},draft_ids=[d['id']])
        assert not app.store.values('learning_promotion_dispatch')
        assert not app.store.values('candidate')
    finally:app.close()


@pytest.mark.asyncio
async def test_interrupted_native_stage_is_durable_and_never_implicitly_retried(tmp_path):
    app,d,t,probes,plan,review=await fixture(tmp_path)
    try:
        sid=app.session_experiences().create_collection(d['scope'],'interrupted')
        flow=app.learning_pipeline(Runner());calls=[]
        async def interrupted(*args,**kwargs):
            calls.append('dispatch');raise RepoError('SIMULATED_UNKNOWN_EXTERNAL_OUTCOME')
        flow.promotion.run=interrupted
        validation=lambda _:{'support_tasks':{t['id']:t},'probes':probes}
        with pytest.raises(RepoError,match='UNKNOWN_EXTERNAL_OUTCOME'):
            await flow.advance(sid,lambda _:model(plan),lambda _:model(review),validation,draft_ids=[d['id']])
        assert not app.store.values('learning_promotion_result')
        # Reconstructing the public API does not erase the durable fence.
        flow=app.learning_pipeline(Runner())
        def forbidden(_):raise AssertionError('Unknown dispatch was retried')
        with pytest.raises(RepoError,match='RECOVERY_REQUIRED'):
            await flow.advance(sid,forbidden,forbidden,validation,draft_ids=[d['id']])
        assert calls==['dispatch']
    finally:app.close()


@pytest.mark.asyncio
async def test_scope_missing_validation_and_unknown_dispatch_do_not_call_models(tmp_path):
    app,d,t,probes,plan,review=await fixture(tmp_path)
    try:
        sid=app.session_experiences().create_collection(d['scope'],'native-link')
        other=app.session_experiences().create_collection('other-project','native-link')
        flow=LearningPipeline(app,Runner())
        def forbidden(_):raise AssertionError('Unexpected model factory')
        with pytest.raises(RepoError,match='SCOPE_OR_SOURCE'):
            await flow.advance(other,forbidden,forbidden,lambda _:None,draft_ids=[d['id']])
        with pytest.raises(RepoError,match='VALIDATION_INPUT_REQUIRED'):
            await flow.advance(sid,forbidden,forbidden,lambda _:None,draft_ids=[d['id']])
        base=app.releases.active()['digest']
        key=digest([flow.REVISION,sid,d['id'],base])
        app.store.put('learning_promotion_dispatch',key,{'state':'DISPATCHING','draft_id':d['id']},immutable=True)
        with pytest.raises(RepoError,match='RECOVERY_REQUIRED'):
            await flow.advance(sid,forbidden,forbidden,lambda _:{'support_tasks':{t['id']:t},'probes':probes},draft_ids=[d['id']])
        assert not app.store.values('procedure_promotion_input')
    finally:app.close()


@pytest.mark.asyncio
async def test_host_stage_is_saved_before_batch_and_replayed_after_restart(tmp_path):
    from repo_code.service import RepoCode
    from repo_code.providers import ScriptedProvider
    from repo_code.episodes import EpisodeBuilder
    from repo_code.verified_stages import VerifiedStages
    from test_review_v151 import human_contract
    from test_curator_evidence_pipeline import provider

    root = tmp_path / 'state'
    app = RepoCode(root, ScriptedProvider(), 'local')
    collected = []
    try:
        collection = app.session_experiences().create_collection('project:stages', 'live')
        for number in range(2):
            folder = tmp_path / str(number)
            folder.mkdir()
            run = app.new_task('Check the task result', folder, human_contract(),
                               session_id=collection, task_family='stage-test')
            intent = app.runtime.prepare(run['id'])
            if number:
                app.store.put('human_acceptance', digest([run['id'], 'review', intent.source_digest]),
                              {'approved': True, 'contract': intent.contract_digest})
            decision = await app.grader.evaluate(intent, app.runtime.contract(run))
            assert not [row for row in app.store.values(VerifiedStages.KIND)
                        if row['payload']['run_id'] == run['id']]
            verdict = app.runtime.commit(intent, decision)
            assert verdict['status'] == ('COMMITTED' if number else 'NOT_VERIFIED')
            assert app.runtime.commit(intent, decision)['status'] == ('ALREADY_COMMITTED' if number else 'NOT_VERIFIED')
            if not number:
                app.runtime.end(run['id'], 'PARTIAL', 'Stopped after negative result')
            stage, = [row['payload'] for row in app.store.values(VerifiedStages.KIND)
                      if row['payload']['run_id'] == run['id']]
            assert stage['source_digest'] == intent.source_digest
            assert stage['outcome'] == ('DELIVERY_VERIFIED' if number else 'DELIVERY_NOT_VERIFIED')
            if not number:
                # Revocation applies before evidence can be expanded or dispatched.
                app.store.put('revocation', decision.evidence_ids[0], {'reason': 'Withdraw checked source'})
            collected.append(stage['id'])
        assert app.session_experiences().accumulation(collection)['stage_candidates'] == collected
        assert not app.store.values('l1_episode')
    finally:
        app.close()

    app = RepoCode(root, ScriptedProvider(), 'local')
    dispatched = []
    try:
        app.store.put('operator_config', 'curator_evidence_pipeline', {'procedure_draft_stage': True})
        app.store.put('operator_config', 'procedure_draft_seed', {'lessons': ['pre-loaded experience']})
        app.store.put('operator_config', 'learning_public_task_context', {'problem_statement': 'a different task'})
        flow = app.learning_pipeline(Runner())
        def curator(candidate_id):
            assert candidate_id == collected[1]
            def respond(payload, index):
                dispatched.append(candidate_id)
                assert payload['metadata']['verification_summary']['stage_outcome'] == 'DELIVERY_VERIFIED'
                assert 'reports' not in payload and not payload['evidence']
                assert 'seed_lessons' not in payload
                assert payload['metadata']['public_task_context'] is None
                return {'status': 'COMPLETE', 'claims': []}
            return provider(respond)
        first = await flow.curate_stages(collection, curator, max_stages=1)
        assert first['results'][0]['status'] == 'BLOCKED'
        assert not dispatched and not app.store.values('l1_episode')
        second = await flow.curate_stages(collection, curator)
        stage = second['results'][0]
        assert stage['reports'][0]['status']=='INCOMPLETE'
        assert stage['status'] == 'NEEDS_EVIDENCE' and not second['reuse_authorized']
        episode = EpisodeBuilder(app.store).get(stage['episode_ids'][0])
        assert episode['stage_candidate_id'] == collected[1]
        assert episode['selection_version'] == 'episode-v2'
        assert not app.store.values('session_grant')
        assert (await flow.curate_stages(collection, curator))['results'] == []
        assert dispatched == [collected[1]]
    finally:
        app.close()
