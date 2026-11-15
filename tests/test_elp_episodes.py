import copy
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from repo_code.episodes import EpisodeBuilder
from repo_code.observation import Observer,EvolutionQueue
from repo_code.storage import Store
from repo_code.util import RepoError
from test_review_workflow import setup,tool


@pytest.mark.asyncio
async def test_single_failure_stage_survives_reads_and_preserves_exact_references(tmp_path):
    app,run=setup(tmp_path)
    try:
        await tool(app,run,'create',{'path':'fixed.py','content':'value=1'},'write')
        with app.store.tx() as c:
            app.store.emit(c,run['id'],'tool.operation_finished',{'tool_name':'process.run','action_digest':'check',
                'result':{'exit_code':1,'is_error':True,'relevant_state':'source','stderr':'AssertionError'}},'DEV')
        for i in range(65):
            await tool(app,run,'read',{'path':'a.py','start':i+1,'lines':1},'read-'+str(i))
        app.runtime.end(run['id'],'PARTIAL','focused check failed')
        ids=EpisodeBuilder(app.store).capture_run(run['id'])
        assert len(ids)==1
        episode=EpisodeBuilder(app.store).get(ids[0])
        assert episode['status']=='CANDIDATE' and episode['priority']=='HIGH'
        assert len(episode['support_refs'])>65 and episode['root_cause_hypotheses']==[]
        from repo_code.evolution import EvolutionEngine
        pattern=next(p for p in app.store.values('pattern') if ids[0] in p.get('episode_refs',[]))
        exact=EvolutionEngine(app,app.provider).evidence(pattern['id'])
        assert exact['evidence_mode']=='EXACT_EPISODE_REFERENCES' and len(exact['events'])>65
        assert Observer(app.store).consume()==[]  # The legacy uninterrupted streak still resets.
        evidence=EpisodeBuilder(app.store).evidence(ids[0])
        assert any(e['type']=='tool.operation_finished' and e['data']['tool_name']=='read' for e in evidence['events'])
        assert EpisodeBuilder(app.store).capture_run(run['id'])==ids
        assert len(app.store.values('l1_episode'))==1
        assert not app.store.values('knowledge')
        assert EvolutionQueue(app.store).claim() is None
        job=EvolutionQueue(app.store).claim(kind='L1_CURATE')
        assert job['problem']==ids[0]
    finally:app.close()


@pytest.mark.asyncio
async def test_automatic_terminal_hook_and_ordinary_success_no_curator(tmp_path):
    app,run=setup(tmp_path)
    try:
        # No writes or failures: scripted handoff is a LOW signal.
        await app.execute(run['id'])
        assert not app.store.values('l1_episode')
        assert app.store.values('trace_manifest')
        with app.store.connect() as c:assert not c.execute("SELECT 1 FROM jobs WHERE kind='L1_CURATE'").fetchone()
        assert app.provider.calls==1
    finally:app.close()


@pytest.mark.asyncio
async def test_episode_revocation_and_hidden_exposure_block_reads_and_jobs(tmp_path):
    app,run=setup(tmp_path)
    try:
        await tool(app,run,'create',{'path':'fixed.py','content':'value=1'},'write')
        app.runtime.end(run['id'],'PARTIAL','not finished')
        builder=EpisodeBuilder(app.store);eid,=builder.capture_run(run['id'])
        event=builder.get(eid)['support_refs'][0]
        with app.store.tx() as c:app.store.put('revocation',event,{'reason':'operator'},c)
        with pytest.raises(RepoError,match='EPISODE_SOURCE_REVOKED'):builder.get(eid)
        assert builder.capture_run(run['id'])==[]
        assert EvolutionQueue(app.store).claim(kind='L1_CURATE') is None
    finally:app.close()


@pytest.mark.parametrize('visibility,reason,extra',[
    ('TRUSTED_ONLY','failed',None),('DEV','BLOCKED_ENV_IMPORT',None),
    ('DEV','ISOLATION_REQUIRED',None),('DEV','USAGE_UNSETTLED',None),
    ('DEV','failed',{'test_patch':'forbidden'}),
])
def test_denied_data_kept_out_of_episodes(tmp_path,visibility,reason,extra):
    app,run=setup(tmp_path)
    try:
        with app.store.tx() as c:
            current=app.runtime.run(run['id'],c);current['data']['visibility']=visibility
            app.runtime.update(c,current,data=current['data'])
            if extra:app.store.emit(c,run['id'],'trusted-input',extra,'DEV')
        app.runtime.end(run['id'],'PARTIAL',reason)
        assert EpisodeBuilder(app.store).capture_run(run['id'])==[]
        assert not app.store.values('l1_episode')
        assert app.store.values('l1_triage_audit')[0]['priority']=='DENY'
    finally:app.close()


@pytest.mark.asyncio
async def test_crash_and_concurrent_capture_do_not_duplicate_objects(tmp_path,monkeypatch):
    app,run=setup(tmp_path)
    try:
        await tool(app,run,'create',{'path':'fixed.py','content':'value=1'},'write')
        app.runtime.end(run['id'],'PARTIAL','failed')
        original=app.store.put
        def broken(kind,*a,**kw):
            original(kind,*a,**kw)
            if kind=='l1_episode':raise RuntimeError('crash before cursor')
        monkeypatch.setattr(app.store,'put',broken)
        with pytest.raises(RuntimeError):EpisodeBuilder(app.store).capture_run(run['id'])
        assert not app.store.values('l1_episode') and not app.store.values('trace_manifest')
        monkeypatch.setattr(app.store,'put',original)
        with ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(EpisodeBuilder(Store(app.store.root)).capture_run,run['id']) for _ in range(2)]
            values=[f.result() for f in futures]
        assert values[0]==values[1] and len(app.store.values('l1_episode'))==1
        with app.store.connect() as c:assert c.execute("SELECT count(*) FROM jobs WHERE kind='L1_CURATE'").fetchone()[0]==1
    finally:app.close()


def test_legacy_observer_rolls_back_problem_and_cursor_atomically(tmp_path,monkeypatch):
    store=Store(tmp_path/'state')
    with store.tx() as c:
        for _ in range(2):store.emit(c,'run','tool.operation_finished',
            {'tool_name':'process.run','action_digest':'a','result':{'is_error':True,'relevant_state':'s','stderr':'AssertionError'}})
    original=store.put
    def broken(kind,*a,**kw):
        original(kind,*a,**kw)
        if kind=='problem':raise RuntimeError('crash after problem before cursor')
    monkeypatch.setattr(store,'put',broken)
    with pytest.raises(RuntimeError):Observer(store).consume()
    assert not store.values('problem') and not store.values('observer_run')
    monkeypatch.setattr(store,'put',original)
    assert len(Observer(store).consume())==1
    assert Observer(store).consume()==[]


def test_queue_owner_expiry_recovery_and_stale_worker_rejection(tmp_path,monkeypatch):
    import repo_code.observation as observation
    clock=[100.0];monkeypatch.setattr(observation,'now',lambda:clock[0])
    store=Store(tmp_path/'state')
    a=EvolutionQueue(store);b=EvolutionQueue(store)
    key=a.enqueue('problem')
    first=a.claim(lease_seconds=2)
    assert b.claim() is None
    clock[0]=103
    second=b.claim(lease_seconds=3)
    assert second['id']==key and first['owner_token']!=second['owner_token']
    with pytest.raises(RepoError,match='JOB_LEASE_LOST'):a.finish(key,owner_token=first['owner_token'])
    b.renew(key,second['owner_token'],10)
    b.finish(key,owner_token=second['owner_token'])


@pytest.mark.parametrize('role',['promotion','final'])
def test_exposed_source_is_denied_even_if_the_new_run_is_labelled_dev(tmp_path,role):
    app,run=setup(tmp_path)
    try:
        from repo_code.governance import Lineage
        Lineage(app.store).expose(run['id'],role,'protected-family')
        app.runtime.end(run['id'],'PARTIAL','failure')
        assert EpisodeBuilder(app.store).capture_run(run['id'])==[]
        assert app.store.values('l1_triage_audit')[0]['reason']=='EPISODE_EXPOSED_SOURCE'
    finally:app.close()


@pytest.mark.asyncio
async def test_recovery_pairs_same_check_across_a_real_source_change(tmp_path):
    app,run=setup(tmp_path)
    original=app.sandbox.run
    fails=[True]
    async def process(*a,**kw):
        value=await original(*a,**kw)
        if fails[0]:value.update(exit_code=1,is_error=True,stderr='AssertionError')
        return value
    app.sandbox.run=process
    try:
        await tool(app,run,'create',{'path':'fixed.py','content':'value=0'},'write')
        await tool(app,run,'process.run',{'argv':['pytest','-k','focused']},'failed')
        await tool(app,run,'read',{'path':'fixed.py'},'read')
        await tool(app,run,'edit',{'path':'fixed.py','old_text':'value=0','new_text':'value=1'},'repair')
        fails[0]=False
        await tool(app,run,'process.run',{'argv':['pytest','-k','focused']},'recheck')
        app.runtime.end(run['id'],'PARTIAL','ready for external')
        eid,=EpisodeBuilder(app.store).capture_run(run['id'])
        episode=EpisodeBuilder(app.store).get(eid)
        assert episode['stage_type']=='RECOVERY' and episode['outcome']=='RESOLVED'
        assert episode['actual']['terminal_state']=='PARTIAL'  # Stage success is not task acceptance.
    finally:app.close()


@pytest.mark.asyncio
async def test_changed_or_deleted_raw_event_invalidates_episode(tmp_path):
    app,run=setup(tmp_path)
    try:
        await tool(app,run,'create',{'path':'fixed.py','content':'value=1'},'write')
        app.runtime.end(run['id'],'PARTIAL','failed')
        builder=EpisodeBuilder(app.store);eid,=builder.capture_run(run['id'])
        event=builder.get(eid)['support_refs'][0]
        with app.store.tx() as c:c.execute('UPDATE events SET data=? WHERE event_id=?',('{"tampered":true}',event))
        with pytest.raises(RepoError,match='EPISODE_SOURCE_CHANGED'):builder.get(eid)
    finally:app.close()

@pytest.mark.parametrize('mode',['missing','corrupt','revoked'])
def test_bad_artifact_denies_once_without_poisoning_observer_cursor(tmp_path,mode):
    app,run=setup(tmp_path)
    try:
        ref=app.store.blob('agent-visible output')
        with app.store.tx() as c:
            app.store.emit(c,run['id'],'tool.operation_finished',{'tool_name':'process.run',
                'result':{'exit_code':1,'artifact':ref}},'DEV')
        if mode=='missing':(app.store.artifact_root/ref[7:]).unlink()
        elif mode=='corrupt':(app.store.artifact_root/ref[7:]).write_bytes(b'changed')
        else:
            with app.store.tx() as c:app.store.put('revocation',ref,{'reason':'operator'},c)
        app.runtime.end(run['id'],'PARTIAL','failure')
        assert Observer(app.store).consume()==[]
        assert not app.store.values('l1_episode')
        assert app.store.values('l1_triage_audit')[0]['priority']=='DENY'
        assert Observer(app.store).consume()==[]
    finally:app.close()

@pytest.mark.asyncio
async def test_lost_artifact_after_publication_rejects_curator_job(tmp_path):
    app,run=setup(tmp_path)
    try:
        ref=app.store.blob('owned evidence')
        with app.store.tx() as c:
            app.store.emit(c,run['id'],'tool.operation_finished',{'tool_name':'process.run',
                'result':{'exit_code':1,'artifact':ref}},'DEV')
        app.runtime.end(run['id'],'PARTIAL','failure')
        eid,=EpisodeBuilder(app.store).capture_run(run['id'])
        (app.store.artifact_root/ref[7:]).unlink()
        with pytest.raises(RepoError,match='EPISODE_SOURCE_MISSING'):EpisodeBuilder(app.store).get(eid)
        assert EvolutionQueue(app.store).claim(kind='L1_CURATE') is None
        with app.store.connect() as c:
            assert c.execute("SELECT state FROM jobs WHERE kind='L1_CURATE'").fetchone()[0]=='REJECTED'
    finally:app.close()
