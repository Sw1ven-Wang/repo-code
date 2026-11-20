import pytest

from repo_code.capabilities import CapabilityRouter
from repo_code.models import Assistant, Budget, ToolCall
from repo_code.providers import ScriptedProvider,model_configuration
from repo_code.util import RepoError,digest
from test_elp_experience import initial,learn,approved_fixture
from test_review_workflow import setup


@pytest.mark.asyncio
async def test_real_middle_anchor_miss_recovered_in_same_run(tmp_path):
    app,source,owner=await initial(tmp_path)
    goal='x '*3200+' RETENTION_RULE '+' y'*3200
    calls=[]
    def respond(system,messages,tools,index):
        calls.append((system,tools))
        if index==0:
            assert 'Host diagnostic turn' in system and tools==[]
            assert 'RETENTION_RULE' in str(messages) or 'RETENTION_RULE' in system
            return Assistant(text='{"query":"RETENTION_RULE"}',usage={'input_tokens':11})
        return Assistant(text='Finished',usage={'input_tokens':13})
    try:
        def change(value):
            value['procedure'].update(triggers=['task_family:editing-boundary'],exclusions=['run_cancelled'])
        prior,_,skill=await learn(app,owner,source,change=change)
        run,_=approved_fixture(app,owner,prior,skill,goal=goal,phases=['PLAN','DEBUG','EDIT','VERIFY','RESUME'])
        sid=app.session_experiences().create_collection(run['data']['manifest']['scope'],'B')
        app.session_experiences().bind(sid,run['id'],'editing-boundary')
        p=ScriptedProvider(handler=respond)
        app.agent.provider=app.provider=app.broker.provider=p
        with app.store.tx() as c:
            r=app.runtime.run(run['id'],c)
            r['data']['model_config']=model_configuration(p)
            r['data']['manifest']['model_config_digest']=digest(r['data']['model_config'])
            app.runtime.update(c,r,data=r['data'])
        result=await app.execute(run['id'])
        assert result['local_query_repair']['status']=='LOCAL_QUERY_VERIFIED'
        assert len(calls)==2 and 'Scoped decision guidance;' in calls[1][0]
        assert result['budget_used']['model_calls']==2 and result['usage']['input_tokens']==24
        receipt=app.store.get('local_query_receipt',run['id'])
        assert app.store.verify(receipt['payload'],receipt['signature']) and not receipt['payload']['reuse_authorized']
        assert app.runtime.current_goal(app.runtime.run(run['id']))==goal
        assert not app.store.values('session_grant') and not app.store.values('learning_promotion_result')
    finally:app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case,expected',[
    ('valid','QUERY_ACCEPTED_NO_MATCH'),('invalid','NO_PROPOSAL'),('tool','NO_PROPOSAL'),
    ('protected',None),('budget',None),('provider','DISPATCH_FAILED'),('without_session',None),
    ('changed','NOT_VERIFIED'),('stale','STALE_RESPONSE')])
async def test_optional_lookup_failure_is_contained(tmp_path,monkeypatch,case,expected):
    diagnostic_calls=[]
    def respond(system,messages,tools,index):
        if 'Host diagnostic turn' not in system:return Assistant(text='Finished')
        diagnostic_calls.append(index)
        assert not tools
        if case=='provider':raise RepoError('MODEL_ERROR')
        if case=='changed':app.runtime.workspace(run).path('a.py').write_text('new=1')
        if case=='stale':
            with app.store.tx() as c:
                app.runtime.update(c,app.runtime.run(run['id'],c),control=True)
        if case=='tool':return Assistant(text='{}',tool_calls=[ToolCall(id='bad',name='create',arguments={'path':'bad','content':'bad'})])
        return Assistant(text='{"query":"scope bypass"}' if case=='invalid' else '{"query":"lookup"}')
    app,_=setup(tmp_path,provider=ScriptedProvider(handler=respond))
    try:
        goal='Investigate lookup'
        base=tmp_path/'source'
        sid=app.session_experiences().create_collection('project:diagnostic','B') if case!='without_session' else None
        run=app.new_task(goal,base,session_id=sid,task_family='diag' if sid else None,
                         budget=Budget(model_calls=1 if case=='budget' else 5,tool_calls=4,child_calls=0))
        original=CapabilityRouter.route
        def route(self,rid,query,**opts):
            if 'search_query' not in opts:raise RepoError('SCOPE_DENIED' if case=='protected' else 'INVALID_MEMORY_QUERY')
            return original(self,rid,query,**opts)
        monkeypatch.setattr(CapabilityRouter,'route',route)
        report=await app.execute(run['id'])
        assert report.get('local_query_repair',{}).get('status')==expected
        assert len(diagnostic_calls)==int(expected is not None)
        assert not (app.runtime.workspace(run).root/'bad').exists() and not app.store.values('session_grant')
        if case=='protected':assert report['reason']=='SCOPE_DENIED' and report['budget_used']['model_calls']==0
        if case=='provider':
            assert report['reason']=='MODEL_ERROR' and report['budget_used']['model_calls']==1
            app.runtime.resume(run['id'])
            again=await app.execute(run['id'])
            assert again['reason']=='LOCAL_REPAIR_RECONCILIATION_REQUIRED'
            assert again['budget_used']['model_calls']==1 and diagnostic_calls==[0]
        if case=='budget':assert report['budget_used']['model_calls']==1
        if case in {'changed','stale'}:assert app.store.get('local_query_receipt',run['id']) is None
    finally:app.close()
