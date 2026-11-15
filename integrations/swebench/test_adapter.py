import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import pytest
from adapter import (App,Environment,CostProvider,projected,patch_from_workspace,save)
from repo_code.models import Assistant,Budget
from repo_code.providers import ScriptedProvider,OpenAIProvider
from repo_code.storage import Store
from repo_code.util import RepoError
from repo_code.workspace import Workspace

def test_projection_excludes_gold_and_hints():
    row=dict(instance_id='x',repo='r',base_commit='a'*40,problem_statement='public issue',
        patch='secret',test_patch='secret',FAIL_TO_PASS=['secret'],PASS_TO_PASS=['secret'],hints_text='secret')
    assert set(projected(row))=={'instance_id','repo','base_commit','problem_statement'}
    assert 'secret' not in json.dumps(projected(row))

def git(path,*args):return subprocess.check_output(['git',*args],cwd=path)

def baseline(tmp_path):
    p=tmp_path/'base';p.mkdir();git(p,'init')
    git(p,'config','core.autocrlf','false')
    for name,text in [('change.py','before\n'),('delete.py','remove\n')]: (p/name).write_text(text)
    git(p,'add','.');git(p,'-c','user.name=fixture','-c','user.email=fixture@localhost','commit','-m','fixture')
    source=tmp_path/'source';shutil.copytree(p,source,ignore=shutil.ignore_patterns('.git'))
    work=tmp_path/'work';shutil.copytree(source,work)
    return p,source,work

def test_patch_create_modify_delete_and_actual_apply(tmp_path):
    base,source,work=baseline(tmp_path)
    (work/'change.py').write_text('after\n');(work/'delete.py').unlink();(work/'new.py').write_text('new\n')
    patch,changed=patch_from_workspace(base,source,work,tmp_path/'replay')
    assert changed==['change.py','delete.py','new.py']
    subprocess.run(['git','apply','--check','-'],cwd=base,input=patch,check=True)
    subprocess.run(['git','apply','-'],cwd=base,input=patch,check=True)
    assert (base/'change.py').read_text()=='after\n' and not (base/'delete.py').exists()
    assert (base/'new.py').read_text()=='new\n'

def test_empty_patch_and_judge_change_rejected(tmp_path):
    base,source,work=baseline(tmp_path)
    patch,changed=patch_from_workspace(base,source,work,tmp_path/'empty-replay')
    assert patch==b'' and not changed
    (work/'conftest.py').write_text('raise SystemExit()\n')
    with pytest.raises(RepoError) as error:
        patch_from_workspace(base,source,work,tmp_path/'invalid-replay')
    assert error.value.code=='PATCH_TEST_OR_JUDGE_CHANGE_DENIED'

def test_git_mode_change_and_ignored_runtime_binary(tmp_path):
    base,source,work=baseline(tmp_path)
    for p in (base,source,work):(p/'.gitignore').write_text('ignored.bin\n')
    git(base,'add','.gitignore');git(base,'-c','user.name=fixture','-c','user.email=fixture@localhost','commit','-m','ignore runtime output')
    (work/'ignored.bin').write_bytes(b'\0runtime output')
    (work/'change.py').chmod(0o755)
    patch,changed=patch_from_workspace(base,source,work,tmp_path/'mode-replay')
    assert changed==['change.py'] and b'new mode 100755' in patch
    assert b'ignored.bin' not in patch

def test_workspace_limits_remain_in_force(tmp_path):
    source=tmp_path/'files';source.mkdir();store=Store(tmp_path/'state')
    for i in range(17):
        with (source/str(i)).open('wb') as f:f.truncate(8*1024*1024)
    with pytest.raises(RepoError,match='WORKSPACE_LIMIT'):Workspace(source,store).manifest()

@pytest.mark.asyncio
async def test_cost_limit_prevents_api_and_failed_usage_settles(tmp_path,monkeypatch):
    ledger=tmp_path/'cost.json';save(ledger,{'limit_cny':0,'charged_upper_cny':0,'halt':None})
    calls=[]
    async def fake(self,*args,**kwargs):
        calls.append(1);self.last_usage={'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}
        raise RepoError('MODEL_ERROR')
    monkeypatch.setattr(OpenAIProvider,'complete',fake)
    p=CostProvider(ledger,tmp_path/'usage.json',model='fixture',client=object(),max_tokens=8192)
    with pytest.raises(RepoError,match='PILOT_COST_LIMIT'):await p.complete('',[],[])
    assert not calls
    save(ledger,{'limit_cny':20,'charged_upper_cny':0,'halt':None})
    with pytest.raises(RepoError,match='MODEL_ERROR'):await p.complete('',[],[])
    record=json.loads(ledger.read_text())
    assert record['charged_upper_cny']==pytest.approx((10*2+2*8)/1e6)
    assert record['halt']=='MODEL_ERROR'
    with pytest.raises(RepoError,match='PILOT_COST_OR_PROTOCOL_STOP'):await p.complete('',[],[])
    assert len(calls)==1

@pytest.mark.asyncio
@pytest.mark.parametrize('failure',[None,'MODEL_ERROR','BUDGET'])
async def test_original_runtime_isolated_read_write_process_and_states(tmp_path,failure):
    env=Environment('repo-code-harbor-controller:2026-10-31','rc-dev-fixture-'+tmp_path.name,activate=False)
    try:
        r=await env.exec("python -c \"from pathlib import Path;Path('/testbed/fixture.py').write_text('before\\n')\"")
        assert r.return_code==0
        if failure=='MODEL_ERROR':provider=ScriptedProvider([RepoError('MODEL_ERROR')])
        else:provider=ScriptedProvider([
            {'tool_calls':[{'id':'r','name':'read','arguments':{'path':'fixture.py'}}]},
            {'tool_calls':[{'id':'w','name':'create','arguments':{'path':'added.py','content':'added\n'}}]},
            {'tool_calls':[{'id':'p','name':'process.run','arguments':{'argv':['python','-c',"from pathlib import Path;assert Path('added.py').read_text()=='added\\n';print('adapter-ok')"],'timeout':30}}]},
            {'text':'Fixture complete.'}])
        app=App(tmp_path/'state',provider,env,{'fixture':'pinned'},'fixture-adapter')
        source=tmp_path/'source';files,meta=await app.sandbox.export(source);app.sandbox.synced=files
        assert not (source/'.git').exists()
        run=app.new_task('Offline adapter fixture only; no benchmark score.',source,
            budget=Budget(model_calls=1 if failure=='BUDGET' else 10,tool_calls=10,wall_seconds=120),authorize_write=True,authorize_process=True)
        app.broker.authorize_run(run['id'],True,True,False)
        result=await app.execute(run['id'])
        if failure=='MODEL_ERROR':assert result['state']=='BLOCKED' and result['reason']=='MODEL_ERROR'
        elif failure=='BUDGET':assert result['state']=='PARTIAL' and result['reason']=='BUDGET_EXHAUSTED'
        else:
            assert result['state']=='PARTIAL' and result['reason'].startswith('CANDIDATE_READY_EXTERNAL_EVALUATION')
            assert (Path(result['workspace'])/'added.py').read_text()=='added\n'
            assert 'adapter-ok' in json.dumps(app.store.messages(run['id']))
            assert result['budget_used']['tool_calls']==3
        env.container.reload();assert env.container.attrs['HostConfig']['NetworkMode']==env.network_policy['docker_network']
        assert not env.container.attrs.get('Mounts')
    finally:await env.stop()

@pytest.mark.asyncio
@pytest.mark.parametrize('delete',[False,True])
async def test_readonly_asset_stays_in_task_and_mutation_is_rejected(tmp_path,delete):
    import hashlib
    env=Environment('repo-code-harbor-controller:2026-10-31','rc-dev-assets-'+tmp_path.name,activate=False)
    try:
        r=await env.exec("python -c \"from pathlib import Path;p=Path('/testbed/doc/images/fixture.gif');p.parent.mkdir(parents=True);p.write_bytes(b'asset');Path('/testbed/code.py').write_text('pass\\n')\"")
        assert r.return_code==0
        env.readonly_assets={'doc/images/fixture.gif':{'bytes':5,'sha256':hashlib.sha256(b'asset').hexdigest()}}
        app=App(tmp_path/'state',ScriptedProvider(),env,{},'fixture')
        source=tmp_path/'source';files,_=await app.sandbox.export(source)
        assert 'code.py' in files and 'doc/images/fixture.gif' not in files
        assert not (source/'doc/images/fixture.gif').exists()
        mutation='p.unlink()' if delete else "p.write_bytes(b'changed')"
        r=await env.exec("python -c \"from pathlib import Path;p=Path('/testbed/doc/images/fixture.gif');assert p.read_bytes()==b'asset';"+mutation+"\"")
        assert r.return_code==0
        with pytest.raises(RepoError) as error:await app.sandbox.export(tmp_path/'after')
        assert error.value.code=='HARBOR_TRANSPORT_FAILED' and 'READONLY_ASSET_CHANGED' in str(error.value)
    finally:await env.stop()
