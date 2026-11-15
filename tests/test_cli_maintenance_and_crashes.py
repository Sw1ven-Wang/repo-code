"""Real CLI, hard crash, backup and cross-process emergency control tests."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from repo_code.models import Assistant,ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.storage import Store
from repo_code.maintenance import Maintenance,restore_backup
from repo_code.delivery import Delivery
from repo_code.skills import import_skill,export_skill
from repo_code.mcp import validate_schema
from repo_code.util import RepoError
from conftest import new_run

ROOT=Path(__file__).resolve().parents[1]

def command(app,*args,timeout=30):
    env=os.environ.copy();env['PYTHONPATH']=str(ROOT/'src')
    result=subprocess.run([sys.executable,'-m','repo_code','--state-root',str(app.store.root),'--sandbox','native',*args],capture_output=True,text=True,env=env,timeout=timeout)
    return result,json.loads(result.stdout) if result.stdout.strip() else None


def test_cli_help_and_version():
    for arg in ('--help','--version'):
        p=subprocess.run([sys.executable,'-m','repo_code',arg],capture_output=True,text=True)
        assert p.returncode==0 and 'repo-code' in p.stdout

@pytest.mark.asyncio
async def test_real_cli_cancel_reaches_process_owner(app,source,contract):
    def scripted(system,messages,tools,i):
        return Assistant(tool_calls=[ToolCall(id='sleep',name='process.run',arguments={'argv':['python','-c','import time; time.sleep(30)'],'timeout':40})])
    app.agent.provider=app.broker.provider=ScriptedProvider(handler=scripted)
    run=new_run(app,source,contract)
    execution=asyncio.create_task(app.execute(run['id'],expose_control=True))
    try:
        for _ in range(300):
            if any(x['state']=='OWNED' for x in app.store.values('lease')):break
            await asyncio.sleep(.01)
        else:raise AssertionError('process never became owned')
        started=time.monotonic()
        result,data=await asyncio.to_thread(command,app,'control',run['id'],'cancel')
        assert result.returncode==0,(result.stdout,result.stderr)
        completed=await asyncio.wait_for(execution,10)
        assert completed['state']=='CANCELLED'
        assert time.monotonic()-started<10
        assert app.sandbox.resources_stopped(run['id'])
        assert app.store.get('executor_endpoint',run['id'])['state']=='CLOSED'
    finally:
        if not execution.done():execution.cancel()
        with contextlib.suppress(asyncio.CancelledError):await execution


def test_finish_control_cannot_trust_caller_stop_boolean(app,source,contract):
    run=new_run(app,source,contract)
    app.store.put('lease','uncertain-lease',{'id':'uncertain-lease','run_id':run['id'],'state':'OWNED','protocol':1})
    app.runtime.admit(run['id'],'pause','PAUSE')
    got=app.runtime.finish_control('pause','pause',resources_stopped=True)
    assert got['state']=='NEEDS_RECONCILIATION'
    # Test fixture removal is not a product force-resume operation.
    app.store.put('lease','uncertain-lease',{'id':'uncertain-lease','run_id':run['id'],'state':'STOPPED','result':{}})


def test_cli_contract_validation_and_skill_round_trip(app,tmp_path,contract):
    path=tmp_path/'contract.json';path.write_text(contract.model_dump_json())
    p,v=command(app,'contract','validate',str(path));assert p.returncode==0 and v['valid']
    skill=tmp_path/'SKILL.md';skill.write_text('---\nname: "pytest"\ndescription: "Run relevant tests"\n---\nRun tests, inspect failures, then change code.\n')
    p,v=command(app,'skill','import',str(skill));assert p.returncode==0 and v['status']=='CANDIDATE'
    out=tmp_path/'exported'/'SKILL.md';p,v2=command(app,'skill','export',v['id'],str(out));assert p.returncode==0 and out.is_file()
    assert app.store.get('knowledge',v['id'])['status']=='CANDIDATE'


def test_offline_backup_restore_preserves_signature_and_original(app,tmp_path):
    blob=app.store.blob('private original bytes');app.store.put('fixture','a',{'blob':blob})
    m=Maintenance(app.store);assert m.inspect()['integrity']=='ok'
    dest=tmp_path/'backup';m.backup(dest)
    restored=tmp_path/'restored';restore_backup(dest,restored)
    other=Store(restored)
    assert other.read_blob(blob)==b'private original bytes'
    assert other.verify({'x':1},app.store.sign({'x':1}))
    assert other.get('fixture','a')==app.store.get('fixture','a')
    with pytest.raises(RepoError,match='RESTORE_REQUIRES_NEW_DIRECTORY'):restore_backup(dest,restored)
    (dest/'artifacts'/blob[7:]).write_text('corrupt')
    with pytest.raises(RepoError,match='CORRUPT_BACKUP'):restore_backup(dest,tmp_path/'badrestore')


def test_half_restore_marker_and_maintenance_active_guard(app,source,contract,tmp_path):
    partial=tmp_path/'partial';partial.mkdir();(partial/'RESTORE_IN_PROGRESS').write_text('1')
    with pytest.raises(RepoError,match='INCOMPLETE_RESTORE'):Store(partial)
    run=new_run(app,source,contract)
    with pytest.raises(RepoError,match='MAINTENANCE_REQUIRES_STOPPED_HOST'):Maintenance(app.store).backup(tmp_path/'backup')
    app.runtime.end(run['id'],'PARTIAL','test ended')
    assert Maintenance(app.store).prune()['history_deleted'] is False
    with pytest.raises(RepoError,match='BACKUP_REQUIRED'):Maintenance(app.store).prune(apply=True)


def test_hard_exit_after_file_effect_before_response_recovers_original_operation(app,source,contract,tmp_path):
    run=new_run(app,source,contract)
    env=os.environ.copy();env['PYTHONPATH']=str(ROOT/'src')
    code=r'''
import asyncio,os,sys
from repo_code.service import RepoCode
from repo_code.providers import ScriptedProvider
from repo_code.models import ToolCall
from repo_code.util import Faults
app=RepoCode(sys.argv[1],ScriptedProvider(),'native')
req=app.broker.register_call(sys.argv[2],'crash-turn',ToolCall(id='crash-write',name='create',arguments={'path':'survivor.txt','content':'one effect'}))
class ExitFaults:
 def hit(self,name):
  if name=='effect_before_result_commit':os._exit(73)
app.broker.faults=ExitFaults()
asyncio.run(app.broker.submit(req))
raise RuntimeError('fault did not fire')
'''
    p=subprocess.run([sys.executable,'-c',code,str(app.store.root),run['id']],env=env,capture_output=True,text=True,timeout=20)
    assert p.returncode==73,(p.returncode,p.stderr)
    ws=app.runtime.workspace(run);assert ws.path('survivor.txt').read_text()=='one effect'
    with app.store.connect() as c:op=dict(c.execute('SELECT * FROM operations WHERE run_id=?',(run['id'],)).fetchone())
    app.runtime.recover(run['id']);assert app.broker.get(op['op_key'],run['id'])['status']=='UNCERTAIN'
    result=app.broker.reconcile(op['op_key'])
    assert result['status']=='SUCCEEDED'
    assert ws.path('survivor.txt').read_text()=='one effect'


def test_mcp_rejects_unsupported_schema_instead_of_ignoring(app):
    with pytest.raises(RepoError) as exc:validate_schema('oops',{'oneOf':[{'type':'integer'}]})
    assert exc.value.code=='MCP_SCHEMA_UNSUPPORTED'
    with pytest.raises(RepoError,match='MCP_ARGUMENT_RANGE'):validate_schema(-1,{'type':'integer','minimum':0})


def test_project_knowledge_not_injected_into_other_project(app,source,tmp_path,contract):
    from repo_code.knowledge import project_scope
    k=app.runtime.knowledge
    a=k.draft('memory','alpha','python private alpha',project_scope(source),['operator'])
    k.activate(a['id'],'operator')
    one=new_run(app,source,contract)
    other=tmp_path/'different-project';other.mkdir()
    two=new_run(app,other,contract)
    m1=one['data']['manifest'];m2=two['data']['manifest']
    assert m1['scope']!=m2['scope']
    assert k.search(m1['knowledge_snapshot_digest'],m1['knowledge_index_digest'],'alpha')
    assert not k.search(m2['knowledge_snapshot_digest'],m2['knowledge_index_digest'],'alpha')


def test_evolution_skill_cannot_bypass_full_release_gates(app):
    k=app.runtime.knowledge;x=k.draft('skill','s','b','user',['verified-run'])
    x['origin']='evolution';app.store.put('knowledge',x['id'],x)
    with pytest.raises(RepoError) as exc:k.activate(x['id'],'operator')
    assert exc.value.code=='RELEASE_GATE_REQUIRED'


def test_complexity_remove_replaces_extension_with_safe_default(app):
    from repo_code.governance import DEFAULTS
    parent=app.releases.candidate('p',{'planning_policy':{'kind':'config','instructions':'extra '*100}},'replace',[])
    removed=app.releases.candidate('p',{'planning_policy':{'kind':'config'}},'remove redundant extension',[],operation='REMOVE',base=parent.candidate_release_digest)
    assert app.releases.components(removed.candidate_release_digest)['planning_policy']==DEFAULTS['planning_policy']
    audit=app.store.get('candidate_state',removed.id)['structural_audit']
    assert audit['delta']['prompt_tokens']<0
    assert app.releases.retire(parent.id,'operator')['physical_deletion'] is False


def test_event_display_redacts_known_secret_shapes(app):
    with app.store.tx() as c:app.store.emit(c,'test','diagnostic',{'api_key':'secret-value','stderr':'password=hunter2'})
    event=app.store.events('test')[0]
    assert event['data']['api_key']=='[REDACTED]' and 'hunter2' not in str(event)

@pytest.mark.asyncio
async def test_metered_provider_does_not_reset_failed_attempt_budget():
    from repo_code.providers import MeteredProvider
    ledger={'calls':0,'limit':1,'usage':[]}
    class Failing:
        async def complete(self,*args,**kwargs):raise RepoError('UPSTREAM_ERROR')
    p=MeteredProvider(Failing(),ledger)
    with pytest.raises(RepoError):await p.complete('',[],[])
    with pytest.raises(RepoError) as exc:await p.complete('',[],[])
    assert exc.value.code=='EVOLUTION_TOTAL_BUDGET' and ledger['calls']==1

@pytest.mark.asyncio
async def test_one_run_cannot_have_two_executors(app,source,contract):
    from repo_code.util import execution_lock
    run=new_run(app,source,contract)
    with execution_lock(app.store.root/'executor_locks'/(run['id']+'.lock')):
        with pytest.raises(RepoError,match='EXECUTOR_ALREADY_ACTIVE'):await app.execute(run['id'])

@pytest.mark.asyncio
async def test_resume_rejects_different_model_configuration(app,source,contract):
    run=new_run(app,source,contract)
    app.agent.provider.model='different-model'
    with pytest.raises(RepoError) as exc:await app.execute(run['id'])
    assert exc.value.code=='RUN_MODEL_CONFIGURATION_CHANGED'


def test_cli_resume_after_approval_state_is_runnable(app,source,contract):
    run=new_run(app,source,contract)
    assert app.runtime.resume(run['id'])['state']=='RUNNING'


def test_gate_report_cannot_override_candidate_identity(app):
    c=app.releases.candidate('p',{'retry_policy':{'kind':'config'}},'h',[])
    with pytest.raises(RepoError,match='GATE_IDENTITY_OVERRIDE'):
        app.releases.record_gate(c,'G0',{'passed':True,'release':'other'})
