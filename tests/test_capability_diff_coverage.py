"""Current-source evidence equivalence; never wildcard tool parameter matching."""
import json
import pytest
from repo_code.capabilities import CapabilityRouter
from test_elp_experience import initial,learn,approved_fixture
from test_review_workflow import tool


async def fixture(tmp_path):
    app,run,owner=await initial(tmp_path)
    source,_,d=await learn(app,owner,run,change=lambda v:v['procedure'].update(steps=['Inspect implementation diff']))
    current,facts=approved_fixture(app,owner,source,d,step_rules=[{'tool':'workspace.diff','arguments_match':{'path':'a.py'}}])
    app.broker.authorize_run(current['id'],True,True,delegate=False)
    router=CapabilityRouter(owner);route=router.route(current['id'],'RETENTION_RULE',facts=facts)
    router.mark_offered(route)
    await tool(app,current,'read',{'path':'a.py'},'read')
    await tool(app,current,'edit',{'path':'a.py','old_text':'value=0\n'*120,'new_text':'value=1\n'},'edit')
    await tool(app,current,'create',{'path':'extra.py','content':'x=2\n'},'extra')
    await tool(app,current,'workspace.diff',{},'diff')
    with app.store.connect() as c:
        row=c.execute("SELECT request,result FROM operations WHERE run_id=? AND request LIKE '%workspace.diff%' ORDER BY created DESC",(current['id'],)).fetchone()
    return app,router,app.runtime.run(current['id']),route,json.loads(row['request'])['arguments'],json.loads(row['result'])


@pytest.mark.asyncio
async def test_complete_workspace_view_proves_file_step_and_persists(tmp_path):
    app,router,run,route,args,result=await fixture(tmp_path)
    try:
        ref,receipt=router.receipt(run['id'],route['contract_refs'][0],route_id=route['id'])
        assert receipt['applied'] and not receipt['verified']
        proof=receipt['step_evidence'][0]['equivalence_proof']
        assert proof['path']=='a.py' and 'extra.py' in proof['coverage_paths']
        assert proof['source_digest']==app.runtime.workspace(run).snapshot()
        assert app.store.get('application_receipt',ref)==receipt
        # A later edit invalidates the old inspection, even if its target file
        # stayed identical. A new complete inspection restores the evidence.
        await tool(app,run,'create',{'path':'later.py','content':'x=3\n'},'later')
        _,stale=router.receipt(run['id'],route['contract_refs'][0],route_id=route['id'])
        assert not stale['applied']
        await tool(app,run,'workspace.diff',{},'refresh')
        _,fresh=router.receipt(run['id'],route['contract_refs'][0],route_id=route['id'])
        assert fresh['applied']
    finally:app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['truncated','offset','next-page','missing-next','stale-source','epoch','writer',
    'baseline','missing-file','altered-diff','metadata-only','wrong-path','other-param','tool','error','no-progress','omitted-truncated','environment'])
async def test_equivalence_requires_visible_complete_current_evidence(tmp_path,fault):
    app,router,run,route,args,result=await fixture(tmp_path)
    try:
        rule={'tool':'workspace.diff','arguments_match':{'path':'a.py'}}
        assert router._diff_coverage_proof(run,rule,args,result)
        if fault=='truncated':result['truncated']=True
        elif fault=='offset':result['offset']=1
        elif fault=='next-page':result['next_offset']=999
        elif fault=='missing-next':result.pop('next_offset')
        elif fault=='stale-source':result['inspection_progress']['source_digest']='old'
        elif fault=='epoch':result['inspection_progress']['control_epoch']+=1
        elif fault=='writer':result['inspection_progress']['writer_epoch']+=1
        elif fault=='baseline':result['baseline']='old'
        elif fault=='missing-file':result['files']=[]
        elif fault=='altered-diff':result['diff']+='fake'
        elif fault=='metadata-only':result['files'][0]['text_diff']='BINARY_OR_LARGE_FILE_METADATA_ONLY'
        elif fault=='wrong-path':rule['arguments_match']['path']='missing.py'
        elif fault=='other-param':rule['arguments_match']['length']=1
        elif fault=='tool':rule['tool']='read'
        elif fault=='error':result['is_error']=True
        elif fault=='no-progress':result.pop('inspection_progress')
        elif fault=='omitted-truncated':result.pop('truncated')
        elif fault=='environment':result['inspection_progress']['environment_digest']='other'
        assert router._diff_coverage_proof(run,rule,args,result) is None
    finally:app.close()
