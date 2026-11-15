"""Invalid model parameters must return actionable errors without executing tools."""
import json

import pytest

from repo_code.models import Assistant,ToolCall,Budget
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode


@pytest.mark.asyncio
async def test_invalid_process_timeout_returns_field_limit_without_execution(tmp_path):
    received=[]
    def reply(system,messages,tools,index):
        if index==0:
            return Assistant(tool_calls=[ToolCall(id='bad-timeout',name='process.run',
                arguments={'argv':['python','-c','raise RuntimeError("must not run")'],'timeout':10000})])
        feedback=json.loads(messages[-1]['content']);received.append(feedback)
        return Assistant(text='The timeout was invalid; no process was executed.')
    app=RepoCode(tmp_path/'state',ScriptedProvider(handler=reply),sandbox='local')
    work=tmp_path/'work';work.mkdir()
    run=app.new_task('Exercise invalid timeout feedback',work,budget=Budget(model_calls=2),authorize_process=True)
    result=await app.execute(run['id'])
    assert result['state']=='PARTIAL'
    assert received[0]['code']=='INVALID_ARGUMENT'
    error=received[0]['details'][0]
    assert error['field']=='timeout' and error['type']=='less_than_equal' and '600' in error['message']
    assert 'input' not in error and '10000' not in json.dumps(received[0])
    assert not app.store.values('lease')
    with app.store.connect() as conn:assert conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
