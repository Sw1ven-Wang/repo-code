"""Regressions for the SQLFluff successful-inspection loop; no model API calls."""
import json
from copy import deepcopy
import pytest
from repo_code.models import ToolCall, Budget
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.memory.models import MemoryProfile
from repo_code.progress import coalesce_process_observations, observation
from repo_code.context import groups
from test_memory_v2_contracts import memory_app


def command(uid, **kw):
    return {'id': uid, 'name': 'process.run', 'arguments': {'argv': ['python', '-c', "print('table_reference alias_expression')"], **kw}}


@pytest.mark.parametrize('memory', [False, True])
@pytest.mark.asyncio
async def test_real_agent_successful_loop_stops_before_consuming_tool_budget(tmp_path, sandbox_kit, memory):
    source=tmp_path/'source'; source.mkdir()
    provider=ScriptedProvider([{'tool_calls':[command(str(i))]} for i in range(8)])
    profile=MemoryProfile(mode='lexical_only',learning_enabled=False) if memory else None
    app=RepoCode(tmp_path/'state',provider,'native',memory_profile=profile);app.sandbox.kit=sandbox_kit
    try:
        run=app.new_task('Inspect then implement',source,budget=Budget(model_calls=20,tool_calls=100),authorize_process=True)
        report=await app.execute(run['id'])
        assert (report['state'],report['reason'])==('PARTIAL','NO_PROGRESS')
        assert app.runtime.run(run['id'])['data']['budget_used']['tool_calls']==5
        assert not app.runtime.unresolved(run['id'])
        assert app.runtime.workspace(run).snapshot()==run['data']['manifest']['workspace_baseline_digest']
        messages=app.store.messages(run['id']); groups(messages)
        withheld=[json.loads(m['content']) for m in messages if m['role']=='tool' and 'PROCESS_NO_PROGRESS' in m['content']]
        assert len(withheld)==2 and all(r['executed'] is False for r in withheld)
        assert all(r['identical_observations']==5 for r in withheld)
    finally:app.close()


@pytest.mark.parametrize('memory', [False, True])
@pytest.mark.asyncio
async def test_model_can_recover_with_edit_and_retest_after_warning(tmp_path,sandbox_kit,memory):
    source=tmp_path/'source';source.mkdir(); seen=[]
    replies=[{'tool_calls':[command(str(i))]} for i in range(6)]
    replies += [{'tool_calls':[{'id':'edit','name':'create','arguments':{'path':'fix.py','content':'fixed=True\n'}}]},
                {'tool_calls':[command('retest')]}, {'text':'Patch ready for external verification.'},
                {'tool_calls':[{'id':'verify','name':'process.run','arguments':{'argv':['python','-c',
                    "scope = {}; exec(open('fix.py').read(), scope); assert scope['fixed'] is True"]}}]},
                {'text':'Final source assertion passed.'}]
    def model(system,messages,tools,index):
        status=json.JSONDecoder().raw_decode(system.split('Host runtime status (not task acceptance): ',1)[1])[0]
        seen.append(status)
        return replies[index]
    profile=MemoryProfile(mode='lexical_only',learning_enabled=False) if memory else None
    app=RepoCode(tmp_path/'state',ScriptedProvider(handler=model),'native',memory_profile=profile);app.sandbox.kit=sandbox_kit
    try:
        run=app.new_task('Inspect then implement',source,budget=Budget(model_calls=20,tool_calls=100),authorize_write=True,authorize_process=True)
        report=await app.execute(run['id'])
        assert report['reason'].startswith('CANDIDATE_READY_EXTERNAL_EVALUATION')
        assert report['candidate_review']['status']=='NEW_CHECK_PASSED'
        assert report['delivery'] is None
        candidates=app.store.values('l1_stage_candidate')
        if memory:
            assert not candidates  # This Memory v2 fixture disables learning.
        else:
            stage,=(row['payload'] for row in candidates)
            assert stage['outcome']=='NEW_CHECK_PASSED' and stage['run_state']=='RUNNING'
            assert stage['source_digest']==app.runtime.workspace(run).snapshot()
        assert not app.store.values('l1_episode')
        assert (app.runtime.workspace(run).root/'fix.py').read_text()=='fixed=True\n'
        assert app.runtime.run(run['id'])['data']['budget_used']['tool_calls']==8
        assert app.provider.calls==len(replies)
        assert seen[3]['process_progress']['identical_observations']==3
        assert seen[3]['process_progress']['warning']
        assert seen[3]['budget']['tool_calls']['remaining']==97
        assert not app.runtime.unresolved(run['id'])
    finally:app.close()


@pytest.fixture
def simulated_process(tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir()
    app=RepoCode(tmp_path/'state',ScriptedProvider(),'local')
    run=app.new_task('Control negative cases',source,budget=Budget(tool_calls=100),authorize_write=True,authorize_process=True)
    stdout=['tree'];environment=[app.sandbox.environment_digest];failure=[False]
    async def execute(req,op_id):
        snapshot=app.runtime.workspace(run).snapshot()
        return {'stdout':stdout[0],'stderr':'','exit_code':1 if failure[0] else 0,'is_error':failure[0],
                'relevant_state':snapshot,'output_snapshot':snapshot,'environment_digest':environment[0]}
    monkeypatch.setattr(app.broker,'_execute',execute)
    async def submit(uid,**kw):
        req=app.broker.register_call(run['id'],uid,ToolCall.model_validate(command(uid,**kw)))
        return req,await app.broker.submit(req)
    return app,run,submit,stdout,environment,failure


@pytest.mark.asyncio
async def test_bounded_explicit_retry_and_idempotent_denial(simulated_process):
    app,run,submit,*_=simulated_process
    for i in range(5):await submit(str(i))
    req,receipt=await submit('denied')
    assert receipt['status']=='CANCELLED' and receipt['result']['executed'] is False
    assert await app.broker.submit(req)==receipt
    assert (await submit('reason1',retry_reason='Waited for external service readiness'))[1]['status']=='SUCCEEDED'
    assert (await submit('same-reason',retry_reason='Waited for external service readiness'))[1]['status']=='CANCELLED'
    assert (await submit('reason2',retry_reason='Confirm a possible intermittent outcome'))[1]['status']=='SUCCEEDED'
    assert (await submit('reason3',retry_reason='Another reason cannot bypass the cap'))[1]['status']=='CANCELLED'
    assert app.runtime.run(run['id'])['data']['budget_used']['tool_calls']==7
    assert not app.runtime.unresolved(run['id'])


@pytest.mark.asyncio
async def test_new_output_source_and_command_are_not_permanently_blocked(simulated_process):
    app,run,submit,stdout,environment,failure=simulated_process
    for i in range(4):await submit(str(i))
    stdout[0]='different evidence'
    await submit('new-output')
    assert app.store.get('process_progress',run['id'])['identical_observations']==1
    for i in range(4):await submit('new'+str(i))
    assert (await submit('blocked'))[1]['status']=='CANCELLED'
    (app.runtime.workspace(run).root/'changed.txt').write_text('new source')
    assert (await submit('new-source'))[1]['status']=='SUCCEEDED'
    assert (await submit('new-command',stdin='different input'))[1]['status']=='SUCCEEDED'


@pytest.mark.asyncio
async def test_failed_process_also_guarded_and_different_result_resets(simulated_process):
    app,run,submit,stdout,environment,failure=simulated_process;failure[0]=True
    for i in range(5):assert (await submit(str(i)))[1]['status']=='FAILED'
    assert (await submit('blocked'))[1]['status']=='CANCELLED'
    failure[0]=False
    assert (await submit('retry',retry_reason='Service was restarted outside the workspace'))[1]['status']=='SUCCEEDED'
    assert app.store.get('process_progress',run['id'])['identical_observations']==1


@pytest.mark.asyncio
async def test_alternating_inspections_do_not_evade_progress_guard(simulated_process):
    app,run,submit,*_=simulated_process
    for i in range(5):
        await submit('a'+str(i),stdin='a')
        await submit('b'+str(i),stdin='b')
    assert (await submit('a-blocked',stdin='a'))[1]['status']=='CANCELLED'
    assert (await submit('b-blocked',stdin='b'))[1]['status']=='CANCELLED'


@pytest.mark.asyncio
async def test_large_bound_results_use_full_output_digest(simulated_process):
    app,run,submit,stdout,*_=simulated_process
    stdout[0]='same prefix '*4000+'first tail'
    for i in range(4):await submit(str(i))
    stdout[0]='same prefix '*4000+'changed tail'
    receipt=(await submit('changed'))[1]
    assert receipt['result']['truncated']
    assert receipt['result']['process_progress']['identical_observations']==1


@pytest.mark.asyncio
async def test_control_change_resets_streak_without_permanent_block(simulated_process):
    app,run,submit,*_=simulated_process
    for i in range(5):await submit(str(i))
    assert (await submit('blocked'))[1]['status']=='CANCELLED'
    with app.store.tx() as c:
        fresh=app.runtime.run(run['id'],c)
        app.runtime.update(c,fresh,control=True)
    assert (await submit('new-control'))[1]['status']=='SUCCEEDED'
    assert app.store.get('process_progress',run['id'])['identical_observations']==1


def history_messages():
    result=[]
    for i in range(8):
        call=command(str(i))
        body={'stdout':'table_reference '*600,'stderr':'','exit_code':0,'is_error':False,
              'environment_digest':'environment','relevant_state':'source','output_snapshot':'source',
              'result_artifact':'artifact-'+str(i),'elapsed':i,'lease_id':str(i)}
        result.extend([{'role':'assistant','content':'','tool_calls':[call]},
                       {'role':'tool','tool_call_id':str(i),'content':json.dumps(body)}])
    return result


def test_projection_deduplicates_old_results_preserves_latest_and_protocol():
    raw=history_messages();before=deepcopy(raw);view=coalesce_process_observations(raw)
    assert raw==before and len(groups(view))==8
    assert all(json.loads(m['content'])['duplicate_process_observation'] for m in view[:-2] if m['role']=='tool')
    assert view[-1]==raw[-1]
    assert len(json.dumps(view))<len(json.dumps(raw))//2
    changed=deepcopy(raw);body=json.loads(changed[1]['content']);body['stdout']='new result';changed[1]['content']=json.dumps(body)
    assert coalesce_process_observations(changed)[1]==changed[1]


@pytest.mark.parametrize('field,value',[('timeout',True),('cancelled',True),('running',True),('environment_digest',None),('stdout',None)])
def test_incomplete_outcome_never_proves_repetition(field,value):
    body=json.loads(history_messages()[1]['content']);body[field]=value
    assert observation(command('x')['arguments'],body) is None


@pytest.mark.asyncio
async def test_memory_projection_keeps_latest_full_tree(memory_app):
    app,run,_=memory_app
    raw=history_messages()
    for message in raw:
        if message['role']=='tool':
            body=json.loads(message['content']);ref=app.store.json_blob(body);body['result_artifact']=ref
            from repo_code.util import digest
            app.store.put('run_artifact',digest([run['id'],ref]),{'run_id':run['id'],'artifact':ref})
            message={**message,'content':json.dumps(body)}
        app.store.append_message(run['id'],message)
    _,messages,manifest=await app.memory.prepare_context(run['id'],[],policy={'max_tokens':16000})
    assert 'stdout' in json.loads(messages[-1]['content'])
    assert len(groups(messages))==len(groups(app.store.messages(run['id'])))+1
    assert manifest['budget_breakdown']['estimated_input_total']<16000


@pytest.mark.asyncio
async def test_pressure_reclaims_old_outputs_before_latest_tree(memory_app):
    app,run,_=memory_app
    for i in range(24):
        app.store.append_message(run['id'],{'role':'assistant','content':'','tool_calls':[
            {'id':str(i),'name':'read','arguments':{'path':str(i)}}]})
        app.store.append_message(run['id'],{'role':'tool','tool_call_id':str(i),'content':str(i)+' old evidence '*150})
    app.store.append_message(run['id'],{'role':'assistant','content':'','tool_calls':[command('latest')]})
    latest=json.dumps({'stdout':'header\n'+'tree '*600+' table_reference alias_expression '+'tree '*600+'\ntail',
                       'stderr':'','exit_code':0,'is_error':False})
    app.store.append_message(run['id'],{'role':'tool','tool_call_id':'latest','content':latest})
    app.store.append_message(run['id'],{'role':'assistant','content':'','tool_calls':[command('withheld')]})
    app.store.append_message(run['id'],{'role':'tool','tool_call_id':'withheld','content':json.dumps({'code':'PROCESS_NO_PROGRESS','executed':False})})
    _,messages,manifest=await app.memory.prepare_context(run['id'],[],policy={'max_tokens':16000})
    assert next(m for m in messages if m.get('tool_call_id')=='latest')['content']==latest
    assert any('bounded_preview' in m['content'] for m in messages if m['role']=='tool')
    assert not manifest['summary_node_ids']
    assert manifest['budget_breakdown']['estimated_input_total']+manifest['reserved_output_tokens']+app.memory.profile.safety_tokens<=16000


