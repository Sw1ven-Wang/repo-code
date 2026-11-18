"""Budget denial is a proved non-effect; completed Usage is not a reserve."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest
from repo_code.memory.budget import ContextBudgetCoordinator
from repo_code.context import input_token_scale
from repo_code.models import Budget,ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.util import RepoError


@pytest.mark.parametrize('kind',['tool','wall','task'])
@pytest.mark.asyncio
async def test_denied_operation_commits_non_dispatch_before_budget_exception(tmp_path,kind):
    source=tmp_path/'input';source.mkdir();(source/'a.txt').write_text('before')
    app=RepoCode(tmp_path/'state',ScriptedProvider(),'local')
    run=app.new_task('Budget fixture',source,budget=Budget(tool_calls=1),authorize_write=True)
    read=app.broker.register_call(run['id'],'read',ToolCall(id='r',name='read',arguments={'path':'a.txt'}))
    await app.broker.submit(read)
    if kind=='wall':
        with app.store.tx() as c:
            r=app.runtime.run(run['id'],c);r['data']['manifest']['budget']['tool_calls']=10;app.runtime.update(c,r,data=r['data'])
        app.runtime.active_seconds=lambda r:601
    if kind=='task':
        with app.store.tx() as c:
            r=app.runtime.run(run['id'],c);r['data']['manifest']['budget']['tool_calls']=10;app.runtime.update(c,r,data=r['data'])
            c.execute('UPDATE tasks SET total_calls=1000 WHERE id=?',(run['task_id'],))
    req=app.broker.register_call(run['id'],'denied',ToolCall(id='w',name='create',arguments={'path':'never.txt','content':'never'}))
    with pytest.raises(RepoError) as e:await app.broker.submit(req)
    assert e.value.code==('TASK_TOTAL_BUDGET' if kind=='task' else 'BUDGET_EXHAUSTED')
    receipt=app.broker.get(req.key)
    assert receipt['status']=='CANCELLED' and receipt['result']['executed'] is False
    assert await app.broker.submit(req)==receipt
    assert app.runtime.run(run['id'])['data']['budget_used']['tool_calls']==1
    assert not (app.runtime.workspace(run).root/'never.txt').exists()
    app.runtime.end(run['id'],'PARTIAL',e.value.code)
    assert app.runtime.run(run['id'])['state']=='PARTIAL'


@pytest.mark.asyncio
async def test_two_distinct_requests_contend_for_one_remaining_slot(tmp_path):
    source=tmp_path/'input';source.mkdir()
    app=RepoCode(tmp_path/'state',ScriptedProvider(),'local')
    run=app.new_task('Concurrent budget fixture',source,budget=Budget(tool_calls=1),authorize_write=True)
    calls=[app.broker.register_call(run['id'],'t',ToolCall(id=str(i),name='create',arguments={'path':str(i)+'.txt','content':'once'})) for i in range(2)]
    results=await asyncio.gather(*(app.broker.submit(c) for c in calls),return_exceptions=True)
    assert sum(isinstance(x,RepoError) and x.code=='BUDGET_EXHAUSTED' for x in results)==1
    assert sorted(app.broker.get(c.key)['status'] for c in calls)==['CANCELLED','SUCCEEDED']
    assert len(list(app.runtime.workspace(run).root.glob('*.txt')))==1
    assert not app.runtime.unresolved(run['id'])


@pytest.mark.asyncio
async def test_real_agent_stops_partial_after_multi_tool_budget_without_reconciliation(app,source):
    app.provider.responses.extend([{'tool_calls':[
        {'id':'first','name':'create','arguments':{'path':'one.txt','content':'one'}},
        {'id':'second','name':'create','arguments':{'path':'two.txt','content':'two'}}]}])
    run=app.new_task('Create two files',source,budget=Budget(model_calls=4,tool_calls=1),authorize_write=True)
    report=await app.execute(run['id'])
    assert report['state']=='PARTIAL' and report['reason']=='BUDGET_EXHAUSTED'
    assert not app.runtime.unresolved(run['id'])
    assert (app.runtime.workspace(run).root/'one.txt').exists()
    assert not (app.runtime.workspace(run).root/'two.txt').exists()


def test_archived_11_token_reserve_excess_preserves_valid_completed_reply_and_tightens_estimate():
    p=SimpleNamespace(model='deepseek-flash',base_url='https://api.deepseek.com')
    coordinator=ContextBudgetCoordinator(p,16000,4096,500)
    b=replace(coordinator.measure('',[],[]),estimated_input_total=11403,raw_estimated_input_total=9502)
    result=coordinator.audit_usage({'prompt_tokens':11415,'completion_tokens':87},b)
    assert result['status']=='SAFETY_MARGIN_USED' and not result['hard_violation']
    assert result['allocation_excess']==11 and result['actual_total']==11502
    assert input_token_scale(p)>1.2
    assert ContextBudgetCoordinator(p,16000,4096,500).scale==result['next_estimate_scale']


@pytest.mark.parametrize('usage,status',[({'prompt_tokens':15900,'completion_tokens':101},'HARD_WINDOW_EXCEEDED'),
    ({'prompt_tokens':1000,'completion_tokens':4097},'OUTPUT_LIMIT_EXCEEDED'),
    ({'prompt_tokens':12500,'completion_tokens':100},'RESERVE_SHORTFALL'),
    ({},'USAGE_UNKNOWN')])
def test_actual_budget_violations_still_distinguished_from_unused_reservations(usage,status):
    result=ContextBudgetCoordinator(SimpleNamespace(),16000,4096,500).audit_usage(usage)
    assert result['status']==status
    assert result['hard_violation']==(status in {'HARD_WINDOW_EXCEEDED','OUTPUT_LIMIT_EXCEEDED'})


def test_legacy_context_repackages_after_provider_feedback(tmp_path):
    from repo_code.context import ContextEngine,estimate_tokens
    source=tmp_path/'input';source.mkdir()
    app=RepoCode(tmp_path/'state',ScriptedProvider(),'local')
    run=app.new_task('Preserve task',source)
    for i in range(8):app.store.append_message(run['id'],{'role':'user','content':str(i)+' detail '*600})
    provider=SimpleNamespace(observed_input_scale=1.0)
    serializer=lambda s,m,t:{'system':s,'messages':m,'tools':t}
    engine=ContextEngine(app.store,app.runtime,serializer,1024,1.0,provider=provider)
    # With proactive low-water packing a 6k window already reaches the minimum
    # summary+last-group layout before feedback. Leave room for a measurable
    # second repack; retain the original calibration and hard-budget assertions.
    _,before=engine.build(run['id'],[],{'max_tokens':12000,'reserve_tokens':1024})
    provider.observed_input_scale=1.8
    system,after=engine.build(run['id'],[],{'max_tokens':12000,'reserve_tokens':1024})
    assert estimate_tokens(serializer(system,after,[]))*1.8+1024<=12000
    assert len(after)<len(before)
