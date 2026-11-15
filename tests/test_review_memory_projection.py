"""Versioned verification priority and nonempty, bounded summary observations."""
import json
import pytest
from repo_code.models import Assistant
from repo_code.providers import ScriptedProvider
from repo_code.context import groups
from test_review_workflow import setup,tool


def fact_extraction(system,messages,tools,count):
    payload=json.loads(messages[0]['content']);changes=[]
    for row in payload['new_closed_groups']:
        if row['message']['role']!='tool':continue
        fields=row.get('quote_fields',[])
        if not fields:continue
        changes.append({'action':'ADD','claim_id':row['ref']['source_id'],'kind':'FACT',
                        'quote_id':fields[0]['quote_id'],'epistemic_status':'OBSERVED'})
    return Assistant(text=json.dumps({'changes':changes[:10],
        'retained_open_loop_ids':[x['id'] for x in payload['mandatory_open_loops']]}))


@pytest.mark.asyncio
async def test_fact_claims_have_bounded_floor_even_with_working_evidence(tmp_path):
    app,run=setup(tmp_path,True,ScriptedProvider(handler=fact_extraction))
    try:
        await tool(app,run,'read',{'path':'a.py'},'read')
        records=app.memory.history.records(run['id']);_,state=app.memory.reducer.reduce(run['id'])
        head,node=await app.memory.compactor.compact(run['id'],records[-1]['seq'],state)
        assert node['claims'] and all(c['kind']=='FACT' for c in node['claims'])
        _,messages,manifest=await app.memory.prepare_context(run['id'],[],approved_guidance='padding '*2500)
        data=json.loads(messages[0]['content'])
        assert data['summary']['claims'] and any(c['kind']=='FACT' for c in data['summary']['claims'])
        controls=[r for r in app.memory.repo.values('memory_control') if r.get('request_digest')==manifest['serialized_request_digest']]
        assert controls[-1]['claim_projection']['floor_claim_ids']
        assert controls[-1]['claim_projection']['floor_tokens']<=320
        b=manifest['budget_breakdown'];assert b['estimated_input_total']+b['reserved_output']+b['safety_margin']<=16000
        groups(messages)
    finally:app.close()


@pytest.mark.asyncio
async def test_successful_check_group_outlives_many_unrelated_processes(tmp_path):
    app,run=setup(tmp_path,True,ScriptedProvider(handler=fact_extraction))
    try:
        await tool(app,run,'create',{'path':'fixed.py','content':'fixed=True\n'},'write')
        await tool(app,run,'process.run',{'argv':['python','-m','pytest']},'check')
        for i in range(21):
            await tool(app,run,'process.run',{'argv':['ls','-'+'a'*(i+1)]},str(i))
        records=app.memory.history.records(run['id']);_,state=app.memory.reducer.reduce(run['id'])
        assert any(o.get('verification') and o['validity']=='CURRENT' for o in state['host_observations'])
        await app.memory.compactor.compact(run['id'],records[-1]['seq'],state)
        _,messages,manifest=await app.memory.prepare_context(run['id'],[])
        outputs={m.get('tool_call_id'):m for m in messages if m['role']=='tool'}
        assert 'check' in outputs and '3 passed' in outputs['check']['content']
        assert 'write' in outputs
        groups(messages)
    finally:app.close()


@pytest.mark.asyncio
async def test_proposed_check_loop_and_no_match_failure_classification(tmp_path):
    app,run=setup(tmp_path,True)
    try:
        await tool(app,run,'create',{'path':'fixed.py','content':'value=1\n'},'write')
        _,state=app.memory.reducer.reduce(run['id'])
        assert state['open_loops'][0]['status']=='PROPOSED'
        old=app.sandbox.run
        async def no_matches(*args,**kwargs):
            result=await old(*args,**kwargs);result.update(exit_code=1,stdout='',stderr='');return result
        app.sandbox.run=no_matches
        await tool(app,run,'process.run',{'argv':['grep','missing','a.py']},'grep')
        _,state=app.memory.reducer.reduce(run['id'])
        assert not state['failed_attempts']
        assert any(o.get('outcome')=='NO_MATCH_NOT_PROCESS_FAILURE' and 'grep' in o['argv_summary'] for o in state['host_observations'])
    finally:app.close()
