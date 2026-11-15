"""R1 counterexamples; runtime gates and the native provider envelope are real."""
from dataclasses import asdict
import json
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest
from repo_code.memory.budget import ContextBudgetCoordinator
from repo_code.providers import OpenAIProvider,ScriptedProvider
from repo_code.memory.models import MemoryProfile
from repo_code.models import Assistant
from repo_code.context import estimate_tokens,groups
from repo_code.util import RepoError
from test_memory_v2_contracts import memory_app,record,compact,state


@pytest.mark.asyncio
async def test_BUD_R1_01_optional_hits_removed_at_final_packing(memory_app,monkeypatch):
    app,run,_=memory_app
    async def hits(*a,**kw):return [{'id':'optional','version':1,'text':'detail '*9000}],'test-trace'
    monkeypatch.setattr(app.memory,'search',hits)
    _,messages,manifest=await app.memory.prepare_context(run['id'],[],policy={'max_tokens':8000})
    assert not json.loads(messages[0]['content'])['retrieved_evidence']
    b=manifest['budget_breakdown'];assert b['estimated_input_total']+b['reserved_output']+b['safety_margin']<=8000


@pytest.mark.asyncio
async def test_BUD_R1_02_parallel_results_externalized_without_splitting(memory_app):
    app,run,_=memory_app
    record(app,run,'assistant','',tool_calls=[{'id':str(i),'name':'read','arguments':{'path':str(i)}} for i in range(3)])
    for i in range(3):record(app,run,'tool','source '*5000,tool_call_id=str(i))
    _,messages,_=await app.memory.prepare_context(run['id'],[],policy={'max_tokens':8000})
    assert len(groups(messages)[-1])==4
    assert all('artifact' in json.loads(m['content']) for m in messages if m['role']=='tool')
    assert len(app.store.messages(run['id'])[-1]['content'])>20000


def test_BUD_R1_03_native_tools_guidance_and_protocol_all_count():
    p=OpenAIProvider('model',client=SimpleNamespace(),thinking='enabled',max_tokens=4096)
    c=ContextBudgetCoordinator(p,8000,4096,500)
    messages=[{'role':'assistant','content':'','reasoning_content':'opaque '*200,'tool_calls':[{'id':'x','name':'read','arguments':{'path':'x'}}]},
              {'role':'tool','tool_call_id':'x','content':'observed'}]
    tools=[{'name':'read','description':'schema '*4000,'parameters':{'type':'object','properties':{}}}]
    b=c.measure('contract'+'guidance '*100,messages,tools)
    assert b.estimated_input_total==estimate_tokens(p.build_request('contract'+'guidance '*100,messages,tools)[0])
    assert b.tool_schemas>4000 and not b.within_limit()
    parts=asdict(b);assert sum(parts[k] for k in ['system_and_contract','tool_schemas','native_protocol_fields','working_state','recent_complete_groups','compaction_projection','retrieved_memory','expanded_artifacts','other_guidance'])==b.estimated_input_total


@pytest.mark.asyncio
async def test_BUD_R1_04_one_summary_prefix_projection(memory_app):
    app,run,_=memory_app;record(app,run,'user','Unique old decision')
    await compact(app,run);record(app,run,'user','Current tail')
    _,messages,manifest=await app.memory.prepare_context(run['id'],[])
    assert not any(m.get('content')=='Unique old decision' for m in messages[1:])
    assert len(manifest['summary_node_ids'])==1 and manifest['packing_attempts']<=2


@pytest.mark.asyncio
async def test_BUD_R1_05_revocation_during_extraction_is_fenced(memory_app):
    app,run,_=memory_app
    from test_memory_v2_contracts import extraction
    def mutate(*args):app.memory.repo.revoke('unrelated-source');return extraction(*args)
    app.memory.compactor.provider=ScriptedProvider(handler=mutate)
    with pytest.raises(RepoError,match='SUMMARY_STALE_FENCE'):await compact(app,run)
    assert not app.memory.repo.get('memory_control','summary:'+run['id'])


def test_BUD_R1_06_concurrent_atomic_total(memory_app):
    app,run,_=memory_app
    def reserve(purpose):
        try:return app.memory.repo.reserve(run['id'],purpose,1000,4096,8,6000)
        except RepoError:return None
    with ThreadPoolExecutor(2) as pool:result=list(pool.map(reserve,['main','compaction']))
    assert len([r for r in result if r])==1


def test_BUD_R1_07_unknown_usage_keeps_reservation(memory_app):
    app,run,_=memory_app
    key=app.memory.repo.reserve(run['id'],'main',2000,4096,8,10000)
    app.memory.repo.settle(key,{},0.2)
    row=app.memory.repo.get('memory_budget',key)
    assert row['state']=='USAGE_UNKNOWN' and row['charged_tokens']==6096


@pytest.mark.asyncio
async def test_BUD_R1_08_required_material_cannot_be_truncated(memory_app):
    app,run,_=memory_app
    before=app.store.messages(run['id'])
    with pytest.raises(RepoError,match='required state, summary'):
        await app.memory.prepare_context(run['id'],[],approved_guidance='Authorized mandatory constraint '*4000,policy={'max_tokens':8000})
    assert app.store.messages(run['id'])==before and not app.agent.report(run['id'])['delivery']


@pytest.mark.asyncio
async def test_BUD_R1_09_no_matches_stay_empty(memory_app):
    app,run,_=memory_app;hits,_=await app.memory.search(run['id'],'absent E_NO_SUCH_SYMBOL',10)
    assert hits==[]


def test_BUD_R1_10_optional_observation_reclamation_fits_original_limit():
    """Portable regression for the archived failure's fixed optional allocation.

    The actual archived state is replayed separately by memory_csv_replay.py.
    """
    p=OpenAIProvider('m',client=SimpleNamespace(),max_tokens=4096)
    c=ContextBudgetCoordinator(p,16000,4096,500)
    context={'type':'memory_context_data','working_state':{'open_loops':[{'id':'o','text':'Preserve CSV API'}]},
             'summary':{'claims':[{'kind':'DECISION','text':'Preserve header order'},{'kind':'FACT','text':'optional '*1800}]},'retrieved_evidence':[]}
    messages=[{'role':'user','content':json.dumps(context)},{'role':'user','content':'required tail '*2200}]
    before=c.measure('frozen contract',messages,[])
    context['summary']['claims']=context['summary']['claims'][:1];messages[0]['content']=json.dumps(context)
    after=c.measure('frozen contract',messages,[])
    assert not before.within_limit() and after.estimated_input_total<before.estimated_input_total and after.within_limit()


def test_legacy_facade_can_measure_the_same_native_request(memory_app):
    from repo_code.service import RepoCode
    from repo_code.context import ContextEngine
    app,_,source=memory_app
    p=OpenAIProvider('fixture',client=SimpleNamespace(),max_tokens=4096)
    legacy=RepoCode(app.store.root/'legacy',p,'local');run=legacy.new_task('Preserve interface',source)
    legacy.store.append_message(run['id'],{'role':'assistant','content':'x'*18000})
    legacy.store.append_message(run['id'],{'role':'user','content':'Continue preserving the interface'})
    engine=ContextEngine(legacy.store,legacy.runtime,lambda s,m,t:p.build_request(s,m,t)[0],4096)
    system,messages=engine.build(run['id'],[],{'max_tokens':8000,'reserve_tokens':1000})
    assert estimate_tokens(p.build_request(system,messages,[])[0])+4096<=8000


def test_dev_calibration_is_shared_and_does_not_enlarge_window():
    import math
    p=OpenAIProvider('deepseek-flash',base_url='https://api.deepseek.com',client=SimpleNamespace(),max_tokens=4096)
    c=ContextBudgetCoordinator(p,16000,4096,500)
    b=c.measure('System contract',[{'role':'user','content':'a'*12000}],[])
    assert b.estimate_scale==1.2 and b.estimated_input_total==math.ceil(b.raw_estimated_input_total*1.2)
    assert b.max_effective_window==16000 and b.reserved_output==4096 and b.safety_margin==500


@pytest.mark.asyncio
async def test_externalized_output_revocation_blocks_envelope_and_artifact_read(memory_app):
    from conftest import tool
    app,run,_=memory_app
    record(app,run,'assistant','',tool_calls=[{'id':'big','name':'read','arguments':{'path':'large.txt'}}])
    record(app,run,'tool','source detail '*4000,tool_call_id='big')
    _,messages,manifest=await app.memory.prepare_context(run['id'],[],policy={'max_tokens':8000,'reserve_tokens':4000})
    result=next(json.loads(m['content']) for m in messages if m['role']=='tool')
    app.memory.repo.revoke(result['artifact'])
    with pytest.raises(RepoError,match='MEMORY_SOURCE_REVOKED'):app.memory.repo.check([manifest['protocol_envelope_ref']])
    _,receipt=await tool(app,run,'artifact.read',{'artifact':result['artifact']})
    assert receipt['status']=='FAILED' and receipt['result']['code']=='MEMORY_SOURCE_REVOKED'


@pytest.mark.asyncio
async def test_escape_heavy_artifact_pages_original_bytes_without_wrapper_chain(memory_app):
    from conftest import tool
    from repo_code.util import canonical,digest
    app,run,_=memory_app
    content=('nested '+json.dumps({'code':'\\'*12+'"'*6})+'\n')*500
    raw=content.encode();ref=app.store.blob(raw)
    app.store.put('run_artifact',digest([run['id'],ref]),{'run_id':run['id'],'artifact':ref})
    offset=0;recovered='';first=None
    while offset<len(raw):
        _,receipt=await tool(app,run,'artifact.read',{'artifact':ref,'offset':offset,'length':50000})
        result=receipt['result'];first=first or result
        assert result['source_artifact']==ref and 'preview' not in result and len(canonical(result))<24000
        assert result['returned_bytes']>0 and result['offset']==offset
        recovered+=result['content'];offset+=result['returned_bytes']
    assert recovered==content
    record(app,run,'assistant','',tool_calls=[{'id':'archive-page','name':'artifact.read','arguments':{'artifact':ref}}])
    record(app,run,'tool',json.dumps(first),tool_call_id='archive-page')
    _,messages,_=await app.memory.prepare_context(run['id'],[],policy={'max_tokens':8000})
    projected=next(json.loads(m['content']) for m in messages if m['role']=='tool')
    assert projected.get('artifact',projected.get('source_artifact'))==ref


def test_explicit_unlimited_model_calls_keeps_other_resource_limits(memory_app):
    from repo_code.models import Budget
    app,_,source=memory_app
    run=app.new_task('Explicit operator unlimited calls',source,budget=Budget(model_calls=None,tool_calls=1,wall_seconds=600))
    app.runtime.consume(run['id'],'model_calls',1001)
    app.runtime.consume(run['id'],'tool_calls')
    with pytest.raises(RepoError) as error:app.runtime.consume(run['id'],'tool_calls')
    assert error.value.code=='BUDGET_EXHAUSTED'
    assert app.runtime.run(run['id'])['data']['budget_used']['model_calls']==1001
    assert Budget().model_calls==30
