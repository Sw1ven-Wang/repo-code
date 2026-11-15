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
