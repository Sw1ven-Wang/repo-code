"""New source is a new memory profile; old pins are never hot-reinterpreted."""
import pytest
from repo_code.service import RepoCode
from repo_code.providers import ScriptedProvider
from repo_code.memory.models import MemoryProfile
from repo_code.util import RepoError
from test_memory_v2_contracts import memory_app


@pytest.mark.asyncio
async def test_ROL_01_changed_implementation_requires_new_run(memory_app,monkeypatch):
    app,run,_=memory_app;original=run['data']['manifest']
    monkeypatch.setattr('repo_code.memory.service.memory_implementation_digest',lambda:'sha256:'+'f'*64)
    changed=RepoCode(app.store.root,ScriptedProvider(),'local',memory_profile=MemoryProfile(mode='lexical_only'))
    with pytest.raises(RepoError,match='RUN_MEMORY_CONFIGURATION_CHANGED'):await changed.execute(run['id'])
    assert changed.runtime.run(run['id'])['data']['manifest']==original
    changed.close()


def test_explicit_old_implementation_profile_is_rejected(memory_app,monkeypatch):
    app,run,_=memory_app
    monkeypatch.setattr('repo_code.memory.service.memory_implementation_digest',lambda:'sha256:'+'e'*64)
    with pytest.raises(RepoError) as error:
        RepoCode(app.store.root,ScriptedProvider(),'local',memory_profile=app.memory.profile)
    assert error.value.code=='MEMORY_IMPLEMENTATION_CHANGED'


@pytest.mark.asyncio
async def test_ROL_02_disable_stops_new_and_pending_context_preserving_pins(memory_app):
    app,run,source=memory_app;manifest=run['data']['manifest']
    _,_,request=await app.memory.prepare_context(run['id'],[])
    app.memory.disable('Injected safety/budget incident')
    assert not app.memory.response_current(request)
    with pytest.raises(RepoError,match='MEMORY_PROFILE_DISABLED'):app.new_task('New canary',source)
    with pytest.raises(RepoError,match='MEMORY_PROFILE_DISABLED'):await app.memory.prepare_context(run['id'],[])
    assert app.runtime.run(run['id'])['data']['manifest']==manifest


def test_disabled_experimental_profile_does_not_change_default_legacy(memory_app):
    app,run,source=memory_app;app.memory.disable('Rollback fixture')
    legacy=RepoCode(app.store.root,ScriptedProvider(),'local')
    next_run=legacy.new_task('Legacy after rollback',source)
    assert next_run['data']['manifest'].get('memory_schema_version')!=2


@pytest.mark.asyncio
async def test_fresh_trial_states_never_import_previous_trial_writes(memory_app,tmp_path,monkeypatch):
    from repo_code.demo import task_fixture
    from repo_code.evaluation import TrialRunner
    from repo_code.sandbox import Sandbox
    app,run,_=memory_app
    app.runtime.end(run['id'],'PARTIAL','Stopped template fixture')
    observed=[]
    async def preflight(*args,**kw):return {'exit_code':0,'stdout':'PREFLIGHT_OK\n'}
    async def execute(trial,run_id,expose_control=False):
        assert trial.store.get('cross_task_probe','previous',None) is None
        trial.store.put('cross_task_probe','previous',{'should_not_transfer':True})
        assert trial.memory is not None and not trial.memory.profile.learning_enabled
        observed.append(trial.store.root)
        trial.runtime.end(run_id,'PARTIAL','Isolation plumbing fixture')
        return trial.agent.report(run_id)
    monkeypatch.setattr(Sandbox,'run',preflight)
    monkeypatch.setattr(RepoCode,'execute',execute)
    runner=TrialRunner(app,lambda public:ScriptedProvider(),isolated_states=True,trial_root=tmp_path/'trials')
    release=app.releases.active()['digest']
    for i in range(2):
        result=await runner.run(task_fixture('fresh-'+str(i)),release,'unseen',True)
        assert result['state']=='PARTIAL'
    assert observed[0]!=observed[1] and app.store.get('cross_task_probe','previous',None) is None


def test_environment_identity_includes_nested_memory_implementation(tmp_path,monkeypatch):
    import repo_code.sandbox as module
    from repo_code.storage import Store
    package=tmp_path/'fake-package';(package/'memory').mkdir(parents=True)
    (package/'sandbox.py').write_text('# stable launcher')
    nested=package/'memory'/'compaction.py';nested.write_text('version=1')
    monkeypatch.setattr(module,'__file__',str(package/'sandbox.py'))
    first=module.Sandbox(Store(tmp_path/'one'),'local')._host_build_digest
    nested.write_text('version=2')
    second=module.Sandbox(Store(tmp_path/'two'),'local')._host_build_digest
    assert first!=second


@pytest.mark.asyncio
async def test_trial_preflight_failure_blocks_created_run_before_paid_api(memory_app,tmp_path,monkeypatch):
    from repo_code.demo import task_fixture
    from repo_code.evaluation import TrialRunner
    from repo_code.sandbox import Sandbox
    from repo_code.storage import Store
    from repo_code.runtime import Runtime
    app,run,_=memory_app;app.runtime.end(run['id'],'PARTIAL','Stopped template fixture')
    async def failure(*args,**kw):return {'exit_code':1,'stdout':'','stderr':'injected workspace EIO'}
    monkeypatch.setattr(Sandbox,'run',failure)
    provider=ScriptedProvider()
    result=await TrialRunner(app,lambda public:provider,True,tmp_path/'trials').run(task_fixture('preflight-failure'),app.releases.active()['digest'],'new',True)
    assert result['state']=='INFRASTRUCTURE_ERROR' and result['error']=='INFRA_ERROR'
    assert result['cost']['model_calls']==0 and result['run_id']
    assert Runtime(Store(result['state_root'])).run(result['run_id'])['state']=='BLOCKED'


def test_shadow_migration_opens_source_readonly_and_preserves_database_bytes(memory_app,tmp_path):
    import subprocess,sys,json,hashlib
    from pathlib import Path
    app,run,_=memory_app;app.runtime.end(run['id'],'PARTIAL','Stopped shadow fixture')
    before=hashlib.sha256(app.store.db_path.read_bytes()).hexdigest()
    profile=tmp_path/'profile.json';profile.write_text(json.dumps({'mode':'lexical_only'}))
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,str(root/'scripts/migrate_memory_v2.py'),'--source-state',str(app.store.root),
        '--destination',str(tmp_path/'shadow'),'--profile',str(profile),'--reviewer','fixture-current-authority'],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert hashlib.sha256(app.store.db_path.read_bytes()).hexdigest()==before
    report=json.loads((tmp_path/'shadow/migration-report.json').read_text())
    assert report['legacy_run_manifests_unchanged'] and report['source_mutated'] is False
