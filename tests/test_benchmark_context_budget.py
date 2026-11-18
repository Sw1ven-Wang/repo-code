"""Only a signed, bounded pre-dispatch grant can enlarge benchmark context."""
import pytest
from repo_code.benchmark import TrustedStageLoader,admit_budget,allocation
from repo_code.models import Assistant
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.util import RepoError,digest


def prepare(tmp_path,context=None,grant=True,size=110000):
    seed=tmp_path/'seed';seed.mkdir();(seed/'first.py').write_text('public = 1\n')
    seen=[]
    def handle(system,messages,tools,index):
        from repo_code.context import estimate_tokens
        seen.append(estimate_tokens([system,messages,tools]))
        return Assistant(text='Handoff only, not acceptance.')
    provider=ScriptedProvider(handler=handle)
    app=RepoCode(tmp_path/'host',provider,sandbox='local')
    run=app.new_task('Retain the public task.',seed,conversation=True)
    args=dict(experiment_digest=digest('frozen-context-test'),session_model_calls=10,session_tool_calls=20,
        stage_model_calls=5,stage_tool_calls=10,wall_seconds=600,stage_seconds=120)
    if grant:
        admit_budget(app.runtime,run['id'],**args,context_tokens=context)
        ws=app.runtime.workspace(app.runtime.run(run['id']))
        TrustedStageLoader(app.runtime).admit(run['id'],'one',{'first.py':b'public = 1\n'},['first.py'],
            prompt_digest=digest('one'),expected_before=ws.snapshot(),initial=True)
    app.store.append_message(run['id'],{'role':'assistant','content':'Z'*size})
    return app,run,args,seen


@pytest.mark.parametrize('context,size,minimum',[(64000,110000,24000),(262144,300000,64000)])
async def test_signed_context_window_admits_large_mandatory_group(tmp_path,context,size,minimum):
    app,run,_,seen=prepare(tmp_path,context,size=size)
    result=await app.execute(run['id'])
    assert result['state']=='WAITING_USER' and len(seen)==1 and minimum<seen[0]<context-4096
    assert allocation(app.runtime,app.runtime.run(run['id']))['context_tokens']==context
    assert app.releases.active()['digest']==run['data']['manifest']['release_digest']
    app.close()


async def test_ordinary_context_window_is_not_enlarged(tmp_path):
    app,run,_,seen=prepare(tmp_path,grant=False)
    result=await app.execute(run['id'])
    assert result['state']=='PARTIAL' and result['reason']=='CONTEXT_BUDGET_EXCEEDED' and not seen
    app.close()


@pytest.mark.parametrize('limit',[True,5999,262145,0,-1])
def test_invalid_context_grant_is_rejected_before_dispatch(tmp_path,limit):
    app,run,args,_=prepare(tmp_path,grant=False)
    with pytest.raises(RepoError) as caught:admit_budget(app.runtime,run['id'],**args,context_tokens=limit)
    assert caught.value.code=='INVALID_BENCHMARK_BUDGET' and app.provider.calls==0
    app.close()


@pytest.mark.parametrize('change',['tamper','revocation'])
async def test_context_authority_is_rechecked_before_model_call(tmp_path,change):
    app,run,_,seen=prepare(tmp_path,64000)
    with app.store.tx() as c:
        current=app.runtime.run(run['id'],c);grant=current['data']['benchmark_allocation']
        if change=='tamper':
            grant['body']['context_tokens']=128000;app.runtime.update(c,current,data=current['data'])
        else:app.store.put('revocation',digest(grant['body']),{'reason':'revoked'},c)
    result=await app.execute(run['id'])
    assert result['state']=='BLOCKED' and result['reason'].startswith('BENCHMARK_ALLOCATION_') and not seen
    app.close()


def test_context_grant_is_frozen_and_bound_to_run(tmp_path):
    app,run,args,_=prepare(tmp_path,64000)
    with pytest.raises(RepoError) as caught:admit_budget(app.runtime,run['id'],**args,context_tokens=128000)
    assert caught.value.code=='BENCHMARK_ALLOCATION_FROZEN'
    app.close()
