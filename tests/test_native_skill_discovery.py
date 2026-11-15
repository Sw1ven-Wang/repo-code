import pytest
from repo_code.capabilities import CapabilityRouter
from repo_code.capability_binding import bind
from test_elp_experience import initial,learn,approved_fixture
from test_portable_capability import SLOTS,RULES,args
from test_review_workflow import tool

async def fixture(tmp_path,offered=True):
    app,source,owner=await initial(tmp_path)
    def procedure(value):
        value['procedure'].update(triggers=['task_family:editing-boundary'],exclusions=['source_changed'],
                                  steps=['Inspect the current reconstruction implementation'])
    old,_,decision=await learn(app,owner,source,change=procedure)
    run,facts=approved_fixture(app,owner,old,decision,binding_slots=SLOTS,step_rules=RULES,
                              phases=['PLAN','DEBUG','EDIT','VERIFY','RESUME'])
    app.broker.authorize_run(run['id'],True,True,False)
    app.store.put('operator_config','capability_router',{'enabled':True})
    router=CapabilityRouter(owner)
    route=router.route(run['id'],'RETENTION_RULE',phase='PLAN',
                       facts={'task_family:editing-boundary':True,'source_changed':False,'run_cancelled':False})
    assert route['guidance']
    if offered:router.mark_offered(route)
    return app,run,router,route

@pytest.mark.asyncio
async def test_native_offered_capability_is_discoverable_even_with_empty_legacy_snapshot(tmp_path):
    app,run,router,route=await fixture(tmp_path)
    try:
        _,result=await tool(app,run,'skill.search',{'query':'RETENTION_RULE','top_k':1},'discover')
        assert result['status']=='SUCCEEDED',result
        matches=result['result']['matches']
        assert len(matches)==1 and matches[0]['kind']=='PORTABLE_DECISION_CAPABILITY'
        assert matches[0]['contract_ref']==route['contract_refs'][0] and matches[0]['route_id']==route['id']
        assert not matches[0]['grants_authority'] and not app.store.values('capability_binding')
        await tool(app,run,'read',{'path':'a.py'},'observe')
        bind(router,run['id'],**args(route,'a.py'))
        _,result=await tool(app,run,'skill.search',{'query':'RETENTION_RULE','top_k':1},'after-bind')
        assert not any(m.get('kind')=='PORTABLE_DECISION_CAPABILITY' for m in result['result']['matches'])
        _,receipt=router.receipt(run['id'],route['contract_refs'][0],route_id=route['id'])
        assert not receipt['applied'] # Discovery/binding do not manufacture actions.
    finally:app.close()

@pytest.mark.asyncio
async def test_knowledge_search_directs_to_native_skill_discovery_without_merging_oracles(tmp_path):
    app,run,router,route=await fixture(tmp_path)
    try:
        _,result=await tool(app,run,'memory.search',{'query':'RETENTION_RULE','top_k':2},'knowledge')
        assert result['status']=='SUCCEEDED' and 'skill.search' in result['result']['skill_search_hint']
        assert not result['result']['matches']
        assert not app.store.values('capability_binding')
    finally:app.close()

@pytest.mark.asyncio
async def test_unoffered_cross_run_and_revoked_capabilities_are_not_discovered(tmp_path):
    app,run,router,route=await fixture(tmp_path,offered=False)
    try:
        _,result=await tool(app,run,'skill.search',{'query':'RETENTION_RULE','top_k':2},'unoffered')
        assert not result['result']['matches'] and not app.store.values('capability_offer')
        router.mark_offered(route)
        other=app.new_task('RETENTION_RULE',app.store.root.parent/'source')
        _,result=await tool(app,other,'skill.search',{'query':'RETENTION_RULE','top_k':2},'other')
        assert not result['result']['matches']
        router.owner.repo.revoke(route['source_revisions'][0])
        _,result=await tool(app,run,'skill.search',{'query':'RETENTION_RULE','top_k':2},'revoked')
        assert result['result'].get('is_error') or not result['result']['matches']
    finally:app.close()

@pytest.mark.asyncio
async def test_host_protocol_survives_evidence_read_without_reinjecting_full_guidance(tmp_path):
    from repo_code.providers import ScriptedProvider,model_configuration
    from repo_code.agent import CodingAgent
    from repo_code.util import ProviderNotDispatched
    app,old,router,route=await fixture(tmp_path,offered=False)
    captured=[]
    def respond(system,messages,tools,count):
        captured.append(system)
        if len(captured)==1:
            return {'tool_calls':[{'id':'observe-current','name':'read','arguments':{'path':'a.py'}}]}
        raise ProviderNotDispatched('CONTEXT_CAPTURE_COMPLETE')
    p=ScriptedProvider(handler=respond)
    try:
        app.provider=app.broker.provider=p
        app.agent=CodingAgent(app.store,app.runtime,app.broker,app.grader,app.releases,p)
        run=app.runtime.create('RETENTION_RULE',app.store.root.parent/'source',old['data']['manifest']['release_digest'],
            task_id=old['task_id'],task_revision=3,expected_active=old['id'],scope=old['data']['manifest']['scope'],
            model_config=model_configuration(p))
        with app.store.tx() as c:
            current=app.runtime.run(run['id'],c);current['data']['manifest']['task_family']='editing-boundary'
            app.runtime.update(c,current,data=current['data'])
        app.broker.authorize_run(run['id'],True,True,False)
        await app.execute(run['id'])
        assert len(captured)==2
        assert all('Host capability-use protocol' in s for s in captured)
        assert 'Scoped decision guidance;' in captured[0] and 'Scoped decision guidance;' not in captured[1]
        assert 'awaiting evidence binding' in captured[1]
        assert not app.store.values('capability_binding')
    finally:app.close()
