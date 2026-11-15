"""Negative/positive controls beyond the seven original review reproductions.

Portable tests exercise real SQLite, Broker and files without claiming OS
isolation. Native tests run candidate Python and MCP in the isolated backend.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from conftest import new_run, tool
from test_review_v151 import portable, human_contract, Sink, EOFReader
from repo_code.acceptance import delivery_verified, freeze_contract
from repo_code.delivery import Delivery
from repo_code.evolution import EvolutionEngine
from repo_code.governance import HoldoutVault, Lineage, DEFAULTS
from repo_code.knowledge import Knowledge
from repo_code.maintenance import Maintenance, restore_backup
from repo_code.mcp import MCPClient
from repo_code.models import Authority, Check, Contract, Requirement, Evidence, Assistant
from repo_code.observation import Observer
from repo_code.providers import ScriptedProvider
from repo_code.regression import CandidateRegression, validate_g4_report
from repo_code.storage import Store
from repo_code.util import Faults, RepoError, digest, uid, release_content_identity


def cand(app, changes=None, **kwargs):
    return app.releases.candidate('review', changes or {'retry_policy': {'kind': 'config', 'require_new_evidence': True}},
                                  'bounded targeted repair', [], **kwargs)


@pytest.mark.asyncio
async def test_cancel_between_patch_rows_compensates_before_terminal(portable, source):
    app = portable
    run = new_run(app, source, human_contract(), content=None)
    points, states = [], []
    def hook(point):
        if point == 'before_file_effect':
            points.append(point)
            if len(points) == 2:
                app.runtime.admit(run['id'], 'cancel-second-row', 'CANCEL')
                states.append(app.runtime.run(run['id'])['state'])
    app.broker.faults = Faults(hook)
    _, receipt = await tool(app, run, 'patch', {'operations': [
        {'kind': 'create', 'path': 'first.txt', 'content': 'a'},
        {'kind': 'create', 'path': 'second.txt', 'content': 'b'},
    ]})
    assert states == ['CONTROL_PENDING']
    assert receipt['status'] == 'FAILED'
    assert not app.runtime.workspace(run).path('first.txt').exists()
    assert not app.runtime.workspace(run).path('second.txt').exists()
    assert app.runtime.run(run['id'])['state'] == 'CANCELLED'


@pytest.mark.asyncio
async def test_cancel_after_effect_before_receipt_drains_operation(portable, source):
    app = portable
    run = new_run(app, source, human_contract(), content=None)
    states = []
    def hook(point):
        if point == 'after_file_effect':
            app.runtime.admit(run['id'], 'cancel-after-effect', 'CANCEL')
            states.append(app.runtime.run(run['id'])['state'])
    app.broker.faults = Faults(hook)
    _, receipt = await tool(app, run, 'create', {'path': 'done.txt', 'content': 'before-cancel'})
    assert states == ['CONTROL_PENDING']  # Already performed effect, still settling receipt.
    assert receipt['status'] == 'SUCCEEDED'
    assert app.runtime.workspace(run).path('done.txt').read_text() == 'before-cancel'
    assert app.runtime.run(run['id'])['state'] == 'CANCELLED'
    assert not app.runtime.unresolved(run['id'])


@pytest.mark.asyncio
async def test_cross_process_cancel_while_file_writer_waits(portable, source):
    app = portable
    run = new_run(app, source, human_contract(), content=None)
    reached, release = threading.Event(), threading.Event()
    def hook(point):
        if point == 'before_file_effect':
            reached.set()
            if not release.wait(15):
                raise RuntimeError('test cancellation barrier timed out')
    app.broker.faults = Faults(hook)
    job = asyncio.create_task(tool(app, run, 'create', {'path': 'never.txt', 'content': 'late'}))
    try:
        assert await asyncio.to_thread(reached.wait, 10)
        code = '''import json,sys
from repo_code.service import RepoCode
from repo_code.providers import ScriptedProvider
app=RepoCode(sys.argv[1],ScriptedProvider(),sandbox='local')
app.runtime.admit(sys.argv[2],'other-process-cancel','CANCEL')
print(json.dumps({'state':app.runtime.run(sys.argv[2])['state']}))
'''
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', code, str(app.store.root), run['id'],
                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 15)
        assert proc.returncode == 0, stderr.decode()
        assert json.loads(stdout)['state'] == 'CONTROL_PENDING'
    finally:
        release.set()
    _, receipt = await job
    assert receipt['status'] == 'FAILED'
    assert not app.runtime.workspace(run).path('never.txt').exists()
    assert app.runtime.run(run['id'])['state'] == 'CANCELLED'


@pytest.mark.asyncio
async def test_late_cancel_supersedes_pending_execution_steer(portable, source):
    app = portable
    run = new_run(app, source, human_contract(), content=None)
    def hook(point):
        if point == 'before_file_effect':
            app.runtime.admit(run['id'], 'steer-first', 'STEER', 'format concisely')
            app.runtime.finish_control('steer-first', 'execution_only')
            app.runtime.admit(run['id'], 'cancel-second', 'CANCEL')
    app.broker.faults = Faults(hook)
    _, receipt = await tool(app, run, 'create', {'path': 'late.txt', 'content': 'x'})
    assert receipt['status'] == 'FAILED'
    assert app.runtime.run(run['id'])['state'] == 'CANCELLED'
    with contextlib.closing(app.store.connect()) as conn:
        assert conn.execute("SELECT status FROM control WHERE id='steer-first'").fetchone()[0] == 'SUPERSEDED_BY_CANCEL'


@pytest.mark.asyncio
async def test_patch_limit_includes_history_and_move_destination(portable, source):
    app = portable
    run = new_run(app, source, human_contract(max_changed_files=2), content=None)
    _, first = await tool(app, run, 'create', {'path': 'old.txt', 'content': 'original'})
    assert first['status'] == 'SUCCEEDED'
    _, receipt = await tool(app, run, 'patch', {'operations': [
        {'kind': 'create', 'path': 'one.txt', 'content': '1'},
        {'kind': 'create', 'path': 'two.txt', 'content': '2'},
    ]})
    assert receipt['result']['code'] == 'FILE_COUNT_LIMIT'
    assert not app.runtime.workspace(run).path('one.txt').exists()
    # The explicit move counts both source and destination in the same union.
    from repo_code.workspace import Mutator
    one = human_contract(max_changed_files=1)
    with pytest.raises(RepoError, match='FILE_COUNT_LIMIT'):
        Mutator(app.runtime.workspace(run), run['id'], one).plan([
            {'kind': 'move', 'source': 'old.txt', 'destination': 'new.txt', 'expected_version': digest('original')}])


@pytest.mark.asyncio
async def test_successor_chain_preserves_origin_and_rejects_external_origin_edit(portable, source):
    app = portable
    c = human_contract()
    first = new_run(app, source, c)
    current = first
    for i in range(2):
        app.runtime.admit(current['id'], f'steer-{i}', 'STEER', 'new goal')
        app.runtime.finish_control(f'steer-{i}', 'contract_change')
        current = app.runtime.successor(current['id'], f'goal {i}', c)
    app.broker.authorize_run(current['id'], workspace_write=True)
    assert current['data']['origin'] == str(source.resolve())
    assert current['data']['origin_digest'] == first['data']['origin_digest']
    _, receipt = await tool(app, current, 'create', {'path': 'new.txt', 'content': 'candidate'})
    assert receipt['status'] == 'SUCCEEDED'
    intent = app.runtime.prepare(current['id'])
    app.store.put('human_acceptance', digest([current['id'], 'review', intent.source_digest]),
                  {'approved': True, 'contract': intent.contract_digest})
    assert app.runtime.commit(intent, await app.grader.evaluate(intent, c))['status'] == 'COMMITTED'
    (source / 'main.py').write_text('user edited source')
    with pytest.raises(RepoError):
        Delivery(app).integrate(current['id'], confirm=True)
    assert (source / 'main.py').read_text() == 'user edited source'
    assert not (source / 'new.txt').exists()


@pytest.mark.asyncio
async def test_signed_low_authority_global_check_is_not_accepted(app, source):
    c = freeze_contract(Contract(requirements=[Requirement(id='R', description='review')],
        checks=[Check(id='human', kind='human'), Check(id='static', kind='static_python')],
        required_checks=['human', 'static'], global_required_checks=['static'], coverage={'R':['human']}), 'operator')
    run = new_run(app, source, c)
    intent = app.runtime.prepare(run['id'])
    app.store.put('human_acceptance', digest([run['id'], 'human', intent.source_digest]), {'approved':True,'contract':intent.contract_digest})
    evidence = {}
    for check in c.checks:
        ev, _ = await app.grader._check(intent, check, None)
        evidence[check.id] = ev
    assert delivery_verified(intent, c, evidence, None, app.store)[0]
    weak = evidence['static'].model_copy(update={'authority': Authority.IN_PROCESS})
    weak = weak.model_copy(update={'signature':app.store.sign(weak.model_dump(mode='json',exclude={'signature'}))})
    evidence['static'] = weak
    assert not delivery_verified(intent, c, evidence, None, app.store)[0]


class BrokenSink(Sink):
    async def drain(self): raise BrokenPipeError('after write')
class MalformedReader:
    async def readline(self): return b'{not json}\n'
class DelayedReader:
    async def readline(self):
        await asyncio.sleep(60)
        return b''


@pytest.mark.asyncio
@pytest.mark.parametrize('sink,reader', [(BrokenSink, EOFReader), (Sink, MalformedReader), (Sink, DelayedReader)])
async def test_mcp_after_send_failure_variants_are_uncertain(portable, source, sink, reader):
    client = MCPClient(portable.sandbox, source, ['python','server.py'], 'test-run')
    transport = sink()
    client.process = SimpleNamespace(returncode=None, stdin=transport, stdout=reader())
    with pytest.raises(RepoError) as caught:
        await client._request('tools/call', {'name':'write','arguments':{}}, timeout=.03, operation_id='stable-op')
    assert caught.value.code == 'MCP_EFFECT_UNKNOWN'
    row = portable.store.values('mcp_request')[-1]
    assert row['state'] == 'UNCERTAIN' and row['operation_id'] == 'stable-op'
    assert len(transport.messages) == 1


@pytest.mark.asyncio
async def test_mcp_pre_send_failure_and_cancelled_send_are_distinct(portable, source):
    client = MCPClient(portable.sandbox, source, ['python'], 'run')
    client.process = SimpleNamespace(returncode=1, stdin=Sink(), stdout=EOFReader())
    with pytest.raises(RepoError, match='MCP_CLOSED'):
        await client._request('tools/call', {})
    assert portable.store.values('mcp_request') == []
    sink = Sink()
    client.process = SimpleNamespace(returncode=None, stdin=sink, stdout=DelayedReader())
    task = asyncio.create_task(client._request('tools/call', {}))
    while not sink.messages: await asyncio.sleep(.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert portable.store.values('mcp_request')[-1]['state'] == 'UNCERTAIN'


@pytest.mark.asyncio
async def test_actual_isolated_mcp_mutation_then_eof_is_uncertain(app, source):
    script = '''import sys,json,os
for line in sys.stdin:
    q=json.loads(line)
    m=q.get('method')
    if 'id' not in q:continue
    if m=='initialize':r={'protocolVersion':'2025-06-18','capabilities':{},'serverInfo':{'name':'eof','version':'1'}}
    elif m=='tools/list':r={'tools':[{'name':'write','inputSchema':{'type':'object'}}]}
    elif m=='tools/call':
        with open('/tmp/effect.txt','w') as f:
            f.write('effect happened');f.flush();os.fsync(f.fileno())
        os._exit(0)
    else:r={}
    print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':r}),flush=True)
'''
    (source/'server.py').write_text(script)
    run = new_run(app, source, human_contract())
    client = MCPClient(app.sandbox, app.runtime.workspace(run).root, ['python','server.py'], run['id'])
    await client.start()
    app.broker.mcp['eof'] = client
    try:
        request, receipt = await tool(app, run, 'mcp.call', {'server':'eof','tool':'write','arguments':{}})
        assert receipt['status'] == 'WAITING_APPROVAL'
        app.broker.approve(request.key)
        receipt = await app.broker.submit(request)
        assert receipt['status'] == 'UNCERTAIN'
        assert (client.root/'tmp'/'effect.txt').read_text() == 'effect happened'
        assert app.runtime.run(run['id'])['state'] == 'NEEDS_RECONCILIATION'
    finally:
        await client.close()
    # Process termination does not prove that the earlier call had no effects.
    assert app.runtime.unresolved(run['id'])


def test_hidden_claims_are_atomic_by_content(portable):
    a, b = cand(portable), cand(portable)
    vault = HoldoutVault(portable.store)
    key = vault.register('one','final',[{'id':'a','goal':'same'}])
    barrier = threading.Barrier(2)
    def claim(c):
        barrier.wait()
        try:
            vault.claim(c.id,key,'G4b')
            return 'accepted'
        except RepoError as exc:return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool: values = list(pool.map(claim,[a,b]))
    assert values.count('accepted') == 1, values
    assert len(portable.store.values('candidate')) == 2


def test_metadata_alias_and_renamed_task_cannot_reset_exposure(portable):
    app = portable
    a = cand(app); vault = HoldoutVault(app.store)
    key = vault.register('first','final',[{'id':'t1','goal':'same','files':{'x':'1'}}])
    vault.claim(a.id,key,'G4b');vault.notice(a.id,key,False,'REJECT')
    old = app.releases.get(a.candidate_release_digest)
    alias = old.model_copy(update={'generation':old.generation+10,'build_attestation_digest':app.store.json_blob({'new_name':True})})
    assert alias.digest != old.digest
    assert release_content_identity(alias) == a.content_identity
    app.store.put('release',alias.digest,alias,immutable=True)
    copied = a.model_copy(update={'id':'renamed-candidate','candidate_release_digest':alias.digest,'parents':[]})
    app.store.put('candidate',copied.id,copied,immutable=True)
    renamed = vault.register('different-cohort-name','final',[{'id':'t2','goal':'same','files':{'x':'1'}}])
    with pytest.raises(RepoError,match='HOLDOUT'):
        vault.claim(copied.id,renamed,'different-gate-label')


def test_l2_learning_lineage_inherits_l1_hidden_feedback(portable):
    a = cand(portable);vault=HoldoutVault(portable.store)
    hidden=vault.register('hidden','final',[{'id':'s','goal':'secret input'}])
    vault.claim(a.id,hidden,'G4b')
    notice=vault.notice(a.id,hidden,False,'REJECT')
    meta=portable.releases.candidate('meta',{'l1_optimizer':{'kind':'config','max_candidates':1}},'learn',
                                    [digest(notice)],level=2)
    with pytest.raises(RepoError,match='HOLDOUT'):
        vault.claim(meta.id,hidden,'META_FINAL')


def test_schema15_migration_preserves_hidden_use(portable):
    app=portable; a=cand(app);vault=HoldoutVault(app.store)
    key=vault.register('past','final',[{'id':'t','goal':'one'},{'id':'other','goal':'two'}])
    vault.claim(a.id,key,'G4b');vault.notice(a.id,key,False,'REJECT')
    # Construct the old schema's weaker PK and missing content reservations.
    with app.store.tx() as c:
        c.execute('DROP TABLE cohort_reservations')
        c.execute('ALTER TABLE exposures RENAME TO exposures_new')
        c.execute('CREATE TABLE exposures(artifact TEXT,cohort TEXT,family TEXT,class TEXT,PRIMARY KEY(artifact,cohort))')
        c.execute('INSERT OR IGNORE INTO exposures SELECT * FROM exposures_new WHERE artifact=?',(a.id,))
        c.execute('DROP TABLE exposures_new')
        c.execute('DELETE FROM lineage WHERE parent=?',(a.content_identity,))
        c.execute('PRAGMA user_version=15')
    migrated=Store(app.store.root)
    b=cand(app)
    renamed=HoldoutVault(migrated).register('renamed','final',[{'id':'new','goal':'two'}])
    with pytest.raises(RepoError,match='HOLDOUT'):
        HoldoutVault(migrated).claim(b.id,renamed,'G4b')
    with contextlib.closing(migrated.connect()) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0] == 16


def emit_retry(app, run_id, stderr='ImportError: missing', state='same', timeout=False, tool_name='process.run'):
    with app.store.tx() as c:
        app.store.emit(c,run_id,'tool.operation_finished',{'tool_name':tool_name,'action_digest':'action',
            'result':{'is_error':True,'stderr':stderr,'relevant_state':state,'timeout':timeout}})


def test_transient_errors_and_new_evidence_do_not_trigger_evolution(portable, source):
    app=portable;run=new_run(app,source,human_contract())
    emit_retry(app,run['id'])
    emit_retry(app,run['id'],'TimeoutError',timeout=True)
    emit_retry(app,run['id'])
    emit_retry(app,run['id'],tool_name='search')
    emit_retry(app,run['id'])
    emit_retry(app,run['id'],state='changed')
    assert Observer(app.store).consume()==[]
    assert app.store.values('problem')==[]


def test_unchanged_retries_across_tasks_have_recorded_triage_basis(portable, source):
    app=portable
    for _ in range(2):
        r=new_run(app,source,human_contract())
        emit_retry(app,r['id']);emit_retry(app,r['id'])
    observer=Observer(app.store);pattern=observer.consume()[0]
    report=observer.triage(pattern)
    assert report['decision']=='SCHEDULE_TARGETED_EVOLUTION'
    assert report['distinct_tasks']==2 and report['qualified_problems']==2
    assert 'CROSS_TASK_RECURRENCE' in report['reason_codes']
    assert all(len(p['evidence'])>=2 for p in app.store.values('problem'))
    p=app.store.get('pattern',pattern);p['contradictions']=['transient behavior observed'];app.store.put('pattern',pattern,p)
    assert observer.triage(pattern)['decision']=='WAIT_FOR_EVIDENCE'


@pytest.mark.asyncio
async def test_inconclusive_diagnosis_produces_no_candidates(portable, source):
    app=portable;r=new_run(app,source,human_contract())
    emit_retry(app,r['id']);emit_retry(app,r['id'])
    pattern=Observer(app.store).consume()[0]
    provider=ScriptedProvider([Assistant(text=json.dumps({'conclusion':'inconclusive','hypothesis':'need more evidence',
                           'targets':[],'evidence_ids':[],'negative_probes':[]}))])
    engine=EvolutionEngine(app,provider)
    diagnosis=await engine.diagnose(pattern)
    assert diagnosis['conclusion']=='inconclusive'
    assert await engine.propose(diagnosis)==[]
    assert len(app.store.values('candidate'))==0


@pytest.mark.asyncio
async def test_g4_executes_exact_bad_candidate_component_and_rejects(app):
    source="def handle(p):\n    return {'max_tokens':'NOT_AN_INTEGER'}\n"
    c=cand(app,{'context_policy':{'kind':'python','source':source}})
    report=await CandidateRegression(app).run(c)
    assert not report['passed']
    assert report['tested_components']==app.releases.get(c.candidate_release_digest).components
    assert next(x for x in report['cases'] if x['id']=='component_abi')['passed'] is False
    assert not validate_g4_report(report,c.candidate_release_digest,report['tested_components'],app.sandbox.environment_digest)
    audit=app.store.read_json(report['audit_digest'])
    assert all(r['release_digest']==c.candidate_release_digest for r in audit['runs'])


def test_publisher_rejects_old_minimal_g4_even_when_host_signed(app):
    c=cand(app)
    for g in ['G0','G1','G2','G3','G4','G4b']:
        app.releases.record_gate(c,g,{'passed':True,'legacy_probe_only':True})
    app.releases.approve(c.id,'test')
    with pytest.raises(RepoError,match='G4_INCOMPLETE_OR_WRONG_RELEASE'):
        app.releases.activate(c.id)


@pytest.mark.asyncio
async def test_growth_rejected_then_simplified_release_passes_g4(app):
    old=app.releases.active()['digest']
    with pytest.raises(RepoError) as caught:
        cand(app,{'planning_policy':{'kind':'config','instructions':'x'*100000}},operation='ADD',
             justification='Candidate proposes a much larger instruction block.')
    assert caught.value.code == 'COMPLEXITY_LIMIT'
    simple=cand(app,{'planning_policy':{'kind':'config','instructions':'Use evidence and verify.'}},operation='SIMPLIFY')
    state=app.store.get('candidate_state',simple.id)
    assert state['structural_audit']['candidate']['prompt_tokens'] < app.releases.governor.policy['max_prompt_tokens']
    report=await CandidateRegression(app).run(simple)
    assert report['passed'],report
    assert app.releases.active()['digest']==old  # G4 alone does NOT authorize deployment.


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['MERGE','REMOVE'])
async def test_merge_remove_candidates_execute_through_real_pipeline(app,operation):
    c=cand(app,{'planning_policy':{'kind':'config','instructions':'Merged implementation guidance'}},operation=operation)
    if operation=='REMOVE':
        assert app.releases.components(c.candidate_release_digest)['planning_policy']==DEFAULTS['planning_policy']
    report=await CandidateRegression(app).run(c)
    assert report['passed'],report
    assert not app.store.get('activated',c.candidate_release_digest)


def test_revocation_survives_archive_and_post_revoke_backup(portable,tmp_path):
    app=portable;k=Knowledge(app.store)
    item=k.draft('memory','test preference','do not add dependencies','user',['operator-note'])
    k.activate(item['id'],'operator');source,index=k.pin('project:test')
    k.archive(item['id'],'operator')
    # Archive changes default discovery, not a previously pinned view.
    assert k.search(source,index,'dependencies')
    k.revoke(item['id'])
    with pytest.raises(RepoError,match='KNOWLEDGE_REVOKED'):k.search(source,index,'dependencies')
    backup=tmp_path/'backup';Maintenance(app.store).backup(backup)
    restored=tmp_path/'restored';restore_backup(backup,restored)
    with pytest.raises(RepoError,match='KNOWLEDGE_REVOKED'):
        Knowledge(Store(restored)).search(source,index,'dependencies')

@pytest.mark.asyncio
async def test_meta_nested_search_invokes_frozen_python_l1_algorithms(app):
    from repo_code.meta import MetaEvaluator
    from repo_code.demo import optimizer_fixture
    from test_meta_nested import problem
    source = '''def handle(p):
    op=p['operation']
    if op=='search_config':return {'max_candidates':1,'max_rounds':1}
    if op=='diagnose':return {'diagnosis_instructions':'FROZEN_PYTHON_L1_DIAG'}
    if op=='propose':return {'proposal_instructions':'FROZEN_PYTHON_L1_PROPOSE'}
    return {}
'''
    candidate = app.releases.candidate('meta-python', {
        'l1_optimizer': {'kind':'python','source':source},
        'l2_optimizer': {'kind':'config','max_candidates':1,'prefer_evidence':True},
    }, 'Use frozen Python optimizer end to end', [], level=2)
    seen=[]
    def factory(public,purpose):
        inner=optimizer_fixture(public,purpose)
        async def handler(system,messages,tools,i):
            if purpose != 'coding':seen.append(system)
            return await inner.complete(system,messages,tools)
        return ScriptedProvider(handler=handler)
    ev=MetaEvaluator(app,HoldoutVault(app.store),factory)
    result=await ev.episode(problem('python-binding'),candidate.candidate_release_digest,'test',False,synthetic=True)
    assert result['status']=='SCORED',result
    assert any('FROZEN_PYTHON_L1_DIAG' in s for s in seen)
    assert any('FROZEN_PYTHON_L1_PROPOSE' in s for s in seen)
    actual=result['optimizer_invocations']
    assert {'search_config','diagnose','propose'} <= {r['operation'] for r in actual}
    expected=app.releases.get(candidate.candidate_release_digest).components['l1_optimizer']
    assert all(r['optimizer_release_digest']==candidate.candidate_release_digest and
               r['optimizer_component_digest']==expected for r in actual)
    assert result['coding_base_release_digest'] != candidate.candidate_release_digest


def test_g4_rejects_incomplete_or_other_release_report(portable):
    from repo_code.regression import G4_CASES,G4_PROTOCOL,suite_digest
    a=cand(portable)
    release=portable.releases.get(a.candidate_release_digest)
    report={'protocol':G4_PROTOCOL,'suite_digest':suite_digest(),'tested_release':release.digest,
            'tested_components':release.components,'environment_digest':portable.sandbox.environment_digest,
            'cases':[{'id':x,'passed':True} for x in G4_CASES],'skipped':[]}
    assert validate_g4_report(report,release.digest,release.components,portable.sandbox.environment_digest)
    assert not validate_g4_report({**report,'cases':report['cases'][:-1]},release.digest,release.components,portable.sandbox.environment_digest)
    b=cand(portable,{'retry_policy':{'kind':'config','require_new_evidence':False}})
    assert not validate_g4_report(report,b.candidate_release_digest,portable.releases.get(b.candidate_release_digest).components,portable.sandbox.environment_digest)
