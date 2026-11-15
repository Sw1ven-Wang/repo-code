"""No paid API: exercise the actual Adapter composition for both memory arms."""
from pathlib import Path
import uuid
import pytest
from adapter import App,Environment
from repo_code.models import Budget
from repo_code.providers import ScriptedProvider
from repo_code.memory.models import MemoryProfile


@pytest.mark.asyncio
@pytest.mark.parametrize('arm',['A','D'])
async def test_memory_arm_runs_process_and_exports_in_independent_state(tmp_path,arm):
    env=Environment('repo-code-harbor-controller:2026-10-31','rc-memory-fixture-'+uuid.uuid4().hex[:10],activate=False)
    app=None
    try:
        r=await env.exec("python -c \"from pathlib import Path;Path('/testbed/code.py').write_text('before\\n')\"")
        assert r.return_code==0
        provider=ScriptedProvider([
            {'tool_calls':[{'id':'r','name':'read','arguments':{'path':'code.py'}}]},
            {'tool_calls':[{'id':'w','name':'create','arguments':{'path':'added.py','content':'new\n'}}]},
            {'tool_calls':[{'id':'p','name':'process.run','arguments':{'argv':['python','-c',"from pathlib import Path;assert Path('added.py').read_text()=='new\\n';print('MEMORY_AD_OK')"],'timeout':20}}]},
            {'text':'Completed.'}])
        profile=MemoryProfile(mode='lexical_only',learning_enabled=False) if arm=='D' else None
        app=App(tmp_path/arm,provider,env,{},'memory-fixture',memory_profile=profile)
        source=tmp_path/'source';files,_=await app.sandbox.export(source);app.sandbox.synced=files
        run=app.new_task('Exercise memory Adapter plumbing.',source,budget=Budget(model_calls=8,tool_calls=10,wall_seconds=120),authorize_write=True,authorize_process=True)
        app.broker.authorize_run(run['id'],True,True,False)
        assert (run['data']['manifest'].get('memory_schema_version')==2)==(arm=='D')
        result=await app.execute(run['id'])
        assert result['state']=='PARTIAL',result
        assert 'MEMORY_AD_OK' in str(app.store.messages(run['id']))
        assert (Path(result['workspace'])/'added.py').read_text()=='new\n'
        assert bool(app.memory)==(arm=='D')
        if app.memory:assert not app.memory.profile.learning_enabled
    finally:
        if app:app.close()
        await env.stop()
