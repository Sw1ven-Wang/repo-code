import json
import zipfile
from pathlib import Path
import pytest
from repo_code.models import Assistant,ToolCall,Check,Contract,Requirement,Subject,Budget
from repo_code.acceptance import freeze_contract
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.delivery import Delivery
from repo_code.util import RepoError
from conftest import new_run,make_contract

def assistant_tool(name,args,tid='c'):return Assistant(tool_calls=[ToolCall(id=tid,name=name,arguments=args)])

def set_provider(app,p):
    app.provider=p;app.agent.provider=p;app.broker.provider=p

@pytest.mark.asyncio
async def test_full_greenfield_repair_and_delivery(app,source,contract,tmp_path):
    p=ScriptedProvider([
        assistant_tool('create',{'path':'main.py','content':'def answer(): return 0\n'},'create'),
        Assistant(text='Please verify'),
        assistant_tool('read',{'path':'main.py'},'read'),
        assistant_tool('edit',{'path':'main.py','old_text':'return 0','new_text':'return 42'},'edit'),
        assistant_tool('process.run',{'argv':['python','-c','from main import answer;print(answer())']},'run'),
        Assistant(text='Ready for Host verification')])
    set_provider(app,p);run=new_run(app,source,contract,content=None)
    result=await app.execute(run['id'])
    assert result['state']=='SUCCEEDED',result
    assert p.calls==6 and result['delivery']['outcome']=='VERIFIED'
    assert not (source/'main.py').exists()
    destination=tmp_path/'exported';destination.mkdir()
    Delivery(app).export(run['id'],destination)
    assert 'return 42' in (destination/'main.py').read_text()
    Delivery(app).integrate(run['id'],True)
    assert 'return 42' in (source/'main.py').read_text()

@pytest.mark.asyncio
async def test_no_contract_never_verified(app,source):
    set_provider(app,ScriptedProvider([Assistant(text='done')]))
    run=new_run(app,source);result=await app.execute(run['id'])
    assert result['state']=='PARTIAL' and result['delivery'] is None

@pytest.mark.asyncio
async def test_model_claim_without_required_work_not_success(app,source,contract):
    set_provider(app,ScriptedProvider([Assistant(text='All tests passed'),Assistant(text='Done')]))
    run=new_run(app,source,contract,content='def answer(): return 0',budget=Budget(model_calls=2))
    result=await app.execute(run['id']);assert result['state']=='PARTIAL' and result['delivery'] is None

@pytest.mark.asyncio
async def test_pending_approval_resume_keeps_original_tool_call(app,source,contract):
    p=ScriptedProvider([assistant_tool('create',{'path':'main.py','content':'def answer():return 42'},'c'),Assistant(text='done')]);set_provider(app,p)
    run=app.new_task('answer',source,contract)
    first=await app.execute(run['id']);assert first['state']=='WAITING_APPROVAL'
    pending=app.store.get('pending_turn',run['id'])
    from repo_code.models import OperationRequest
    req=OperationRequest.model_validate(pending['requests'][0]);app.broker.approve(req.key)
    result=await app.execute(run['id']);assert result['state']=='SUCCEEDED'
    assert app.runtime.run(run['id'])['data']['budget_used']['tool_calls']==1

@pytest.mark.asyncio
async def test_origin_change_blocks_integrate(app,source,contract):
    set_provider(app,ScriptedProvider([Assistant(text='done')]))
    run=new_run(app,source,contract);result=await app.execute(run['id']);assert result['state']=='SUCCEEDED'
    (source/'human.txt').write_text('keep')
    with pytest.raises(RepoError,match='ORIGIN_CHANGED'):Delivery(app).integrate(run['id'],True)
    assert (source/'human.txt').read_text()=='keep'

@pytest.mark.asyncio
async def test_fastapi_external_asgi_cases(app,source):
    code='from fastapi import FastAPI\napp=FastAPI()\n@app.get("/value/{n}")\nasync def value(n:int):return {"value":n*2}\n'
    contract=freeze_contract(Contract(requirements=[Requirement(id='api',description='GET /value doubles integers')],
        checks=[Check(id='http',kind='asgi',definition={'module':'main','app':'app','cases':[{'id':'normal','path':'/value/21','expected_status':200,'expected_body':{'value':42}},{'id':'missing','path':'/missing','expected_status':404,'expected_body':{'detail':'Not Found'}}]})],required_checks=['http'],coverage={'api':['http']}),'operator')
    run=new_run(app,source,contract,content=code);intent=app.runtime.prepare(run['id']);decision=await app.grader.evaluate(intent,contract)
    if not decision.verified:
        print([app.store.read_json(app.store.get('evidence',e)['report_digest']) for e in decision.evidence_ids])
    assert decision.verified


def wheel_contract():
    return freeze_contract(Contract(deliverable='artifact',requirements=[Requirement(id='R',description='Wheel answer returns42',subjects=[Subject.SOURCE,Subject.ARTIFACT])],
        checks=[Check(id='src',kind='function',definition={'module':'main','function':'answer','cases':[{'expected':42}]}),
            Check(id='build',kind='build',subject=Subject.BUILD,definition={'argv':['python','build.py'],'output':'dist/sample.whl'}),
            Check(id='wheel',kind='artifact_function',subject=Subject.ARTIFACT,definition={'module':'main','function':'answer','cases':[{'expected':42}]})],
        required_checks=['src','build','wheel'],global_required_checks=['build'],coverage={'R':['src','wheel']}),'operator')

@pytest.mark.asyncio
async def test_source_build_artifact_chain(app,source):
    build="import zipfile,pathlib\npathlib.Path('dist').mkdir(exist_ok=True)\nwith zipfile.ZipFile('dist/sample.whl','w') as z:z.write('main.py','main.py')\n"
    (source/'build.py').write_text(build);contract=wheel_contract();run=new_run(app,source,contract)
    intent=app.runtime.prepare(run['id']);decision=await app.grader.evaluate(intent,contract)
    assert decision.verified, [app.store.get('evidence',e) for e in decision.evidence_ids]
    assert decision.artifact_digest is not None
    es=[app.store.get('evidence',e) for e in decision.evidence_ids]
    assert es[0]['tested_artifact'] is None
    assert es[1]['built_artifact']==es[2]['tested_artifact']==decision.artifact_digest
    assert app.runtime.commit(intent,decision)['status']=='COMMITTED'

@pytest.mark.asyncio
async def test_wrong_wheel_cannot_fall_back_to_source(app,source):
    (source/'build.py').write_text("import zipfile,pathlib\npathlib.Path('dist').mkdir(exist_ok=True)\nwith zipfile.ZipFile('dist/sample.whl','w') as z:z.writestr('other.py','def answer():return 42')\n")
    contract=wheel_contract();run=new_run(app,source,contract)
    intent=app.runtime.prepare(run['id']);decision=await app.grader.evaluate(intent,contract)
    assert not decision.verified

@pytest.mark.asyncio
async def test_fake_pytest_success_is_only_diagnostic(app,source):
    (source/'fake.py').write_text("print('100 passed')")
    contract=make_contract()
    run=new_run(app,source,contract,content='def answer():return 0');i=app.runtime.prepare(run['id']);d=await app.grader.evaluate(i,contract)
    # Diagnostics are supplemental, not mandatory acceptance. Keep the actual
    # subprocess observation while asserting it cannot certify the behavior.
    ev,_=await app.grader._check(i,Check(id='pytest',kind='diagnostic',definition={'argv':['python','fake.py']}),None)
    assert ev.status=='PASSED' and ev.authority.value=='SANDBOX_IN_PROCESS'
    assert not d.verified

@pytest.mark.asyncio
async def test_invalid_tool_call_feedback_does_not_execute(app,source,contract):
    p=ScriptedProvider([assistant_tool('create',{'path':'bad.txt','content':'x','extra':'not allowed'}),Assistant(text='done')]);set_provider(app,p)
    run=new_run(app,source,contract);r=await app.execute(run['id'])
    assert r['state']=='SUCCEEDED'
    assert not app.runtime.workspace(run).path('bad.txt').exists()
    assert any(m.get('is_error') for m in app.store.messages(run['id']))

@pytest.mark.asyncio
async def test_readonly_child_cannot_write(app,source,contract):
    p=ScriptedProvider([assistant_tool('create',{'path':'pwn','content':'x'})]);set_provider(app,p)
    from conftest import tool
    run=new_run(app,source,contract)
    _,r=await tool(app,run,'delegate',{'goal':'Explore','max_turns':1})
    assert r['status']=='FAILED' and r['result']['code']=='CHILD_CAPABILITY_DENIED'
    assert not app.runtime.workspace(run).path('pwn').exists()

@pytest.mark.asyncio
async def test_readonly_child_returns_sourced_findings(app,source,contract):
    p=ScriptedProvider([assistant_tool('read',{'path':'main.py'}),Assistant(text='main.py answer returns 42')]);set_provider(app,p)
    from conftest import tool
    run=new_run(app,source,contract)
    _,r=await tool(app,run,'delegate',{'goal':'Explore'})
    assert r['status']=='SUCCEEDED' and '42' in r['result']['result']
    assert app.runtime.run(run['id'])['data']['budget_used']['child_calls']==1
