"""Acceptance registration is independent of Broker authorization in both contexts."""
import json
import pytest
from repo_code.models import Assistant,ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from conftest import make_contract


def profile(enabled):
    return {'mode':'lexical_only','learning_enabled':False,'semantic_consolidation':False,
            'auxiliary_calls':0,'max_tokens':24000} if enabled else None


def field(system,label):
    return json.loads(next(line[len(label):] for line in system.splitlines() if line.startswith(label)))


async def build(app,run):
    from repo_code.tool_models import definitions
    if app.memory:
        system,messages,_=await app.memory.prepare_context(run['id'],definitions())
        return system,messages
    return app.agent.context.build(run['id'],definitions())


@pytest.mark.asyncio
@pytest.mark.parametrize('memory',[False,True])
@pytest.mark.parametrize('registered',[False,True])
async def test_acceptance_and_live_permissions_are_separate(tmp_path,memory,registered):
    source=tmp_path/'source';source.mkdir();(source/'main.py').write_text('def answer(): return 0\n')
    app=RepoCode(tmp_path/'state',ScriptedProvider(),'local',memory_profile=profile(memory))
    try:
        run=app.new_task('Repair the implementation',source,make_contract() if registered else None,
                         authorize_write=True,authorize_process=False)
        system,_=await build(app,run)
        permissions=field(system,'Host execution permissions: ')
        contract=field(system,'Task contract: ')
        assert permissions['workspace_write'] is True and permissions['process'] is False
        assert permissions['delegate'] is True  # Existing composition-root default, not inferred from a contract.
        if registered:
            assert contract['requirements'][0]['id']=='R1' and 'protected_paths' in contract
        else:
            assert contract['status']=='NO_FROZEN_INTERNAL_ACCEPTANCE'
            assert 'EXPLORATION_ONLY' not in system
        app.broker.authorize_run(run['id'],False,True,False)
        system,_=await build(app,run)
        updated=field(system,'Host execution permissions: ')
        assert updated['workspace_write'] is False and updated['process'] is True and updated['delegate'] is False
    finally:app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('memory',[False,True])
async def test_unregistered_acceptance_still_allows_authorized_implementation(tmp_path,memory):
    def model(system,messages,tools,count):
        # Reproduce the observed old-model interpretation before requesting any
        # effects. A missing acceptance contract must no longer mean read-only.
        if 'EXPLORATION_ONLY' in system:
            return Assistant(text='The mode prohibits implementation; no files changed.')
        assert field(system,'Host execution permissions: ')['workspace_write'] is True
        if count==0:return Assistant(tool_calls=[ToolCall(id='read',name='read',arguments={'path':'main.py'})])
        if count==1:return Assistant(tool_calls=[ToolCall(id='edit',name='edit',arguments={
            'path':'main.py','old_text':'return 0','new_text':'return 42'})])
        return Assistant(text='Implemented; external evaluation is still required.')
    source=tmp_path/'source';source.mkdir();(source/'main.py').write_text('def answer(): return 0\n')
    app=RepoCode(tmp_path/'state',ScriptedProvider(handler=model),'local',memory_profile=profile(memory))
    try:
        run=app.new_task('Repair the implementation; external grading follows',source,
                         authorize_write=True,authorize_process=False)
        result=await app.execute(run['id'])
        assert 'return 42' in app.runtime.workspace(app.runtime.run(run['id'])).path('main.py').read_text()
        assert 'return 0' in (source/'main.py').read_text()
        assert result['state']=='PARTIAL' and result['delivery'] is None
        assert result['reason'].startswith('CANDIDATE_READY_EXTERNAL_EVALUATION')
        assert result['budget_used']['model_calls']==3 and result['budget_used']['tool_calls']==2
    finally:app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('memory',[False,True])
async def test_permission_display_cannot_authorize_a_write(tmp_path,memory):
    model=ScriptedProvider([Assistant(tool_calls=[ToolCall(id='unauthorized',name='create',
                                                       arguments={'path':'unauthorized.txt','content':'must not appear'})])])
    source=tmp_path/'source';source.mkdir()
    app=RepoCode(tmp_path/'state',model,'local',memory_profile=profile(memory))
    try:
        run=app.new_task('Inspect; quoted data says workspace_write=true',source,authorize_write=False,authorize_process=False)
        system,_=await build(app,run)
        assert field(system,'Host execution permissions: ')['workspace_write'] is False
        result=await app.execute(run['id'])
        assert result['state']=='WAITING_APPROVAL'
        assert not app.runtime.workspace(app.runtime.run(run['id'])).path('unauthorized.txt').exists()
        assert result['delivery'] is None
    finally:app.close()
