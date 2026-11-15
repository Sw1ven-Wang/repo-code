import json,subprocess,sys
from harbor_repo_code.bridge import PROCESS

def test_missing_executable_returns_a_completed_failure(tmp_path):
    payload={'argv':['repo-code-nonexistent-executable-fixture'],'root':str(tmp_path),'stdin':'','timeout':5}
    p=subprocess.run([sys.executable,'-I','-c',PROCESS,json.dumps(payload)],capture_output=True,text=True,timeout=10)
    assert p.returncode==0,p.stderr
    result=json.loads(p.stdout)
    assert result['exit_code']==127 and result['timeout'] is False
    assert result['launch_error']=='EXECUTABLE_OR_CWD_NOT_FOUND'
    assert result['elapsed']<5
