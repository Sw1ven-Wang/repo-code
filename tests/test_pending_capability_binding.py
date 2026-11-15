"""A one-shot offer remains usable after evidence reads, without re-offering."""
import json
import pytest
from repo_code.capability_binding import bind
from repo_code.util import RepoError
from test_portable_capability import prepared,args
from test_review_workflow import tool


@pytest.mark.asyncio
async def test_pending_handle_survives_read_and_stops_on_binding_before_application(tmp_path):
    app,run,router,first=await prepared(tmp_path)
    try:
        route=router.route(run['id'],'RETENTION_RULE',facts={'task_family:retention':True})
        # Use the exact originally admitted route facts; a cached offer is empty
        # as full guidance, while its native applicability is still inspectable.
        route={**first,'guidance':[]}
        pending=router.pending_bindings(run['id'],route)
        assert len(pending)==1 and 'steps' not in pending[0]
        assert pending[0]['contract_ref']==first['contract_refs'][0]
        assert pending[0]['route_id']==first['id']
        await tool(app,run,'read',{'path':'a.py'},'observe')
        assert router.pending_bindings(run['id'],route)==pending
        result=bind(router,run['id'],**args(first,'a.py'))
        assert result['steps'] and result['bound_step_rules'][0]['arguments_match']=={'path':'a.py'}
        assert not result['grants_authority']
        assert not router.pending_bindings(run['id'],route)
        _,receipt=router.receipt(run['id'],first['contract_refs'][0],route_id=first['id'])
        assert not receipt['applied'] # Binding is not execution.
        await tool(app,run,'read',{'path':'a.py'},'execute')
        _,receipt=router.receipt(run['id'],first['contract_refs'][0],route_id=first['id'])
        assert receipt['applied'] and not receipt['verified']
        assert len(app.store.values('capability_offer'))==1
    finally:app.close()


@pytest.mark.asyncio
async def test_inapplicable_revoked_cross_run_and_stale_control_never_refill(tmp_path):
    app,run,router,route=await prepared(tmp_path)
    try:
        assert not router.pending_bindings(run['id'],{**route,'considered_contract_refs':[]})
        assert not router.pending_bindings(run['id'],router.route(run['id'],'RETENTION_RULE',facts={'source_changed':True}))
        other=app.new_task('Unrelated',app.store.root.parent/'source')
        with pytest.raises(RepoError,match='ROUTE_MISMATCH'):
            router.pending_bindings(other['id'],route)
        with pytest.raises(RepoError,match='ROUTE_MISMATCH'):
            router.pending_bindings(run['id'],{**route,'control_epoch':999})
        router.owner.repo.revoke(route['source_revisions'][0])
        assert not router.pending_bindings(run['id'],route)
    finally:app.close()


@pytest.mark.asyncio
async def test_bad_path_value_identifies_slot_without_binding(tmp_path):
    app,run,router,route=await prepared(tmp_path)
    try:
        request=args(route,'a.py');request['values']['implementation']='user task requirements'
        _,result=await tool(app,run,'skill.bind',request,'bad-path')
        assert result['status']=='FAILED'
        assert result['result']['code']=='CAPABILITY_BINDING_PATH_EVIDENCE'
        assert 'implementation' in result['result']['message'] and 'relative file path' in result['result']['message']
        assert not app.store.values('capability_binding')
    finally:app.close()


@pytest.mark.asyncio
async def test_argv_string_is_known_input_rejection_and_correct_array_can_retry(tmp_path):
    from test_portable_capability import SLOTS,RULES
    slots={**SLOTS,'verification':{'kind':'argv','purpose':'Current source-based validation command with arguments.'}}
    rules=[RULES[0],{'tool':'process.run','arguments_match':{'argv':{'slot':'verification'}}}]
    app,run,router,route=await prepared(tmp_path,slots,rules)
    try:
        await tool(app,run,'read',{'path':'a.py'},'observe')
        request=args(route,'a.py');request['values']['verification']='python -m pytest -q'
        request['evidence']['verification']=['a.py']
        _,result=await tool(app,run,'skill.bind',request,'bad-argv')
        assert result['status']=='FAILED' and result['result']['code']=='CAPABILITY_BINDING_ARGV_TYPE'
        assert 'verification' in result['result']['message'] and 'JSON array' in result['result']['message']
        assert app.runtime.run(run['id'])['state']!='NEEDS_RECONCILIATION'
        assert not app.store.values('capability_binding')
        request['values']['verification']=['python','-m','pytest','-q']
        _,fixed=await tool(app,run,'skill.bind',request,'correct-argv')
        assert fixed['status']=='SUCCEEDED'
        _,receipt=router.receipt(run['id'],route['contract_refs'][0],route_id=route['id'])
        assert not receipt['applied'] # No retroactive action or automatic approval.
    finally:app.close()
