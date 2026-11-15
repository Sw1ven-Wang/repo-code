"""A pressure preview must expose tool data, including failing stderr tails."""
import json
import pytest
from repo_code.service import RepoCode
from repo_code.providers import ScriptedProvider
from repo_code.memory.models import MemoryProfile


@pytest.mark.asyncio
async def test_tiny_process_preview_keeps_stdout_and_stderr_evidence(tmp_path):
    source=tmp_path/'input';source.mkdir()
    app=RepoCode(tmp_path/'host',ScriptedProvider(),'local',memory_profile=MemoryProfile(mode='lexical_only',max_tokens=8000,reserve_tokens=4096,safety_tokens=500,evidence_tokens=128,learning_enabled=False))
    run=app.new_task('Investigate failing tests',source)
    raw=json.dumps({'cancelled':False,'environment_digest':'sha256:'+'e'*64,'elapsed':1,'exit_code':1,'is_error':True,
                    'stdout':'COLLECTED_TWO\n'+'details\n'*1000+'STDOUT_END',
                    'stderr':'TRACE_START\n'+'frame\n'*1000+'ASSERTION_FAILED','relevant_state':'sha256:'+'f'*64})
    app.store.append_message(run['id'],{'role':'assistant','content':'','tool_calls':[{'id':'probe','name':'process.run','arguments':{'argv':['python','-m','pytest']}}]})
    app.store.append_message(run['id'],{'role':'tool','tool_call_id':'probe','content':raw})
    before=app.store.messages(run['id'])
    _,messages,manifest=await app.memory.prepare_context(run['id'],[])
    value=json.loads(next(m['content'] for m in messages if m['role']=='tool'))
    preview=value['bounded_preview']
    assert 'stdout:' in preview and 'stderr:' in preview
    assert 'COLLECTED_TWO' in preview and 'ASSERTION_FAILED' in preview
    assert 'environment_digest' not in preview
    assert value['observed_metadata']['exit_code']==1 and value['observed_metadata']['is_error'] is True
    assert app.store.messages(run['id'])==before
    budget=manifest['budget_breakdown'];assert budget['estimated_input_total']+4096+500<=8000
    app.close()
