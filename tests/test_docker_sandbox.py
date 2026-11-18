"""Explicit Docker tests: REPO_CODE_DOCKER_IMAGE must be a local pinned image."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from repo_code.acceptance import freeze_contract
from repo_code.models import Contract,Requirement,Check
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode

pytestmark=[pytest.mark.isolation,pytest.mark.docker_backend]


@pytest.mark.asyncio
async def test_benchmark_process_import_preserves_stage_protected_paths(docker_app,tmp_path):
    from repo_code.benchmark import TrustedStageLoader,admit_budget
    from repo_code.models import ToolCall
    from repo_code.util import digest
    seed=tmp_path/'bench-seed';seed.mkdir()
    run=docker_app.new_task('Web source',seed,conversation=True,authorize_write=True,authorize_process=True)
    admit_budget(docker_app.runtime,run['id'],experiment_digest=digest('docker-stage-control'),
        session_model_calls=200,session_tool_calls=1000,stage_model_calls=10,stage_tool_calls=20,wall_seconds=600,stage_seconds=120)
    ws=docker_app.runtime.workspace(docker_app.runtime.run(run['id']))
    TrustedStageLoader(docker_app.runtime).admit(run['id'],'web',{'src/index.html':b'protected','src/App.tsx':b'initial'},
        ['src/*'],protected_paths=['src/index.html'],prompt_digest=digest('Web source'),expected_before=ws.snapshot(),initial=True)
    call=ToolCall(id='forbidden-process-write',name='process.run',arguments={
        'argv':['python','-c',"open('src/index.html','w').write('forbidden')"],'expect_workspace_change':True})
    request=docker_app.broker.register_call(run['id'],'stage',call)
    receipt=await docker_app.broker.submit(request)
    assert receipt['status']=='FAILED' and receipt['result']['code']=='BENCHMARK_STAGE_SCOPE_DENIED'
    assert ws.path('src/index.html').read_bytes()==b'protected'


@pytest.fixture
def docker_app(tmp_path):
    image=os.getenv('REPO_CODE_DOCKER_IMAGE')
    if not image:pytest.skip('Set REPO_CODE_DOCKER_IMAGE to opt into Docker integration tests')
    app=RepoCode(tmp_path/'state',ScriptedProvider(),sandbox='docker',image=image)
    yield app
    for row in app.store.values('lease'):
        app.sandbox.stop_run(row['run_id'])


@pytest.mark.asyncio
async def test_docker_restrictions_and_no_host_credentials(docker_app,tmp_path,monkeypatch):
    monkeypatch.setenv('REPO_CODE_TEST_HOST_SECRET','sentinel')
    work=tmp_path/'work';work.mkdir()
    code='''import os,json,socket,pathlib
s=socket.socket();s.settimeout(1)
try:s.connect(('1.1.1.1',443));network_denied=False
except OSError:network_denied=True
finally:s.close()
try:pathlib.Path('/root-probe').write_text('x');root_readonly=False
except OSError:root_readonly=True
status=dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
print(json.dumps({'uid':os.getuid(),'network_denied':network_denied,'root_readonly':root_readonly,
 'capabilities':int(status['CapEff'].strip(),16),'no_new_privs':status['NoNewPrivs'].strip(),
 'credentials_absent':not any(k in os.environ for k in ['OPENAI_API_KEY','ANTHROPIC_API_KEY','REPO_CODE_TEST_HOST_SECRET']),
 'memory_limit':pathlib.Path('/sys/fs/cgroup/memory.max').read_text().strip()}))
'''
    result=await docker_app.sandbox.run('restrictions',work,['python','-c',code],timeout=15,readonly=True)
    assert result['exit_code']==0,result['stderr']
    actual=json.loads(result['stdout'])
    assert actual=={'uid':65534,'network_denied':True,'root_readonly':True,'capabilities':0,
                    'no_new_privs':'1','credentials_absent':True,'memory_limit':str(768*1024*1024)}


@pytest.mark.asyncio
async def test_docker_readonly_workspace_cannot_write(docker_app,tmp_path):
    work=tmp_path/'work';work.mkdir();(work/'keep.txt').write_text('original')
    code="from pathlib import Path\ntry:Path('keep.txt').write_text('changed')\nexcept OSError:print('DENIED')"
    result=await docker_app.sandbox.run('readonly',work,['python','-c',code],readonly=True)
    assert result['exit_code']==0 and result['stdout']=='DENIED\n'
    assert (work/'keep.txt').read_text()=='original'


@pytest.mark.asyncio
async def test_docker_process_exports_actual_file_effect(docker_app,tmp_path):
    work=tmp_path/'work';work.mkdir()
    result=await docker_app.sandbox.run('export',work,['python','-c',"from pathlib import Path;Path('new.txt').write_text('actual effect')"],export=True)
    assert result['exit_code']==0,result
    assert (work/'new.txt').read_text()=='actual effect'


@pytest.mark.asyncio
async def test_docker_timeout_removes_owned_container(docker_app,tmp_path):
    work=tmp_path/'work';work.mkdir()
    result=await docker_app.sandbox.run('timeout',work,['python','-c','import time;time.sleep(30)'],timeout=1)
    assert result['timeout'] and result['exit_code']!=0
    assert docker_app.sandbox.resources_stopped('timeout')
    probe=subprocess.run(['docker','ps','-aq','--filter','name=repo-code-'+result['lease_id']],capture_output=True,text=True,timeout=15)
    assert probe.returncode==0 and not probe.stdout.strip()


def test_docker_doctor_reports_actual_network_denial(docker_app,tmp_path):
    command=[sys.executable,'-m','repo_code','--state-root',str(tmp_path/'doctor-state'),
             '--sandbox','docker','--image',docker_app.sandbox.image,'doctor']
    result=subprocess.run(command,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['probe_passed'] and report['observation']['network_denied']


@pytest.mark.asyncio
@pytest.mark.parametrize('value,expected_pass',[(42,True),(0,False)])
async def test_docker_external_verdict_controls_delivery(docker_app,tmp_path,value,expected_pass):
    work=tmp_path/'work';work.mkdir();(work/'main.py').write_text(f'def answer():return {value}\n')
    contract=freeze_contract(Contract(requirements=[Requirement(id='R',description='Return integer 42')],
        checks=[Check(id='behavior',kind='function',definition={'module':'main','function':'answer','cases':[{'id':'value','expected':42}]})],
        required_checks=['behavior'],coverage={'R':['behavior']}),'docker-test-curator')
    run=docker_app.new_task('Return 42',work,contract)
    intent=docker_app.runtime.prepare(run['id'])
    decision=await docker_app.grader.evaluate(intent,contract)
    assert decision.verified is expected_pass
    committed=docker_app.runtime.commit(intent,decision)
    assert (committed['status']=='COMMITTED') is expected_pass


@pytest.mark.asyncio
async def test_docker_candidate_g4_completes_all_cases(docker_app):
    from repo_code.regression import CandidateRegression,validate_g4_report
    candidate=docker_app.releases.candidate('docker-g4',
        {'retry_policy':{'kind':'config','max_identical_failures':2,'require_new_evidence':True}},
        'Verify Docker candidate compatibility',['docker-g4-regression'])
    report=await CandidateRegression(docker_app).run(candidate)
    assert report['passed'],report['cases']
    assert validate_g4_report(report,candidate.candidate_release_digest,
        docker_app.releases.get(candidate.candidate_release_digest).components,
        docker_app.sandbox.environment_digest)
    boundary=next(case['details'] for case in report['cases'] if case['id']=='sandbox_boundaries')
    assert boundary['socket_created'] is True
    assert boundary['network_probe']=='outbound_tcp_connect'
    assert boundary['network_interfaces']==['lo']
