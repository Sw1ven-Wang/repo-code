"""Network capability checks; never calls any model API or installs the target package."""
import hashlib
import json
from pathlib import Path
import shlex
import uuid
import pytest
from adapter import ROOT,Environment,prepare_container,save
from repo_code.util import RepoError

def test_network_configuration_is_explicit(monkeypatch):
    monkeypatch.delenv('REPO_CODE_TASK_NETWORK',raising=False)
    monkeypatch.delenv('REPO_CODE_DOCKER_NETWORK',raising=False)
    assert Environment.policy()['mode']=='public'
    assert Environment.policy('offline')['docker_network']=='none'
    for mode in ('host','container:other','invalid'):
        with pytest.raises(RepoError):Environment.policy(mode)
    for name in ('host','bridge','none','name with spaces'):
        with pytest.raises(RepoError):Environment.policy('public',name)

PROBE=r'''
import hashlib,json,os,socket,sys,subprocess,urllib.request,zipfile
from pathlib import Path
import sqlfluff
source=Path('/testbed/src/sqlfluff/rules/L031.py')
before=hashlib.sha256(source.read_bytes()).hexdigest()
version_before=sqlfluff.__version__
addresses=sorted({item[4][0] for item in socket.getaddrinfo('pypi.org',443,type=socket.SOCK_STREAM)})
with urllib.request.urlopen('https://pypi.org/pypi/sqlfluff/0.7.0/json',timeout=30) as response:
    assert response.status==200
    metadata=json.load(response)
assert metadata['info']['version']=='0.7.0'
download=Path('/tmp/repo-code-network-download');download.mkdir()
command=[sys.executable,'-m','pip','download','sqlfluff==0.7.0','--no-deps','--only-binary=:all:',
    '--no-cache-dir','--disable-pip-version-check','--index-url','https://pypi.org/simple',
    '--timeout','30','--retries','0','-d',str(download)]
result=subprocess.run(command,capture_output=True,text=True,timeout=120)
if result.returncode:raise RuntimeError(result.stdout[-2000:]+result.stderr[-2000:])
wheels=list(download.glob('*.whl'));assert len(wheels)==1
wheel=wheels[0];sha=hashlib.sha256(wheel.read_bytes()).hexdigest()
entry=next(item for item in metadata['urls'] if item['filename']==wheel.name)
assert sha==entry['digests']['sha256']
with zipfile.ZipFile(wheel) as archive:
    name=next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))
    text=archive.read(name).decode()
    assert 'Name: sqlfluff' in text and 'Version: 0.7.0' in text
assert sqlfluff.__version__==version_before
assert hashlib.sha256(source.read_bytes()).hexdigest()==before
assert not any(k in os.environ for k in ['DEEPSEEK_API_KEY_REPO','OPENAI_API_KEY','ANTHROPIC_API_KEY'])
assert not Path('/var/run/docker.sock').exists()
print(json.dumps({'dns_addresses':addresses,'https_status':200,'downloaded_package':'sqlfluff==0.7.0',
    'download_path':str(wheel),'wheel_sha256':sha,'wheel_bytes':wheel.stat().st_size,
    'pypi_sha256_verified':True,'installed_version_unchanged':version_before,
    'baseline_rule_sha256_unchanged':before,'api_credentials_absent':True,'docker_socket_absent':True,
    'model_calls':0}))
'''

@pytest.mark.asyncio
async def test_live_dns_https_and_original_requested_download(monkeypatch):
    monkeypatch.setenv('REPO_CODE_TASK_NETWORK','public')
    task='sqlfluff__sqlfluff-1625'
    row={'instance_id':task,'repo':'sqlfluff/sqlfluff','base_commit':'14e1a23a3166b9a645a16de96f694c77a5d4abb7',
        'problem_statement':'Network capability probe only; no model execution.'}
    images={task:{'id':'sha256:b61e33ddbf217f64b2ce29abc39aa9e577e0eacdbcd1b885d78e4ecdfaff9069'}}
    env=await prepare_container(row,images,'rc-net-download-'+uuid.uuid4().hex[:10])
    try:
        result=await env.exec('python3 -I -c '+shlex.quote(PROBE),timeout_sec=180)
        assert result.return_code==0,result.stderr[-3000:]
        observed=json.loads(result.stdout)
        observed['isolation']=env.inspect_isolation()
        output=ROOT/'network-validation';output.mkdir(parents=True,exist_ok=True)
        destination=output/Path(observed['download_path']).name
        await env.download_file(observed['download_path'],destination)
        assert hashlib.sha256(destination.read_bytes()).hexdigest()==observed['wheel_sha256']
        observed['evidence_wheel']=str(destination)
        save(output/'public-network.json',observed)
    finally:await env.stop()

@pytest.mark.asyncio
async def test_explicit_offline_mode_remains_offline():
    env=Environment('repo-code-harbor-controller:2026-10-31','rc-net-offline-'+uuid.uuid4().hex[:10],activate=False,network_mode='offline')
    try:
        script="import json,os,socket;s=socket.socket();s.settimeout(2);allowed=False\ntry:s.connect(('1.1.1.1',443));allowed=True\nexcept OSError:pass\nfinally:s.close()\nassert not allowed;assert os.listdir('/sys/class/net')==['lo'];print(json.dumps({'egress_allowed':allowed,'interfaces':os.listdir('/sys/class/net')}))"
        result=await env.exec('python -I -c '+shlex.quote(script),timeout_sec=10)
        assert result.return_code==0,result.stderr
        observed=json.loads(result.stdout);observed['isolation']=env.inspect_isolation()
        save(ROOT/'network-validation/offline-network.json',observed)
    finally:await env.stop()
