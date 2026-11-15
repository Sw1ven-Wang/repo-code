"""Offline protocol fixtures; synthetic validation is never a benefit claim."""
import pytest
from repo_code.util import RepoError,digest
from repo_code.capabilities import CapabilityRouter
from repo_code.session_experience import SessionExperiences
from test_procedure_promotion import fixture,Runner,model
from test_review_workflow import tool

async def prepared(tmp_path):
    app,d,t,p,plan,review=await fixture(tmp_path)
    flow=app.procedure_promotion(Runner())
    result=await flow.run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=p)
    key=digest([result['contract_ref'],result['candidate']])
    # Trusted protocol fixture only: production coordinator rejects scripted models.
    s2=app.store.get('skill_s2',key);s2['evidence_class']='MODEL_TASK_EVALUATION'
    app.store.put('skill_s2',key,s2)
    sessions=app.session_experiences();sid=sessions.create(d['scope'])
    gid=sessions.admit(sid,result['contract_ref'],result['candidate'])
    source=tmp_path/'followup';source.mkdir();(source/'main.py').write_text('value=42\n')
    run=app.new_task('Inspect transformation metadata',source,session_id=sid,task_family=d['task_family'])
    return app,flow,d,sessions,sid,gid,run,result

def route(flow,run,d):
    return CapabilityRouter(flow.owner).route(run['id'],'Inspect transformation metadata',
        facts={'task_family:'+d['task_family']:True,'run_cancelled':False})

@pytest.mark.asyncio
async def test_session_reuses_on_later_task_without_publishing_and_persists(tmp_path):
    app,flow,d,s,sid,gid,run,result=await prepared(tmp_path)
    try:
        active=app.releases.active();router=CapabilityRouter(flow.owner)
        r=route(flow,run,d);assert len(r['guidance'])==1
        assert r['guidance'][0]['authority']=='SESSION_ADVISORY_ONLY'
        router.mark_offered(r)
        await tool(app,run,'read',{'path':'main.py'},'actual-session-read')
        ref,receipt=router.receipt(run['id'],result['contract_ref'],route_id=r['id'])
        assert receipt['applied']
        s.feedback(run['id'],{'state':'SUCCEEDED','application_receipts':[ref]})
        assert app.store.values('session_feedback') and s.entries(run['id'])
        from repo_code.service import RepoCode
        from repo_code.providers import ScriptedProvider
        reopened=RepoCode(app.store.root,ScriptedProvider(),sandbox='local')
        try:assert reopened.session_experiences().entries(run['id'])==s.entries(run['id'])
        finally:reopened.close()
        assert not app.store.get('capability_trial',run['id'])
        assert app.releases.active()==active and not app.store.values('release_approval')
        assert not route(flow,run,d)['guidance'] # no repeated injection
        s.revoke(gid,'Counterexample invalidated the procedure')
        assert not route(flow,run,d)['guidance'] and not s.entries(run['id'])
        first=app.store.get('session_grant_state',gid);s.revoke(gid,'A later reason must not erase the first')
        assert app.store.get('session_grant_state',gid)==first
        assert app.store.values('session_feedback') # audit is retained
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['close','expire','revoke','signature','environment','scope','family','source','s2','missing-state'])
async def test_session_scope_lifecycle_and_evidence_fail_closed(tmp_path,fault):
    app,flow,d,s,sid,gid,run,result=await prepared(tmp_path)
    try:
        assert route(flow,run,d)['guidance']
        if fault=='close':s.close(sid)
        elif fault=='expire':
            row=app.store.get('experience_session',sid);row['expires']=0;app.store.put('experience_session',sid,row)
        elif fault=='revoke':s.revoke(gid,'No longer supported')
        elif fault=='signature':
            g=app.store.get('session_grant',gid);g['signature']='forged';app.store.put('session_grant',gid,g)
        elif fault in {'environment','scope','family'}:
            b=app.store.get('session_run',run['id']);b['payload'][fault]='different';b['signature']=app.store.sign(b['payload']);app.store.put('session_run',run['id'],b)
        elif fault=='source':flow.owner.repo.revoke(result['contract_ref'])
        elif fault=='s2':
            key=digest([result['contract_ref'],result['candidate']]);row=app.store.get('skill_s2',key);row['passed']=False;app.store.put('skill_s2',key,row)
        elif fault=='missing-state':
            with app.store.tx() as c:c.execute("DELETE FROM objects WHERE kind='session_grant_state' AND id=?",(gid,))
        assert not route(flow,run,d)['guidance']
    finally:app.close()

@pytest.mark.asyncio
async def test_cross_session_no_inheritance_and_failed_application_revokes(tmp_path):
    app,flow,d,s,sid,gid,run,result=await prepared(tmp_path)
    try:
        sid2=s.create(d['scope']);other=app.new_task('Inspect',tmp_path/'followup',session_id=sid2,task_family=d['task_family'])
        assert not route(flow,other,d)['guidance']
        r=route(flow,run,d);router=CapabilityRouter(flow.owner);router.mark_offered(r)
        await tool(app,run,'read',{'path':'main.py'},'actual-read')
        ref,_=router.receipt(run['id'],result['contract_ref'],route_id=r['id'])
        s.feedback(run['id'],{'state':'PARTIAL','application_receipts':[ref]})
        assert not s.entries(run['id'])
        assert app.store.get('session_grant_state',gid)['status']=='REVOKED'
        assert app.store.values('session_feedback')[0]['causal_benefit']=='UNKNOWN'
    finally:app.close()

@pytest.mark.asyncio
async def test_revoke_between_route_and_offer_is_denied(tmp_path):
    app,flow,d,s,sid,gid,run,result=await prepared(tmp_path)
    try:
        r=route(flow,run,d);s.revoke(gid,'Concurrent invalidation')
        with pytest.raises(RepoError,match='SESSION_AUTHORIZATION_CHANGED'):CapabilityRouter(flow.owner).mark_offered(r)
    finally:app.close()

@pytest.mark.asyncio
async def test_scripted_coordinator_cannot_mint_real_session_grant(tmp_path):
    app,d,t,p,plan,review=await fixture(tmp_path)
    try:
        sid=app.session_experiences().create(d['scope'])
        r=await app.procedure_promotion(Runner()).run(d['id'],model(plan),model(review),support_tasks={t['id']:t},probes=p,session_id=sid)
        assert r['reason']=='SESSION_REAL_MODEL_EVIDENCE_REQUIRED'
        assert not app.store.values('session_grant')
    finally:app.close()

@pytest.mark.asyncio
async def test_native_agent_injects_session_guidance_without_trial_release(tmp_path):
    from repo_code.providers import ScriptedProvider
    from repo_code.models import Assistant,ToolCall
    app,flow,d,s,sid,gid,unused,result=await prepared(tmp_path)
    seen=[]
    try:
        def reply(system,messages,tools,i):
            seen.append(system)
            return Assistant(text='Inspect' if i==0 else 'No changes required',
                tool_calls=[ToolCall(id='inspect',name='read',arguments={'path':'main.py'})] if i==0 else [],
                usage={'input_tokens':20,'output_tokens':5})
        p=ScriptedProvider(handler=reply);app.provider=app.agent.provider=app.broker.provider=p
        run=app.new_task('Inspect transformation metadata',tmp_path/'followup',session_id=sid,task_family=d['task_family'])
        report=await app.execute(run['id'])
        assert sum('SESSION_ADVISORY_ONLY' in x for x in seen)==1
        assert report['application_receipts'] and app.store.values('session_feedback')
        assert not app.store.get('capability_trial',run['id'])
    finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('wrong',[False,True])
async def test_pinned_plan_is_not_regenerated_and_still_requires_real_review(tmp_path,wrong):
    app,d,t,p,plan,review=await fixture(tmp_path)
    try:
        flow=app.procedure_promotion(Runner());sid=app.session_experiences().create(d['scope'])
        prepared={'plan':plan,'source_digest':'wrong' if wrong else digest(flow._source(d['id'])),
            'public_support_digest':digest([{k:t[k] for k in ['id','goal','files']}]),'origin':'unit protocol fixture; no real efficacy'}
        r=await flow.run(d['id'],None,model(review),support_tasks={t['id']:t},probes=p,session_id=sid,prepared_plan=prepared)
        assert r['reason']==('PREPARED_PLAN_SOURCE_BINDING' if wrong else 'SESSION_REAL_MODEL_EVIDENCE_REQUIRED')
        assert not app.store.values('session_grant')
        if not wrong:
            saved=next(x for x in app.store.values('procedure_promotion_step') if x['stage']=='PLAN')['result']
            assert saved['value']==plan and saved['model_calls']==0 and saved['provider']['adapter']=='HostPreparedPlan'
    finally:app.close()
