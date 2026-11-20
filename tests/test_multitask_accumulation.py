"""Offline Host contracts, not evidence of real-model learning benefit."""
import pytest
from repo_code.service import RepoCode
from repo_code.providers import ScriptedProvider
from repo_code.models import Assistant,ToolCall,Budget
from repo_code.util import RepoError


def test_collection_cli_arguments():
    from repo_code.cli import parser
    p=parser()
    a=p.parse_args(['session','create-collection','--scope','project:shared','--name','learning'])
    assert a.session_action=='create-collection' and a.scope=='project:shared'
    a=p.parse_args(['session','accumulation','collection-id'])
    assert a.session_action=='accumulation' and a.session_id=='collection-id'


async def failed_task(app,sid,workspace):
    app.agent.provider=app.provider=ScriptedProvider([
        Assistant(tool_calls=[ToolCall(id='probe',name='process.run',arguments={
            'argv':['python','-c','assert False, "boundary failure"']})]),
        Assistant(text='Unable to complete this task.')])
    run=app.new_task('Repair a boundary failure',workspace,session_id=sid,
        task_family='boundary-validation',authorize_process=True,budget=Budget(model_calls=3,tool_calls=5))
    await app.execute(run['id'])
    return run


@pytest.mark.asyncio
async def test_unverified_errors_are_counted_across_tasks_after_restart(tmp_path):
    app=RepoCode(tmp_path/'state',ScriptedProvider(),'local')
    try:
        sid=app.session_experiences().create_collection('project:shared','boundary-learning')
        active=app.releases.active()
        workspaces=[]
        for name in ['one','two']:
            w=tmp_path/name;w.mkdir();(w/'main.py').write_text('value=1\n');workspaces.append(w)
        first=await failed_task(app,sid,workspaces[0])
        a=app.session_experiences().accumulation(sid)
        assert a['distinct_tasks']==1 and a['raw_tool_error_observations']==1
        assert a['episodes']==a['stage_candidates']==[]
        app.close()
        app=RepoCode(tmp_path/'state',ScriptedProvider(),'local')
        assert app.session_experiences().create_collection('project:shared','boundary-learning')==sid
        second=await failed_task(app,sid,workspaces[1])
        b=app.session_experiences().accumulation(sid)
        assert b['distinct_tasks']==2 and b['raw_tool_error_observations']==2
        assert b['episodes']==b['stage_candidates']==b['patterns']==[]
        assert app.runtime.workspace(app.runtime.run(first['id'])).root!=app.runtime.workspace(app.runtime.run(second['id'])).root
        assert app.session_experiences().accumulation(sid)==b # No duplicate episodes/jobs.
        assert not app.store.values('candidate') and app.releases.active()==active
    finally:app.close()


@pytest.mark.asyncio
async def test_collection_scope_isolation_and_closed_recovery(tmp_path):
    app=RepoCode(tmp_path/'state',ScriptedProvider(),'local')
    try:
        sessions=app.session_experiences()
        sid=sessions.create_collection('project:a','learning')
        other=sessions.create_collection('project:b','learning')
        w=tmp_path/'work';w.mkdir();(w/'main.py').write_text('value=1\n')
        await failed_task(app,sid,w)
        assert sessions.accumulation(sid)['distinct_tasks']==1
        assert sessions.accumulation(other)['distinct_tasks']==0
        sessions.close(sid)
        with pytest.raises(RepoError,match='CLOSED'):
            sessions.create_collection('project:a','learning')
        for scope,name in [('', 'x'),('x',''),('x',None)]:
            with pytest.raises(RepoError,match='COLLECTION_SCOPE_OR_NAME'):
                sessions.create_collection(scope,name)
    finally:app.close()
