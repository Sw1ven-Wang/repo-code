"""Explicit real-container adapter tests; never model benchmark scores."""
import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor_repo_code.agent import HarborRepoCode
from harbor_repo_code.bridge import host_path
from repo_code.models import Assistant,Budget,OperationRequest,ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.workspace import Workspace


class DockerTransport:
    def __init__(self,container):
        self.container=container;self.executions=0;self.drop_reply=False
        self.workdir='/repo-code-fixture'
        self._owned_container=container;self.started=asyncio.Event()
    async def exec(self,command,cwd=None,timeout_sec=120,**kwargs):
        is_process='proc=subprocess.Popen' in command
        if is_process:self.executions+=1;self.started.set()
        result=await asyncio.to_thread(subprocess.run,
            ['docker','exec','-w',cwd or self.workdir,self.container,'bash','-lc',command],
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout_sec)
        if is_process and self.drop_reply:raise ConnectionError('Injected lost execution reply')
        return SimpleNamespace(return_code=result.returncode,stdout=result.stdout,stderr=result.stderr)
    async def upload_file(self,source_path,target_path):
        await asyncio.to_thread(subprocess.run,['docker','cp',str(source_path),self.container+':'+target_path],check=True,capture_output=True)
    async def download_file(self,source_path,target_path):
        await asyncio.to_thread(subprocess.run,['docker','cp',self.container+':'+source_path,str(target_path)],check=True,capture_output=True)
    async def stop(self,delete=True):
        await asyncio.to_thread(subprocess.run,['docker','rm','-f',self.container],check=True,capture_output=True)


@pytest.fixture
def transport():
    image=os.environ['REPO_CODE_HARBOR_TEST_IMAGE']
    cid=subprocess.check_output(['docker','run','-d','--network=none','--cap-drop=ALL',
        '--security-opt=no-new-privileges',image,'sleep','600'],text=True).strip()
    env=DockerTransport(cid)
    subprocess.run(['docker','exec',cid,'bash','-lc',
        'mkdir /repo-code-fixture && cd /repo-code-fixture && git init -q && git -c user.name=fixture -c user.email=fixture@localhost commit -q --allow-empty -m baseline'],check=True,capture_output=True)
    yield env
    subprocess.run(['docker','rm','-f',cid],capture_output=True)


async def app_and_run(tmp_path,transport,responses):
    app=HarborRepoCode(tmp_path/'host',ScriptedProvider(responses),transport,transport.workdir,os.environ['REPO_CODE_HARBOR_TEST_IMAGE'])
    source=host_path(tmp_path/'source');files,_=await app.sandbox.export(source);app.sandbox.synced=files
    run=app.new_task('Adapter integration fixture only',source,budget=Budget(model_calls=5,wall_seconds=120),authorize_write=True,authorize_process=True)
    return app,run


@pytest.mark.asyncio
async def test_original_loop_file_process_and_idempotent_receipt(tmp_path,transport):
    responses=[Assistant(tool_calls=[ToolCall(id='write',name='create',arguments={'path':'repo_code_probe.py','content':'print(42)\n'})]),
        Assistant(tool_calls=[ToolCall(id='execute',name='process.run',arguments={'argv':['python3','repo_code_probe.py']})]),Assistant(text='Done')]
    app,run=await app_and_run(tmp_path,transport,responses)
    result=await app.execute(run['id'])
    assert result['state']=='PARTIAL' and result['reason'].startswith('EXPLORATION_ONLY')
    assert result['delivery'] is None
    calls=[x['request'] for x in app.store.values('host_call') if x['request']['tool_name']=='process.run']
    request=OperationRequest.model_validate(calls[0])
    receipt=await app.broker.submit(request)
    assert receipt['status']=='SUCCEEDED' and receipt['result']['stdout']=='42\n'
    assert transport.executions==1
    actual=await transport.exec('python3 repo_code_probe.py')
    assert actual.stdout=='42\n'
    patch=await transport.exec('git add -N -- repo_code_probe.py && git --no-pager diff --binary -- repo_code_probe.py')
    assert patch.return_code==0 and '+print(42)' in patch.stdout


@pytest.mark.asyncio
async def test_lost_process_reply_is_uncertain_and_not_replayed(tmp_path,transport):
    responses=[Assistant(tool_calls=[ToolCall(id='execute',name='process.run',arguments={'argv':['python3','-c',"open('execution-count.txt','a').write('once\\n')"]})])]
    app,run=await app_and_run(tmp_path,transport,responses)
    transport.drop_reply=True
    result=await app.execute(run['id'])
    assert result['state']=='NEEDS_RECONCILIATION'
    request=OperationRequest.model_validate(app.store.values('host_call')[0]['request'])
    assert (await app.broker.submit(request))['status']=='UNCERTAIN'
    assert transport.executions==1
    assert any(x['state']=='UNCERTAIN' for x in app.store.values('lease'))


@pytest.mark.asyncio
async def test_cancel_stops_the_owned_task_container(tmp_path,transport):
    responses=[Assistant(tool_calls=[ToolCall(id='long',name='process.run',arguments={'argv':['sleep','60'],'timeout':90})])]
    app,run=await app_and_run(tmp_path,transport,responses)
    work=asyncio.create_task(app.execute(run['id']))
    await asyncio.wait_for(transport.started.wait(),timeout=30)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):await work
    inspect=subprocess.run(['docker','inspect',transport.container],capture_output=True)
    assert inspect.returncode!=0
    assert all(x['state']=='STOPPED' for x in app.store.values('lease'))
