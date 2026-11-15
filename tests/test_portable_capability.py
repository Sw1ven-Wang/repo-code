"""Offline native binding integration. No claim of real-model efficacy."""
import copy
import json
import pytest
from repo_code.capabilities import CapabilityRouter
from repo_code.capability_binding import bind, validate_template
from repo_code.util import RepoError, digest
from test_elp_experience import initial, learn, approved_fixture
from test_review_workflow import tool

SLOTS = {'implementation': {'kind': 'path', 'purpose': 'Locate the implementation that loses state at reconstruction.'}}
RULES = [{'tool': 'read', 'arguments_match': {'path': {'slot': 'implementation'}}}]


async def prepared(tmp_path, slots=None, rules=None, app=None):
    if app is None:
        app, run, owner = await initial(tmp_path)
    else:
        from repo_code.memory.repository import MemoryRepository
        from repo_code.memory.consolidation import Consolidator
        from repo_code.memory.models import MemoryProfile
        source=tmp_path/'source';source.mkdir();(source/'a.py').write_text('value=0\n')
        run=app.new_task('Preserve reconstruction state',source,authorize_write=True,authorize_process=True)
        owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
    rules=rules or RULES;slots=slots or SLOTS
    source, _, d = await learn(app, owner, run, change=lambda v: v['procedure'].update(steps=['Inspect and preserve reconstruction state']*len(rules)))
    current, facts = approved_fixture(app, owner, source, d, binding_slots=slots, step_rules=rules)
    app.broker.authorize_run(current['id'],True,True,False)
    router = CapabilityRouter(owner)
    route = router.route(current['id'], 'RETENTION_RULE', facts=facts)
    router.mark_offered(route)
    return app, current, router, route


def args(route, path):
    return {'contract_ref': route['contract_refs'][0], 'route_id': route['id'],
            'values': {'implementation': path}, 'evidence': {'implementation': [path]},
            'rationale': 'Observed current implementation contains the reconstruction boundary described by the capability.'}


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['engine/tree.py', 'lib/transforms/state.py'])
async def test_different_repositories_resolve_same_abstract_template(tmp_path, path):
    app, run, router, route = await prepared(tmp_path)
    try:
        assert 'a.py' not in json.dumps(route['guidance'])
        await tool(app, run, 'create', {'path': path, 'content': 'value=1\n'}, 'create-target')
        await tool(app, run, 'read', {'path': path}, 'locate')
        request = args(route, path)
        # Native broker call followed by actual tool operation, no model turn required.
        _, result = await tool(app, run, 'skill.bind', request, 'bind')
        assert result['status'] == 'SUCCEEDED', result
        _, before = router.receipt(run['id'], request['contract_ref'], route_id=route['id'])
        assert not before['applied'] and before['binding_ref']
        await tool(app, run, 'read', {'path': path}, 'actual-step')
        _, receipt = router.receipt(run['id'], request['contract_ref'], route_id=route['id'])
        assert receipt['applied'] and not receipt['verified']
        # Binding is persisted, signed and separate from immutable published template.
        assert app.store.get('capability_contract', request['contract_ref'])['step_rules'] == RULES
        reloaded = CapabilityRouter(router.owner)
        _, again = reloaded.receipt(run['id'], request['contract_ref'], route_id=route['id'])
        assert again['binding_ref'] == receipt['binding_ref'] and again['applied']
        assert not router.route(run['id'], 'RETENTION_RULE', facts={'source_changed': True})['guidance']
    finally:
        app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('implementation,check', [('engine.py','verify.py'),('lib/nodes.py','checks/check_state.py')])
@pytest.mark.parametrize('sandbox_mode',['local',pytest.param('docker',marks=pytest.mark.docker_backend)])
async def test_native_edit_and_real_command_across_layouts(tmp_path,implementation,check,sandbox_mode):
    slots={**SLOTS,'verification':{'kind':'argv','purpose':'Run current project tests for changed metadata, unchanged values and immutable inputs.'}}
    rules=[RULES[0],{'tool':'edit','arguments_match':{'path':{'slot':'implementation'}}},
           {'tool':'process.run','arguments_match':{'argv':{'slot':'verification'}}},
           {'tool':'workspace.diff','arguments_match':{'path':{'slot':'implementation'}}}]
    selected=None
    if sandbox_mode=='docker':
        import os
        from repo_code.service import RepoCode
        from repo_code.providers import ScriptedProvider
        image=os.getenv('REPO_CODE_DOCKER_IMAGE')
        if not image:pytest.skip('Explicit pinned Docker image required')
        selected=RepoCode(tmp_path/'state',ScriptedProvider(),'docker',image=image)
    app,run,router,route=await prepared(tmp_path,slots,rules,selected)
    try:
        if 'run' in app.sandbox.__dict__:del app.sandbox.run # Actual subprocess, no mocked result.
        original='def normalize(pair, precision):\n    return pair\n'
        check_code=("import runpy\nf=runpy.run_path("+repr(implementation)+")[\"normalize\"]\n"
                    "for value, old, target in [(1,10,20),(0,20,10),(-3,5,5)]:\n"
                    "    source=(value,old)\n    assert f(source,target)==(value,target)\n    assert source==(value,old)\n"
                    "assert f((1,10),20)!=f((2,10),20)\nprint('positive negative boundary passed')\n")
        for path,content in [(implementation,original),(check,check_code)]:
            await tool(app,run,'create',{'path':path,'content':content},'create-'+path)
            await tool(app,run,'read',{'path':path},'read-'+path)
        command=['python',check]
        _,failed=await tool(app,run,'process.run',{'argv':command},'reproduce')
        assert failed['result']['exit_code']!=0,failed
        request=args(route,implementation)
        request['values']['verification']=command
        request['evidence']['verification']=[check]
        _,bound=await tool(app,run,'skill.bind',request,'bind')
        assert bound['status']=='SUCCEEDED',bound
        await tool(app,run,'read',{'path':implementation},'execute-read')
        await tool(app,run,'edit',{'path':implementation,'old_text':'return pair','new_text':'return (pair[0], precision)'},'repair')
        _,passed=await tool(app,run,'process.run',{'argv':command},'validate')
        assert passed['result']['exit_code']==0,passed
        await tool(app,run,'workspace.diff',{'path':implementation},'inspect')
        _,receipt=router.receipt(run['id'],route['contract_refs'][0],route_id=route['id'])
        assert receipt['applied'] and len(receipt['step_evidence'])==4
        assert not receipt['verified'] # Local command success cannot manufacture trusted acceptance.
        assert app.store.get('capability_contract',route['contract_refs'][0])['step_rules']==rules
    finally:app.close()


@pytest.mark.asyncio
async def test_missing_read_stale_read_and_path_escape_rejected(tmp_path):
    app, run, router, route = await prepared(tmp_path)
    try:
        request = args(route, 'a.py')
        with pytest.raises(RepoError) as exc:
            bind(router, run['id'], **request)
        assert exc.value.code=='CAPABILITY_BINDING_READ_REQUIRED'
        await tool(app, run, 'read', {'path': 'a.py'}, 'read')
        ws = app.runtime.workspace(app.runtime.run(run['id']))
        ws.path('a.py').write_text('changed without a new observation')
        with pytest.raises(RepoError) as exc:
            bind(router, run['id'], **request)
        assert exc.value.code=='CAPABILITY_BINDING_READ_REQUIRED'
        with pytest.raises(RepoError) as exc:
            bind(router, run['id'], **args(route, '../outside.py'))
        assert exc.value.code=='PATH_DENIED'
        assert not app.store.values('capability_binding')
    finally:
        app.close()


@pytest.mark.asyncio
async def test_tamper_cross_run_control_and_no_retroactive_binding(tmp_path):
    app, run, router, route = await prepared(tmp_path)
    try:
        await tool(app, run, 'read', {'path': 'a.py'}, 'read')
        request = args(route, 'a.py')
        bound = bind(router, run['id'], **request)
        assert bind(router, run['id'], **request)['binding_ref'] == bound['binding_ref']
        _, receipt = router.receipt(run['id'], request['contract_ref'], route_id=route['id'])
        assert not receipt['applied'] # Read before binding is not retroactive proof.
        changed = {**request, 'rationale': 'A changed rationale cannot silently rewrite an admitted application binding.'}
        with pytest.raises(RepoError, match='CAPABILITY_BINDING_IMMUTABLE'):
            bind(router, run['id'], **changed)
        other = app.new_task('Unrelated task', app.store.root.parent/'source')
        with pytest.raises(RepoError, match='CAPABILITY_BINDING_OFFER_REQUIRED'):
            bind(router, other['id'], **request)
        await tool(app, run, 'read', {'path': 'a.py'}, 'execute')
        row = app.store.get('capability_binding', bound['binding_ref'])
        row['payload']['step_rules'][0]['arguments_match']['path'] = 'wrong.py'
        app.store.put('capability_binding', bound['binding_ref'], row)
        _, receipt = router.receipt(run['id'], request['contract_ref'], route_id=route['id'])
        assert not receipt['applied']
    finally:
        app.close()


@pytest.mark.parametrize('slots,rules', [
    (SLOTS, [{'tool': 'read', 'arguments_match': {'path': 'fixed.py'}}]),
    (SLOTS, [{'tool': 'process.run', 'arguments_match': {'argv': {'slot': 'implementation'}}}]),
    (SLOTS, [{'tool': 'read', 'arguments_match': {'path': {'slot': 'unknown'}}}]),
    ({**SLOTS, 'unused': SLOTS['implementation']}, RULES),
])
def test_template_rejects_literals_wrong_types_and_unused_slots(slots, rules):
    with pytest.raises(RepoError):
        validate_template(slots, rules, ['Inspect boundary'])


@pytest.mark.asyncio
async def test_portable_promotion_materializes_abstract_contract(tmp_path):
    from test_procedure_promotion import fixture, model, Runner
    app, draft, task, probes, plan, review = await fixture(tmp_path)
    try:
        plan.update(binding_slots=SLOTS, step_rules=RULES)
        result = await app.procedure_promotion(Runner()).run(draft['id'], model(plan), model(review),
                         support_tasks={task['id']: task}, probes=probes)
        assert result['reason'] == 'INDEPENDENT_TRANSFER_SUITE_REQUIRED', result
        contract = app.store.get('capability_contract', result['contract_ref'])
        assert contract['binding_slots'] == SLOTS and contract['step_rules'] == RULES
        assert 'main.py' not in json.dumps(contract)
        assert not result['published']
        from repo_code.memory.repository import MemoryRepository
        from repo_code.memory.consolidation import Consolidator
        from repo_code.memory.models import MemoryProfile
        owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
        altered=copy.deepcopy(SLOTS);altered['implementation']['purpose']='A different purpose was not approved by the independent reviewer.'
        bad_ref,_=CapabilityRouter(owner).contract(contract['skill_id'],contract['revision'],
                       step_rules=RULES,probe_ids=contract['required_probe_ids'],binding_slots=altered)
        rejected=app.procedure_promotion(Runner()).validator.s1(bad_ref,result['candidate'],{task['id']:task})
        assert rejected['reasons']==['SKILL_PORTABLE_REVIEW_BINDING']
    finally:
        app.close()


@pytest.mark.asyncio
async def test_portable_prose_cannot_retain_support_filename(tmp_path):
    from test_procedure_promotion import fixture, model, Runner
    app, draft, task, probes, plan, review = await fixture(tmp_path)
    try:
        plan.update(binding_slots=SLOTS, step_rules=RULES)
        plan['procedure']['steps'] = ['Inspect main.py reconstruction']
        result = await app.procedure_promotion(Runner()).run(draft['id'], model(plan), model(review),
                         support_tasks={task['id']: task}, probes=probes)
        assert result['reason'] == 'PROMOTION_TASK_LITERAL_IN_CAPABILITY', result
        assert not app.store.values('promotion_materialization')
    finally:
        app.close()


@pytest.mark.asyncio
async def test_binding_atomic_recovery_restart_and_control_invalidation(tmp_path,monkeypatch):
    from repo_code.service import RepoCode
    from repo_code.providers import ScriptedProvider
    from repo_code.memory.repository import MemoryRepository
    from repo_code.memory.consolidation import Consolidator
    from repo_code.memory.models import MemoryProfile
    app,run,router,route=await prepared(tmp_path)
    root=app.store.root
    try:
        await tool(app,run,'read',{'path':'a.py'},'observe')
        request=args(route,'a.py');original=app.store.put
        def crash(kind,*a,**kw):
            result=original(kind,*a,**kw)
            if kind=='capability_binding':raise RuntimeError('after insert before commit')
            return result
        monkeypatch.setattr(app.store,'put',crash)
        with pytest.raises(RuntimeError):bind(router,run['id'],**request)
        assert not app.store.values('capability_binding')
        assert not any(e['type']=='capability.bound' for e in app.store.events(run['id'],trusted=True))
        monkeypatch.setattr(app.store,'put',original)
        bound=bind(router,run['id'],**request)
        await tool(app,run,'read',{'path':'a.py'},'action')
        app.close()
        app=RepoCode(root,ScriptedProvider(),'local')
        owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
        router=CapabilityRouter(owner)
        _,receipt=router.receipt(run['id'],request['contract_ref'],route_id=route['id'])
        assert receipt['applied'] and receipt['binding_ref']==bound['binding_ref']
        with app.store.tx() as c:app.runtime.update(c,app.runtime.run(run['id'],c),control=True)
        _,receipt=router.receipt(run['id'],request['contract_ref'],route_id=route['id'])
        assert not receipt['applied']
        with pytest.raises(RepoError):bind(router,run['id'],**request)
    finally:app.close()


@pytest.mark.asyncio
async def test_portable_transfer_requires_new_layout_before_model_calls(tmp_path):
    from test_procedure_promotion import fixture,model,Runner
    app,draft,task,probes,plan,review=await fixture(tmp_path)
    try:
        plan.update(binding_slots=SLOTS,step_rules=RULES)
        runner=Runner();flow=app.procedure_promotion(runner)
        result=await flow.run(draft['id'],model(plan),model(review),support_tasks={task['id']:task},probes=probes)
        tasks=[]
        for i,role in enumerate(['CORE','CORE','CORE','TRANSFER','NEGATIVE']):
            t=copy.deepcopy(task);t.update(id='independent-'+str(i),skill_role=role)
            t['files']['main.py']='# distinct implementation '+str(i)+'\ndef answer(x): return x\n'
            tasks.append(t)
        count=runner.calls
        with pytest.raises(RepoError,match='SKILL_PORTABLE_TRANSFER_LAYOUT_REQUIRED'):
            await flow.validator.s3(result['contract_ref'],result['candidate'],tasks)
        assert runner.calls==count
    finally:app.close()


@pytest.mark.asyncio
async def test_missing_slot_and_unread_command_evidence_fail_closed(tmp_path):
    slots={**SLOTS,'verification':{'kind':'argv','purpose':'Locate the current regression command from observed test configuration.'}}
    rules=RULES+[{'tool':'process.run','arguments_match':{'argv':{'slot':'verification'}}}]
    app,run,router,route=await prepared(tmp_path,slots,rules)
    try:
        await tool(app,run,'read',{'path':'a.py'},'observe')
        request=args(route,'a.py')
        with pytest.raises(RepoError,match='CAPABILITY_BINDING_SLOT_COVERAGE'):bind(router,run['id'],**request)
        request['values']['verification']=['python','-m','pytest'];request['evidence']['verification']=[]
        with pytest.raises(RepoError,match='CAPABILITY_BINDING_EVIDENCE_REQUIRED'):bind(router,run['id'],**request)
        assert not app.store.values('capability_binding')
    finally:app.close()
