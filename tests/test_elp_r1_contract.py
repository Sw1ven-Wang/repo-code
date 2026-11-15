import copy
import pytest
from repo_code.commands import command_evidence
from repo_code.execution_evidence import bind_execution,valid_execution
from repo_code.storage import Store
from repo_code.cost_metrics import trial_cost,aggregate_cost,compare_cost


@pytest.mark.parametrize('code',['assert True','if False:\n assert False',
    'try:\n assert False\nexcept: pass','text="assert False"'])
def test_unbound_text_is_not_runtime_verification(code):
    result=command_evidence(['python','-c',code],{'exit_code':0},strict=True)
    assert not result['verification'] and result['outcome']!='PASSED'


def fixture(tmp_path):
    store=Store(tmp_path/'state')
    run={'id':'run-1','control_epoch':2,'data':{'manifest':{'release_digest':'release','contract_digest':'contract'}}}
    args={'argv':['python','-'],'stdin':'assert observed == 7'}
    body={'exit_code':0,'relevant_state':'source','output_snapshot':'source',
          'environment_digest':'env','checked_probe':'probe','assertions_completed':True,'stdout':'','stderr':''}
    body['execution_evidence']=bind_execution(store,run,'operation',args,['python','-c','host generated'],'',body,'probe')
    return store,run,args,body


def test_signed_binding_persists_and_checks_complete_identity(tmp_path):
    store,run,args,body=fixture(tmp_path)
    kwargs={'run':run,'operation_id':'operation','approved_epoch':2}
    assert valid_execution(store,body,args['argv'],args['stdin'],**kwargs)
    store=Store(tmp_path/'state')
    evidence=command_evidence(args['argv'],body,args['stdin'],strict=True,store=store,**kwargs)
    assert evidence['outcome']=='PASSED' and evidence['trusted_binding']
    for field,value in [('control_epoch',3),('id','other')]:
        other={**run,field:value}
        assert not valid_execution(store,body,args['argv'],args['stdin'],run=other)
    for field in ['release_digest','contract_digest']:
        other=copy.deepcopy(run);other['data']['manifest'][field]='wrong'
        assert not valid_execution(store,body,args['argv'],args['stdin'],run=other)
    assert not valid_execution(store,body,args['argv'],args['stdin'],operation_id='wrong')
    assert not valid_execution(store,body,args['argv'],args['stdin'],approved_epoch=1)
    assert not valid_execution(store,body,args['argv'],'assert observed == 8')


@pytest.mark.parametrize('field,value',[('timeout',True),('cancelled',True),('running',True),
    ('executed',False),('assertions_completed',False),('exit_code',1),('environment_digest','other'),
    ('relevant_state','changed'),('output_snapshot','changed'),('stdout','forged'),('stderr','forged')])
def test_incomplete_modified_or_stale_result_is_not_passing(tmp_path,field,value):
    store,run,args,body=fixture(tmp_path)
    body[field]=value
    assert not valid_execution(store,body,args['argv'],args['stdin'],run=run)


def test_signature_and_registration_cannot_be_forged(tmp_path):
    store,run,args,body=fixture(tmp_path)
    bad=copy.deepcopy(body);bad['execution_evidence']['data']['control_epoch']=0
    assert not valid_execution(store,bad,args['argv'],args['stdin'])
    with store.tx() as c:c.execute("DELETE FROM objects WHERE kind='process_check'")
    assert not valid_execution(store,body,args['argv'],args['stdin'])


def test_exit_zero_before_assertions_does_not_produce_valid_proof(tmp_path):
    store,run,args,body=fixture(tmp_path)
    body['assertions_completed']=False
    body['execution_evidence']=bind_execution(store,run,'operation',args,['python','-c','exit(0)'],'',body,'probe')
    assert not valid_execution(store,body,args['argv'],args['stdin'])


def test_unknown_attempt_cannot_be_hidden_by_later_success():
    rows=[{'status':'FAILED','reservation':200,'purpose':'main'},
          {'status':'COMPLETED','settled_tokens':30,'purpose':'auxiliary','usage':{'input_tokens':20,'output_tokens':10}}]
    result=trial_cost({'cost':{'tool_calls':3},'wall_seconds':1.0},rows)
    assert result['model_attempts']==2 and result['successful_responses']==1
    assert result['auxiliary_calls']==1 and result['unknown_usage']==1
    assert result['unknown_reservation']==200 and not result['cost_settled']
    assert result['actual_cost'] is None


def test_resources_are_not_interchangeable_and_wall_jitter_is_not_gain():
    old=aggregate_cost([{'cost':{'model_calls':10,'tool_calls':2},
                         'usage':{'input_tokens':100,'output_tokens':10},'wall_seconds':10}])
    new={**old,'model_attempts':1,'tool_calls':11,'wall_seconds':1}
    assert not compare_cost(old,new,2)['bounded']
    assert not compare_cost(old,{**old,'wall_seconds':1},2)['improved']
    assert compare_cost(old,{**old,'input_tokens':90},2)['improved']
    unknown=aggregate_cost([{'cost':{'model_calls':1,'tool_calls':1}}])
    assert unknown['input_tokens'] is None and not unknown['cost_settled']


@pytest.mark.asyncio
async def test_completed_review_is_not_rejected_at_call_limit(tmp_path):
    from test_review_workflow import setup,tool
    app,run=setup(tmp_path)
    try:
        await tool(app,run,'create',{'path':'fixed.py','content':'value=1\n'},'write')
        requested,reason=app.agent._candidate_review(app.runtime.run(run['id']),None)
        assert requested and reason is None
        review=app.store.get('candidate_review',run['id'])
        await tool(app,run,'process.run',{'argv':['pytest','-k','boundary']},'check')
        again,reason=app.agent._candidate_review(app.runtime.run(run['id']),review,limit_reached=True)
        assert not again and reason is None
        assert app.store.get('candidate_review',run['id'])['status']=='NEW_CHECK_PASSED'
    finally:app.close()
