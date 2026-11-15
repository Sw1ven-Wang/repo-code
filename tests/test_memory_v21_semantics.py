"""R2 semantic counterexamples; assertions cover the effective projection."""
import json
from pathlib import Path
import pytest
from repo_code.util import RepoError
from test_memory_v2_contracts import memory_app,record,compact,state


def claim(app,run,text,quote,kind='FACT',status='OBSERVED',refs=None,**kw):
    rows=app.memory.history.records(run['id'])
    return {'changes':[{'claim_id':'observed','text':text,'quote':quote,'kind':kind,'epistemic_status':status,
        'source_ids':refs or [rows[-1]['ref']['source_id']],**kw}]}


def test_SEM_01_failed_test_cannot_become_verified_fact(memory_app):
    app,run,_=memory_app;record(app,run,'tool','1 failed',tool_call_id='test')
    rows=app.memory.history.records(run['id'])
    result=app.memory.compactor.validate(claim(app,run,'All tests passed','1 failed'),rows,state(app,run),[])
    assert result[0]['text']=='1 failed' and result[0]['model_interpretation']['status']=='UNVERIFIED_INFERENCE'
    assert state(app,run)['completion_from_model'] is False


@pytest.mark.asyncio
async def test_SEM_02_previous_passing_result_becomes_historical_without_new_summary(memory_app):
    app,run,_=memory_app;before=app.runtime.workspace(run).snapshot()
    record(app,run,'assistant','',tool_calls=[{'id':'test','name':'process.run','arguments':{'argv':['pytest']}}])
    record(app,run,'tool',json.dumps({'stdout':'1 passed','exit_code':0,'relevant_state':before}),tool_call_id='test')
    rows=app.memory.history.records(run['id'])
    claims=app.memory.compactor.validate(claim(app,run,'1 passed','1 passed'),rows,state(app,run),[])
    app.memory.compactor.commit(app.memory.compactor.fence(run['id'],rows),rows,claims,state(app,run))
    (Path(run['data']['workspace'])/'a.py').write_text('value=2\n')
    _,messages,_=await app.memory.prepare_context(run['id'],[])
    visible=json.loads(messages[0]['content'])['summary']['claims']
    assert visible[0]['validity']=='HISTORICAL'


@pytest.mark.asyncio
async def test_SEM_03_original_user_constraint_survives_three_compactions(memory_app):
    app,run,_=memory_app;constraint='Do not install any extra dependency; preserve requirements.txt.'
    record(app,run,'user',constraint)
    for i in range(3):
        record(app,run,'user','Phase '+str(i));await compact(app,run)
    _,messages,_=await app.memory.prepare_context(run['id'],[])
    context=json.loads(messages[0]['content'])
    assert any(x['text']==constraint for x in context['working_state']['accepted_user_material'])
    assert app.store.messages(run['id'])[1]['content']==constraint


@pytest.mark.asyncio
async def test_SEM_04_latest_correction_marks_old_decision_and_preserves_both_sources(memory_app):
    app,run,_=memory_app;record(app,run,'user','Use legacy rounding');rows=app.memory.history.records(run['id'])
    proposed=claim(app,run,'Use legacy rounding','Use legacy rounding',kind='DECISION',status='USER_STATED')
    claims=app.memory.compactor.validate(proposed,rows,state(app,run),[])
    record(app,run,'user','Correction: use ROUND_HALF_UP; previous rounding is obsolete')
    rows=app.memory.history.records(run['id'])
    result=app.memory.compactor.validate({'changes':[]},rows,state(app,run),claims)
    assert result[0]['validity']=='HISTORICAL' and result[0]['conflict_refs']
    assert result[0]['support_refs'][0]['source_id']!=result[0]['conflict_refs'][0]['source_id']


@pytest.mark.asyncio
async def test_SEM_05_bad_source_preserves_installed_summary(memory_app):
    app,run,_=memory_app;await compact(app,run)
    head=app.memory.repo.get('memory_control','summary:'+run['id'])
    with pytest.raises(RepoError,match='SUMMARY_SOURCE_DENIED'):
        app.memory.compactor.validate(claim(app,run,'invented','invented',refs=['message:999999']),app.memory.history.records(run['id']),state(app,run),[])
    assert app.memory.repo.get('memory_control','summary:'+run['id'])==head


def test_mixed_citation_cannot_launder_model_statement_as_host_observation(memory_app):
    app,run,_=memory_app;record(app,run,'assistant','All checks passed');record(app,run,'tool','Actual test failed',tool_call_id='x')
    rows=app.memory.history.records(run['id']);refs=[r['ref']['source_id'] for r in rows[-2:]]
    with pytest.raises(RepoError,match='SUMMARY_FACT_UNSUPPORTED'):
        app.memory.compactor.validate(claim(app,run,'All checks passed','All checks passed',refs=refs),rows,state(app,run),[])


def test_overlong_optional_claim_is_quarantined_without_truncating_quote(memory_app):
    app,run,_=memory_app;record(app,run,'user','Preserve interface')
    valid=claim(app,run,'Preserve interface','Preserve interface',kind='DECISION',status='USER_STATED')
    invalid={**valid['changes'][0],'claim_id':'oversized','quote':'x'*1201}
    value={**valid,'changes':valid['changes']+[invalid]}
    result=app.memory.compactor.validate(value,app.memory.history.records(run['id']),state(app,run),[])
    assert len(result)==1 and result[0]['claim_id']=='observed'
    assert any(x.get('rejected_claims',[{}])[0].get('reason')=='SUMMARY_CLAIM_TOO_LARGE' for x in app.memory.repo.values('memory_control'))


@pytest.mark.asyncio
async def test_semantic_input_does_not_duplicate_large_model_code_payload(memory_app):
    app,run,_=memory_app
    record(app,run,'assistant','Proposed code only',tool_calls=[{'id':'write','name':'create','arguments':{'path':'b.py','content':'x'*30000}}])
    record(app,run,'tool','FILE_EXISTS',tool_call_id='write')
    await compact(app,run)
    assert len(app.store.messages(run['id'])[-2]['tool_calls'][0]['arguments']['content'])==30000
    rows=[x for x in app.memory.repo.values('memory_budget') if x['purpose']=='compaction']
    assert rows[0]['estimated_input']<5000


@pytest.mark.asyncio
async def test_long_closed_trace_uses_bounded_exact_tool_excerpts(memory_app):
    app,run,_=memory_app
    for i in range(30):
        record(app,run,'assistant','Inspect result',tool_calls=[{'id':str(i),'name':'process.run','arguments':{'argv':['pytest']}}])
        record(app,run,'tool',json.dumps({'stdout':'Failure detail '*200,'stderr':'','exit_code':1,'is_error':True,
            'result_artifact':'sha256:'+'a'*64,'relevant_state':'sha256:'+'b'*64}),tool_call_id=str(i))
    await compact(app,run)
    row=app.memory.repo.values('memory_budget')[0]
    assert row['estimated_input']+4096+500<=16000


@pytest.mark.asyncio
async def test_large_version_maps_reclaim_optional_semantic_metadata_without_sources(memory_app):
    from types import SimpleNamespace
    from repo_code.providers import OpenAIProvider
    app,run,_=memory_app
    p=app.memory.compactor.provider
    p.model='deepseek-flash';p.base_url='https://api.deepseek.com';p.max_tokens=4096
    p.build_request=OpenAIProvider(p.model,base_url=p.base_url,client=SimpleNamespace(),max_tokens=4096).build_request
    for i in range(30):
        record(app,run,'assistant','Inspect failing process',tool_calls=[{'id':'v'+str(i),'name':'process.run','arguments':{'argv':['pytest']}}])
        record(app,run,'tool',json.dumps({'stdout':'Exact failure detail '*30,'stderr':'assertion failure','exit_code':1,'is_error':True,
            'versions':{f'file-{j}.py':'sha256:'+'d'*64 for j in range(50)}}),tool_call_id='v'+str(i))
    await compact(app,run)
    row=next(r for r in app.memory.repo.values('memory_control') if r.get('packing_limit')==2)
    assert len(row['input_estimates'])==2 and row['input_estimates'][0]>11404>=row['input_estimates'][1]
    assert row['closed_sources_removed']==row['host_loops_removed']==0 and row['source_count']==61
    assert sum(r['purpose']=='compaction' for r in app.memory.repo.values('memory_budget'))==1


def test_duplicate_citations_are_bounded_only_after_all_authorized(memory_app):
    app,run,_=memory_app
    for i in range(12):record(app,run,'tool','same repeated failure',tool_call_id=str(i))
    rows=app.memory.history.records(run['id']);refs=[r['ref']['source_id'] for r in rows[1:]]
    value=claim(app,run,'same repeated failure','same repeated failure',refs=refs)
    result=app.memory.compactor.validate(value,rows,state(app,run),[])
    assert len(result[0]['support_refs'])==8
    value['changes'][0]['source_ids'].append('message:999999')
    with pytest.raises(RepoError,match='SUMMARY_SOURCE_DENIED'):app.memory.compactor.validate(value,rows,state(app,run),[])


def test_empty_quote_is_rejected_not_used_to_advance_an_entire_bad_summary(memory_app):
    app,run,_=memory_app;record(app,run,'user','Keep interface')
    proposal=claim(app,run,'Keep interface','Keep interface',kind='DECISION',status='USER_STATED')
    bad={**proposal['changes'][0],'claim_id':'unsupported','quote':''}
    result=app.memory.compactor.validate({'changes':[proposal['changes'][0],bad]},app.memory.history.records(run['id']),state(app,run),[])
    assert len(result)==1
    with pytest.raises(RepoError,match='SUMMARY_QUOTE_UNSUPPORTED'):
        app.memory.compactor.validate({'changes':[bad]},app.memory.history.records(run['id']),state(app,run),result)


@pytest.mark.parametrize('bad',[None,'invalid',{'source_ids':[{}]*10}])
def test_malformed_source_shapes_reject_as_schema_error_not_unhandled_typeerror(memory_app,bad):
    from pydantic import ValidationError
    app,run,_=memory_app
    with pytest.raises(ValidationError):app.memory.compactor.validate({'changes':[bad]},app.memory.history.records(run['id']),state(app,run),[])


def test_model_cannot_drop_host_open_loops_by_returning_empty_ids(memory_app):
    app,run,_=memory_app
    app.store.put('plan',run['id'],{'steps':['Investigate rollback','Verify registered behavior']})
    rows=app.memory.history.records(run['id']);host=state(app,run)
    claims=app.memory.compactor.validate({'changes':[],'retained_open_loop_ids':[]},rows,host,[])
    _,node=app.memory.compactor.commit(app.memory.compactor.fence(run['id'],rows),rows,claims,host)
    assert set(node['open_loop_ids'])=={x['id'] for x in host['open_loops']}
