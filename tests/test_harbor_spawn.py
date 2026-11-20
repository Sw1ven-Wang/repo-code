import io,json,subprocess,sys,tarfile
from pathlib import Path

import pytest

from harbor_repo_code.bridge import PROCESS,HarborSandbox
from repo_code.storage import Store
from repo_code.util import RepoError,digest

def test_missing_executable_returns_a_completed_failure(tmp_path):
    payload={'argv':['repo-code-nonexistent-executable-fixture'],'root':str(tmp_path),'stdin':'','timeout':5}
    p=subprocess.run([sys.executable,'-I','-c',PROCESS,json.dumps(payload)],capture_output=True,text=True,timeout=10)
    assert p.returncode==0,p.stderr
    result=json.loads(p.stdout)
    assert result['exit_code']==127 and result['timeout'] is False
    assert result['launch_error']=='EXECUTABLE_OR_CWD_NOT_FOUND'
    assert result['elapsed']<5


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'read-denied', 'removed'])
async def test_source_export_requires_materialized_fixture(tmp_path,monkeypatch,fault):
    fixture='test/files/sample.bin'
    data=b'benign binary fixture\x00'
    target=tmp_path/'mirror'

    class Environment:
        async def upload_file(self,local,remote):pass

        async def download_file(self,remote,local):
            with tarfile.open(local,'w') as archive:
                entry=tarfile.TarInfo(fixture)
                entry.size=len(data)
                archive.addfile(entry,io.BytesIO(data))

    sandbox=HarborSandbox(Store(tmp_path/'state'),Environment(),'/public-source','fixture')

    async def remote(script,payload):
        return {'current':{fixture:digest(data)},'modes':{fixture:0o644}}
    monkeypatch.setattr(sandbox,'remote',remote)

    if fault=='read-denied':
        original=Path.read_bytes

        def read(path):
            if path.name=='sample.bin':raise OSError(22,'fixture unavailable',str(path))
            return original(path)
        monkeypatch.setattr(Path,'read_bytes',read)
    elif fault=='removed':
        original=tarfile.TarFile.extractall

        def extract(archive,destination,**options):
            original(archive,destination,**options)
            (destination/fixture).unlink()
        monkeypatch.setattr(tarfile.TarFile,'extractall',extract)
        # A previous snapshot must not stand in for an entry removed during this export.
        sandbox.synced={fixture:{'blob':digest(data),'executable':False}}

    if fault:
        with pytest.raises(RepoError,match='sample.bin') as failed:
            await sandbox.export(target)
        assert failed.value.code=='HARBOR_SOURCE_MATERIALIZATION_FAILED'
        assert not sandbox.modes
    else:
        files,_=await sandbox.export(target)
        assert files[fixture]['blob']==digest(data)
        assert (target/fixture).read_bytes()==data
