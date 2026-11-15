import os
import sys
from pathlib import Path
import pytest
from repo_code.models import Contract,Requirement,Check,Budget,ToolCall
from repo_code.acceptance import freeze_contract
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.storage import Store
from repo_code.sandbox import Sandbox

@pytest.fixture(scope='session')
def sandbox_kit(tmp_path_factory):
    if sys.platform!='linux' or os.geteuid()!=0:
        pytest.skip('Native isolation requires Linux root; no diagnostic-mode fallback')
    root=tmp_path_factory.mktemp('shared-kit')
    sandbox=Sandbox(Store(root/'state'),'native')
    existing=os.getenv('REPO_CODE_TEST_KIT')
    if existing and (Path(existing)/'READY').is_file():sandbox.kit=Path(existing)
    else:sandbox.prepare()
    return sandbox.kit

@pytest.fixture
def app(tmp_path,sandbox_kit):
    x=RepoCode(tmp_path/'state',ScriptedProvider(),'native');x.sandbox.kit=sandbox_kit
    yield x
    for row in x.store.values('lease'):
        if row['state']!='STOPPED':x.sandbox.stop_run(row['run_id'])

@pytest.fixture
def source(tmp_path):
    p=tmp_path/'source';p.mkdir();return p

@pytest.fixture
def contract():return make_contract()

def make_contract(expected=42,kind='function',extra=None):
    definition={'module':'main','function':'answer','cases':[{'id':'answer','expected':expected}]}
    if kind=='cli':definition={'cases':[{'id':'output','argv':['python','main.py'],'stdout':str(expected)+'\n'}]}
    checks=[Check(id='behavior',kind=kind,definition=definition)]
    if extra:checks+=extra
    return freeze_contract(Contract(requirements=[Requirement(id='R1',description='Returns the expected value')],checks=checks,required_checks=[x.id for x in checks],coverage={'R1':['behavior']},global_required_checks=[x.id for x in checks if x.id!='behavior']), 'test-operator')

def new_run(app,source,contract=None,content='def answer():\n    return 42\n',**kwargs):
    if content is not None:(source/'main.py').write_text(content)
    return app.new_task('Implement answer according to the contract',source,contract,kwargs.pop('budget',Budget(model_calls=10,wall_seconds=120)),True,True,**kwargs)

async def tool(app,run,name,args,turn='turn',call_id=None):
    from repo_code.util import uid
    c=ToolCall(id=call_id or uid('call-'),name=name,arguments=args)
    req=app.broker.register_call(run['id'],turn,c)
    return req,await app.broker.submit(req)
