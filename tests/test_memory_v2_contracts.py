"""E0 deterministic contracts. Local model quality is measured separately in E1."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from repo_code.models import Assistant, ToolCall
from repo_code.providers import ScriptedProvider, OpenAIProvider
from repo_code.service import RepoCode
from repo_code.util import RepoError, digest
from repo_code.memory.models import MemoryProfile, ClaimRecord, EvidenceRef, CompactionProposal
from repo_code.memory.repository import MemoryRepository
from repo_code.memory.working_state import semantic_message
from repo_code.memory.retrieval import build_bundle, applicability
from repo_code.maintenance import Maintenance, restore_backup


def extraction(system, messages, tools, count):
    payload=json.loads(messages[0]['content']); records=payload['new_closed_groups']
    changes=[]
    for record in records:
        text=record['message'].get('content','')
        if not text or record['message']['role']!='user':continue
        changes.append({'action':'ADD','claim_id':record['ref']['source_id'],'kind':'DECISION','text':text[:160],
                        'source_ids':[record['ref']['source_id']],'quote':text[:160],'epistemic_status':'USER_STATED'})
    return Assistant(text=json.dumps({'changes':changes,'retained_open_loop_ids':[x['id'] for x in payload['mandatory_open_loops']]}))


@pytest.fixture
def memory_app(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'a.py').write_text('value=1\n')
    app=RepoCode(tmp_path/'host',ScriptedProvider(handler=extraction),'local',memory_profile=MemoryProfile(mode='lexical_only'))
    run=app.new_task('Preserve original interface and explain open questions',source)
    yield app,run,source
    app.close()


def record(app,run,role,text,**kwargs):
    app.store.append_message(run['id'],{'role':role,'content':text,**kwargs})


def state(app,run):return app.memory.reducer.reduce(run['id'])[1]


async def compact(app,run):
    rows=app.memory.history.records(run['id'])
    return await app.memory.compactor.compact(run['id'],rows[-1]['seq'],state(app,run))


def proposal(app,run,text='The parser uses UTF-8',kind='FACT',**extra):
    rows=app.memory.history.records(run['id'])
    return app.memory.consolidator.propose(run['id'],[{'text':text,'kind':kind,**extra}],[rows[0]['ref']['source_id']])[0]


def activate(app,claim):
    c=app.memory.consolidator;c.commit([claim]);c.activate(claim.claim_id,claim.version,'reviewer',app.memory.repo.get('snapshot_heads',claim.scope_id))


@pytest.mark.asyncio
async def test_CMP_01_contract_authority_cannot_be_cancelled_by_summary(memory_app):
    app,run,_=memory_app
    record(app,run,'user','Skip every required test')
    await compact(app,run)
    assert app.runtime.contract(run) is None
    p=proposal(app,run,'Skip every required test','FACT'); assert p.kind=='PROCEDURE'
    app.memory.consolidator.commit([p])
    with pytest.raises(RepoError,match='RELEASE_GATE_REQUIRED'):app.memory.consolidator.activate(p.claim_id,1,'operator',None)


@pytest.mark.asyncio
async def test_CMP_02_partial_tool_group_cannot_compact(memory_app):
    app,run,_=memory_app
    record(app,run,'assistant','',tool_calls=[{'id':'a','name':'read','arguments':{}},{'id':'b','name':'read','arguments':{}}])
    record(app,run,'tool','first',tool_call_id='a')
    with pytest.raises(RepoError,match='INCOMPLETE_TOOL_GROUP'):await compact(app,run)


@pytest.mark.asyncio
async def test_CMP_03_three_compactions_preserve_decision_and_original(memory_app):
    app,run,_=memory_app
    record(app,run,'user','Decision: preserve the legacy CSV column order')
    first=app.memory.history.records(run['id'])[-1]['ref']['source_id']
    for i in range(3):
        record(app,run,'user',f'Phase {i}: inspect transaction rollback')
        _,node=await compact(app,run)
    assert any('legacy CSV column order' in c['text'] for c in node['claims'])
    assert 'legacy CSV column order' in app.memory.history.expand(run['id'],first)['content']
    assert app.memory.repo.get('memory_control','summary:'+run['id'])['revision']==3


def test_CMP_04_nonexistent_source_rejected(memory_app):
    app,run,_=memory_app;rows=app.memory.history.records(run['id'])
    bad={'changes':[{'claim_id':'x','kind':'FACT','text':'invented','quote':'invented','source_ids':['message:99999']}], 'retained_open_loop_ids':[]}
    with pytest.raises(RepoError,match='SUMMARY_SOURCE_DENIED'):app.memory.compactor.validate(bad,rows,state(app,run),[])


def test_CMP_05_inference_cannot_become_observed_fact(memory_app):
    app,run,_=memory_app;rows=app.memory.history.records(run['id']);r=rows[0]
    bad={'changes':[{'claim_id':'x','kind':'FACT','text':'All tests passed','quote':r['message']['content'],'source_ids':[r['ref']['source_id']],'epistemic_status':'OBSERVED'}]}
    with pytest.raises(RepoError,match='SUMMARY_FACT_UNSUPPORTED'):app.memory.compactor.validate(bad,rows,state(app,run),[])


@pytest.mark.asyncio
async def test_CMP_06_control_change_during_compaction_is_fenced(memory_app):
    app,run,_=memory_app
    def change(*args):
        with app.store.tx() as c:app.runtime.update(c,app.runtime.run(run['id'],c),control=True)
        return extraction(*args)
    app.memory.compactor.provider=ScriptedProvider(handler=change)
    with pytest.raises(RepoError,match='SUMMARY_STALE_FENCE'):await compact(app,run)
    assert app.memory.repo.get('memory_control','summary:'+run['id']) is None


@pytest.mark.asyncio
async def test_CMP_07_unrelated_tail_is_preserved(memory_app):
    app,run,_=memory_app
    def append(*args):record(app,run,'user','New tail must remain visible');return extraction(*args)
    app.memory.compactor.provider=ScriptedProvider(handler=append)
    _,node=await compact(app,run)
    assert app.memory.history.records(run['id'])[-1]['seq']>node['source_end_seq']
    assert app.store.messages(run['id'])[-1]['content']=='New tail must remain visible'


@pytest.mark.asyncio
async def test_CMP_08_bad_json_does_not_replace_legal_summary(memory_app):
    app,run,_=memory_app;await compact(app,run)
    head=app.memory.repo.get('memory_control','summary:'+run['id'])
    record(app,run,'user','Later observation')
    app.memory.compactor.provider=ScriptedProvider([Assistant(text='not JSON')])
    with pytest.raises(ValueError):await compact(app,run)
    assert app.memory.repo.get('memory_control','summary:'+run['id'])==head


@pytest.mark.asyncio
async def test_CMP_09_huge_tool_output_is_archived_as_complete_group(memory_app):
    app,run,_=memory_app
    record(app,run,'assistant','',tool_calls=[{'id':'a','name':'read','arguments':{}}])
    record(app,run,'tool','large output '*15000,tool_call_id='a')
    _,messages,_=await app.memory.prepare_context(run['id'],[])
    tool=next(x for x in messages if x['role']=='tool');value=json.loads(tool['content'])
    assert len(app.store.read_blob(value['artifact']))>100000
    assert app.store.get('run_artifact',digest([run['id'],value['artifact']]))


def test_CMP_10_old_check_is_historical_after_file_changes(memory_app):
    app,run,_=memory_app
    from repo_code.models import OperationRequest
    req=OperationRequest(run_id=run['id'],turn_id='t',tool_call_uid='x',tool_name='process.run',tool_spec_digest='x',arguments={})
    baseline=app.runtime.workspace(run).snapshot()
    with app.store.tx() as c:
        c.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)',('op','key',run['id'],'SUCCEEDED','s',req.model_dump_json(),json.dumps({'exit_code':0,'relevant_state':baseline}),None,0))
    (Path(run['data']['workspace'])/'a.py').write_text('value=2\n')
    obs=state(app,run)['host_observations'][0]
    assert obs['validity']=='HISTORICAL' and obs['needs_reverification']


class RecordingEncoder:
    """E0 instrumentation only, not a substitute for E1 genuine model scores."""
    identity='e0-recording-encoder';seconds=0
    def __init__(self):self.inputs=[]
    def embed(self,texts,query=False):self.inputs+=texts;return [[1.,0.] for _ in texts]
    def rerank(self,query,texts):self.inputs+=texts;return [1. for _ in texts]
    def close(self):pass


@pytest.mark.asyncio
async def test_RET_01_dense_and_rerank_are_executed_in_strict_path(tmp_path):
    source=tmp_path/'source';source.mkdir();encoder=RecordingEncoder()
    app=RepoCode(tmp_path/'host',ScriptedProvider(),'local',memory_profile=MemoryProfile(),memory_encoder=encoder)
    k=app.runtime.knowledge;p=k.draft('memory','Connection reset','连接被对端重置',__import__('repo_code.knowledge',fromlist=['project_scope']).project_scope(source),['manual']);k.activate(p['id'],'operator')
    run=app.new_task('连接中断',source)
    hits,trace=await app.memory.search(run['id'],'连接中断')
    assert encoder.inputs.count('连接中断')>=1 and hits
    assert 'dense' in app.memory.repo.get('retrieval_traces',trace)['channels']


@pytest.mark.asyncio
async def test_RET_02_exact_error_identifier_survives(memory_app):
    app,run,source=memory_app;claim=proposal(app,run,'CSV_EMPTY_HEADER identifies malformed input');activate(app,claim)
    later=app.new_task('debug parser',source)
    hits,_=await app.memory.search(later['id'],'CSV_EMPTY_HEADER')
    assert hits[0]['id']==claim.claim_id


def test_RET_03_framework_mismatch_excluded(memory_app):
    app,run,_=memory_app;key,s=app.memory.reducer.reduce(run['id']);intent=app.memory.reducer.intent(run['id'],key,s)
    intent=intent.model_copy(update={'environment':{'django':'5'}})
    assert applicability({'preconditions':{'django':'4'}},intent,{})==('FALSE','DEPENDENCY_MISMATCH')


def test_RET_04_unknown_precondition_requires_evidence(memory_app):
    app,run,_=memory_app;key,s=app.memory.reducer.reduce(run['id']);intent=app.memory.reducer.intent(run['id'],key,s)
    assert applicability({'preconditions':{'django':'4'}},intent,{})[0]=='UNKNOWN'


def test_RET_05_plan_switch_changes_query_with_same_goal(memory_app):
    app,run,_=memory_app;key,s=app.memory.reducer.reduce(run['id']);old=app.memory.reducer.intent(run['id'],key,s)
    app.store.put('plan',run['id'],{'steps':['Investigate async cancellation rollback']})
    key,s=app.memory.reducer.reduce(run['id']);new=app.memory.reducer.intent(run['id'],key,s)
    assert old.semantic_query!=new.semantic_query and 'cancellation' in new.semantic_query


def test_RET_06_history_read_cannot_authorize_current_edit(memory_app):
    app,run,_=memory_app
    app.memory.history.expand(run['id'],app.memory.history.records(run['id'])[0]['ref']['source_id'])
    with pytest.raises(RepoError) as caught:app.broker.register_call(run['id'],'t',ToolCall(id='edit',name='edit',arguments={'path':'a.py','old_text':'1','new_text':'2'}))
    assert caught.value.code=='READ_REQUIRED'


def test_RET_07_scope_filter_precedes_encoder_exposure(memory_app):
    app,run,_=memory_app;k=app.runtime.knowledge
    p=k.draft('memory','PRIVATE_SENTINEL','not authorized','different-project',['manual']);k.activate(p['id'],'operator')
    encoder=RecordingEncoder();profile=MemoryProfile()
    snap,index=build_bundle(app.memory.repo,run['data']['manifest']['scope'],profile,encoder)
    assert not app.store.read_json(snap)['items'] and not encoder.inputs


@pytest.mark.asyncio
async def test_RET_08_revocation_after_cache_before_admission_blocks(memory_app):
    app,run,_=memory_app
    _,_,manifest=await app.memory.prepare_context(run['id'],[])
    app.memory.repo.revoke(run['id'])
    with pytest.raises(RepoError):app.memory.admission(manifest)


def test_RET_09_missing_real_model_does_not_silently_degrade(memory_app):
    app,run,_=memory_app
    with pytest.raises(RepoError,match='SEMANTIC_MODELS_UNAVAILABLE'):build_bundle(app.memory.repo,run['data']['manifest']['scope'],MemoryProfile(),None)


@pytest.mark.asyncio
async def test_RET_10_unmatched_query_returns_empty(memory_app):
    app,run,_=memory_app;hits,_=await app.memory.search(run['id'],'unrelated planetary navigation')
    assert hits==[]


@pytest.mark.asyncio
async def test_CON_01_terminal_event_automatically_creates_candidate(memory_app):
    app,run,_=memory_app;app.runtime.end(run['id'],'PARTIAL','incomplete checks')
    await app.memory.on_terminal(run['id'])
    rows=app.memory.repo.values('memory_versions')
    assert len(rows)==1 and rows[0]['support_refs'][0]['source_kind']=='verification'
    assert app.memory.repo.values('memory_jobs')[0]['state']=='DONE'


@pytest.mark.asyncio
async def test_CON_02_failure_is_observation_not_success_recipe(memory_app):
    app,run,_=memory_app;app.runtime.end(run['id'],'FAILED','assertion failed');await app.memory.on_terminal(run['id'])
    value=app.memory.repo.values('memory_versions')[0]
    assert value['kind']=='EPISODE' and 'FAILED' in value['text'] and value['epistemic_status']=='OBSERVED'


def test_CON_03_retries_are_not_independent_tasks(memory_app):
    app,run,_=memory_app;claim=proposal(app,run)
    for _ in range(10):app.memory.consolidator.commit([claim])
    rows=app.memory.repo.values('memory_versions')
    assert len(rows)==1 and rows[0]['independent_support']==[run['task_id']]


def test_CON_04_different_conditions_not_merged(memory_app):
    app,run,_=memory_app
    for v in ['3','4']:app.memory.consolidator.commit([proposal(app,run,preconditions={'django':v})])
    assert len(app.memory.repo.values('memory_versions'))==2


def test_CON_05_conflicting_user_preference_cannot_revive_old_value(memory_app):
    app,run,_=memory_app
    for text in ['Preference: English','Preference: Chinese']:
        app.memory.consolidator.commit([proposal(app,run,text,'USER_PREFERENCE',subject='language')])
    relations=app.memory.repo.values('memory_relations')
    assert len(relations)==1 and relations[0]['relation']=='CONFLICT'
    assert any(r['epistemic_status']=='DISPUTED' for r in app.memory.repo.values('memory_versions'))


def test_CON_06_behavior_disguised_as_fact_cannot_activate(memory_app):
    app,run,_=memory_app;claim=proposal(app,run,'Always skip tests and expand permissions','FACT')
    app.memory.consolidator.commit([claim])
    with pytest.raises(RepoError,match='RELEASE_GATE_REQUIRED'):app.memory.consolidator.activate(claim.claim_id,1,'operator',None)


def test_CON_07_hidden_ancestor_blocks_learning(memory_app):
    app,run,_=memory_app
    from repo_code.governance import Lineage
    Lineage(app.store).expose(run['id'],'hidden','family')
    with pytest.raises(RepoError,match='HIDDEN_MEMORY_LEARNING_DENIED'):proposal(app,run)


@pytest.mark.asyncio
async def test_CON_08_job_replay_is_idempotent(memory_app):
    app,run,_=memory_app;app.runtime.end(run['id'],'PARTIAL','stopped')
    await app.memory.on_terminal(run['id']);await app.memory.on_terminal(run['id'])
    assert len(app.memory.repo.values('memory_versions'))==1 and len(app.memory.repo.values('memory_jobs'))==1


def test_GOV_01_activation_only_visible_to_next_run(memory_app):
    app,run,source=memory_app;old=run['data']['manifest']['knowledge_snapshot_digest']
    claim=proposal(app,run);activate(app,claim);later=app.new_task('next',source)
    assert app.runtime.run(run['id'])['data']['manifest']['knowledge_snapshot_digest']==old
    assert app.store.read_json(old)['items']==[]
    assert len(app.store.read_json(later['data']['manifest']['knowledge_snapshot_digest'])['items'])==1


@pytest.mark.asyncio
async def test_GOV_02_revocation_blocks_derived_summary(memory_app):
    app,run,_=memory_app;key,node=await compact(app,run)
    app.memory.repo.revoke(node['raw_source_refs'][0]['source_id'])
    with pytest.raises(RepoError,match='MEMORY_SOURCE_REVOKED'):app.memory.repo.check([key])
    assert app.memory.repo.values('revocation_derivatives')[0]['physical_erasure'] is False


def test_GOV_03_restore_requires_current_revocation_ledger(memory_app,tmp_path):
    app,run,_=memory_app;app.runtime.end(run['id'],'PARTIAL','backup')
    backup=tmp_path/'backup';Maintenance(app.store).backup(backup)
    restored=tmp_path/'restored';restore_backup(backup,restored)
    from repo_code.storage import Store
    repo=MemoryRepository(Store(restored))
    with pytest.raises(RepoError,match='REVOCATION_SYNC_REQUIRED'):repo.check([])


def test_GOV_04_model_cannot_supply_wider_scope(memory_app):
    app,run,_=memory_app
    with pytest.raises(Exception):app.broker.register_call(run['id'],'t',ToolCall(id='scope',name='memory.search',arguments={'query':'private','scope':'all'}))


def test_PRV_01_native_envelope_preserves_thinking_and_tool_alias():
    from types import SimpleNamespace
    p=OpenAIProvider('deepseek-flash',client=SimpleNamespace(),thinking='enabled')
    request,_=p.build_request('system',[{'role':'assistant','reasoning_content':'opaque','tool_calls':[{'id':'c','name':'process.run','arguments':{}}]},
                                      {'role':'tool','tool_call_id':'c','content':'result'}],[])
    assert request['messages'][1]['reasoning_content']=='opaque'
    assert request['messages'][1]['tool_calls'][0]['function']['name']=='process_run'


def test_PRV_02_opaque_state_excluded_from_learning():
    message={'role':'assistant','content':'visible','reasoning_content':'PRIVATE_REASONING','native_blocks':[{'type':'thinking','thinking':'PRIVATE_REASONING'}]}
    assert 'PRIVATE_REASONING' not in json.dumps(semantic_message(message))


@pytest.mark.asyncio
async def test_PRV_03_unverified_rebase_blocks_before_semantic_call(memory_app):
    app,run,_=memory_app
    for i in range(30):record(app,run,'assistant','long narrative '*500,reasoning_content='opaque')
    with pytest.raises(RepoError,match='PROTOCOL_REBASE_UNVERIFIED'):await app.memory.prepare_context(run['id'],[])
    assert app.provider.calls==0


def test_BUD_01_concurrent_reservations_cannot_overspend(memory_app):
    app,run,_=memory_app
    def reserve(_):
        try:return app.memory.repo.reserve(run['id'],'compaction',1000,1000,1,2500)
        except RepoError:return None
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(reserve,range(4)))
    assert sum(x is not None for x in results)==1
    app.memory.repo.settle(next(x for x in results if x),{},0)
    assert app.memory.repo.values('memory_budget')[0]['state']=='USAGE_UNKNOWN'


@pytest.mark.asyncio
async def test_parallel_tool_results_share_one_group_budget(memory_app):
    app,run,_=memory_app
    calls=[{'id':str(i),'name':'read','arguments':{'path':f'{i}.py'}} for i in range(8)]
    record(app,run,'assistant','',tool_calls=calls)
    for i in range(8):record(app,run,'tool','line of source '*700,tool_call_id=str(i))
    _,messages,manifest=await app.memory.prepare_context(run['id'],[],policy={'max_tokens':8000,'reserve_tokens':4000})
    assert len([m for m in messages if m['role']=='tool'])==8
    assert manifest['estimated_input_tokens']+4000+app.memory.profile.safety_tokens<=8000
    assert len(app.store.messages(run['id'])[-1]['content'])>5000


@pytest.mark.asyncio
async def test_retrieval_cache_respects_requested_token_budget(memory_app):
    app,run,source=memory_app
    claim=proposal(app,run,'CSV_EMPTY_HEADER '+('important diagnostic detail '*100));activate(app,claim)
    later=app.new_task('debug',source)
    hits,_=await app.memory.search(later['id'],'CSV_EMPTY_HEADER',token_budget=3000)
    assert hits
    hits,_=await app.memory.search(later['id'],'CSV_EMPTY_HEADER',token_budget=100)
    assert hits==[]


@pytest.mark.asyncio
async def test_embedded_tool_json_quote_has_exact_field_selector(memory_app):
    app,run,_=memory_app
    record(app,run,'assistant','',tool_calls=[{'id':'read','name':'read','arguments':{'path':'a.py'}}])
    record(app,run,'tool',json.dumps({'content':'1: value=1\n2: result=2'}),tool_call_id='read')
    rows=app.memory.history.records(run['id']);ref=rows[-1]['ref']['source_id']
    value={'changes':[{'claim_id':'source-line','kind':'FACT','text':'1: value=1','quote':'1: value=1','source_ids':[ref],'epistemic_status':'OBSERVED'}]}
    claims=app.memory.compactor.validate(value,rows,state(app,run),[])
    assert claims[0]['support_refs'][0]['selector']=={'type':'embedded_json_pointer','container':'/content','value':'/content'}


def test_old_consolidation_job_cannot_commit_after_user_correction(memory_app):
    app,run,_=memory_app;memory=app.memory;claim=proposal(app,run)
    job=memory.jobs.enqueue(claim.scope_id,run['id'],digest(memory.history.records(run['id'])),memory.profile_digest,'test')
    job=memory.jobs.claim(job['id'])
    record(app,run,'user','Correction: earlier preference was wrong')
    with pytest.raises(RepoError,match='CONSOLIDATION_SOURCE_CHANGED'):
        memory.consolidator.commit([claim],job,memory.jobs,memory.repo.get('snapshot_heads',claim.scope_id))
    assert memory.repo.values('memory_versions')==[]


@pytest.mark.asyncio
async def test_versioned_file_fact_auto_activation_and_secret_exclusion(memory_app):
    app,run,source=memory_app
    app.memory.profile=app.memory.profile.model_copy(update={'auto_activate_observations':True,'semantic_consolidation':False})
    app.memory.consolidator.profile=app.memory.profile
    workspace=Path(run['data']['workspace'])
    (workspace/'project.json').write_text('{"rounding":"ROUND_HALF_UP"}')
    (workspace/'private.json').write_text('{"api_key":"DO_NOT_LEARN_THIS_VALUE"}')
    from conftest import tool
    for path in ['project.json','private.json']:await tool(app,run,'read',{'path':path})
    app.runtime.end(run['id'],'PARTIAL','observation capture')
    await app.memory.on_terminal(run['id'])
    rows=app.memory.repo.values('memory_versions')
    facts=[x for x in rows if x['kind']=='FACT']
    assert len(facts)==1 and facts[0]['file_dependencies']['project.json']
    assert 'DO_NOT_LEARN_THIS_VALUE' not in json.dumps(rows)


def test_summary_cas_rejects_second_commit_of_same_old_head(memory_app):
    app,run,_=memory_app;rows=app.memory.history.records(run['id']);fence=app.memory.compactor.fence(run['id'],rows)
    app.memory.compactor.commit(fence,rows,[],state(app,run))
    with pytest.raises(RepoError,match='SUMMARY_HEAD_CAS'):app.memory.compactor.commit(fence,rows,[],state(app,run))


@pytest.mark.asyncio
@pytest.mark.parametrize('verified',[False,True])
async def test_latest_closed_group_can_only_be_rebased_with_matching_profile(tmp_path,verified):
    from repo_code.providers import model_configuration
    source=tmp_path/'source';source.mkdir();p=ScriptedProvider(handler=extraction)
    profile=MemoryProfile(mode='lexical_only',max_tokens=8000,reserve_tokens=2000,safety_tokens=500,
        protocol_rebase_verified=verified,protocol_rebase_profile_digests=[digest(model_configuration(p))],protocol_rebase_tool_digests=[digest([])])
    app=RepoCode(tmp_path/'host',p,'local',memory_profile=profile)
    run=app.new_task('Preserve the original decision',source)
    record(app,run,'assistant','',tool_calls=[{'id':'large','name':'create','arguments':{'path':'module.py','content':'x'*24000}}])
    record(app,run,'tool','{"created":true}',tool_call_id='large')
    if not verified:
        with pytest.raises(RepoError,match='required state, summary'):await app.memory.prepare_context(run['id'],[])
    else:
        _,messages,manifest=await app.memory.prepare_context(run['id'],[])
        assert not any(m.get('tool_calls') for m in messages)
        assert manifest['summary_cutoff']>0 and manifest['estimated_input_tokens']<5500
        assert len(app.store.messages(run['id'])[1]['tool_calls'][0]['arguments']['content'])==24000


@pytest.mark.asyncio
async def test_request_can_be_reconstructed_and_usage_is_recorded(memory_app):
    app,run,_=memory_app
    system,messages,manifest=await app.memory.prepare_context(run['id'],[])
    row=app.memory.repo.get('context_requests',manifest['turn_request_id'])
    assert app.store.read_json(row['request_ref'])=={'system':system,'messages':messages,'tools':[]}
    assert row['request_ref']==manifest['serialized_request_digest']
    app.memory.record_usage(manifest,{'input_tokens':123,'output_tokens':45},.2)
    updated=app.memory.repo.get('context_requests',manifest['turn_request_id'])
    assert updated['usage_status']=='REPORTED' and updated['usage']['input_tokens']==123


def test_restored_v2_state_also_fences_legacy_snapshot_reads(memory_app,tmp_path):
    app,run,_=memory_app
    old_pin=app.runtime.knowledge.pin(run['data']['manifest']['scope'])
    app.runtime.end(run['id'],'PARTIAL','backup')
    backup=tmp_path/'backup';Maintenance(app.store).backup(backup)
    restored=tmp_path/'restored';restore_backup(backup,restored)
    from repo_code.storage import Store
    from repo_code.knowledge import Knowledge
    with pytest.raises(RepoError,match='REVOCATION_SYNC_REQUIRED'):Knowledge(Store(restored)).validate(*old_pin)


def test_report_cost_includes_auxiliary_and_unknown_usage(memory_app):
    app,run,_=memory_app;r=app.memory.repo
    key=r.reserve(run['id'],'compaction',100,100,8,1000);r.settle(key,{'prompt_tokens':100,'completion_tokens':20,'completion_tokens_details':{'reasoning_tokens':5}},.1)
    key=r.reserve(run['id'],'main',100,100,8,1000);r.settle(key,{},.1)
    cost=app.agent.report(run['id'])['memory_cost']
    assert cost['known_total_tokens']==120 and not cost['usage_complete']
    assert cost['by_purpose']['main']['conservative_reserved_tokens']==200


def test_activated_snapshot_has_active_lifecycle_and_distinct_file_conditions(memory_app):
    app,run,source=memory_app
    for version in ['sha256:old','sha256:new']:
        claim=proposal(app,run,'File-dependent observation',file_dependencies={'a.py':version})
        activate(app,claim)
    later=app.new_task('inspect knowledge',source)
    snapshot=app.store.read_json(later['data']['manifest']['knowledge_snapshot_digest'])
    assert len(snapshot['items'])==2
    assert all(app.store.read_json(e['digest'])['lifecycle']=='ACTIVE' for e in snapshot['items'])


def test_incomplete_vector_dimensions_cannot_publish_ready_index(memory_app):
    app,run,_=memory_app;claim=proposal(app,run);activate(app,claim)
    profile=MemoryProfile(encoder={'dimension':3})
    with pytest.raises(RepoError,match='MEMORY_INDEX_DIMENSION'):build_bundle(app.memory.repo,claim.scope_id,profile,RecordingEncoder())


@pytest.mark.parametrize('adapter,module,constructor,key_env',[('OpenAIProvider','openai','AsyncOpenAI','OPENAI_API_KEY'),('AnthropicProvider','anthropic','AsyncAnthropic','ANTHROPIC_API_KEY')])
def test_sdk_cannot_retry_outside_host_budget(monkeypatch,adapter,module,constructor,key_env):
    import sys
    from types import SimpleNamespace
    import repo_code.providers as providers
    captured={}
    def make(**kwargs):captured.update(kwargs);return SimpleNamespace()
    monkeypatch.setitem(sys.modules,module,SimpleNamespace(**{constructor:make}));monkeypatch.setenv(key_env,'local-fixture')
    getattr(providers,adapter)('fixture-model')
    assert captured['max_retries']==0


def test_empty_strict_snapshot_still_requires_available_models(tmp_path):
    source=tmp_path/'source';source.mkdir()
    app=RepoCode(tmp_path/'host',ScriptedProvider(),'local',memory_profile=MemoryProfile())
    with pytest.raises(RepoError,match='ENCODER_PIN_REQUIRED'):app.new_task('No silent lexical fallback',source)
    assert not app.memory.repo.values('snapshot_heads')


@pytest.mark.asyncio
async def test_final_report_includes_post_task_consolidation_cost(tmp_path):
    source=tmp_path/'source';source.mkdir()
    p=ScriptedProvider([Assistant(text='Finished exploration',usage={'input_tokens':10,'output_tokens':2}),
                        Assistant(text='{"status":"COMPLETE","claims":[]}',usage={'input_tokens':20,'output_tokens':3})])
    app=RepoCode(tmp_path/'host',p,'local',memory_profile=MemoryProfile(mode='lexical_only'))
    run=app.new_task('Explore the empty fixture',source)
    # A valuable failed-check signal exercises post-task extraction. Ordinary
    # LOW exploration now deliberately avoids a generative consolidation call.
    with app.store.tx() as c:
        app.store.emit(c,run['id'],'tool.operation_finished',{'tool_name':'process.run',
            'result':{'exit_code':1,'stderr':'AssertionError: observed counterexample'}},'DEV')
    result=await app.execute(run['id'])
    assert result['memory_cost']['known_total_tokens']==35
    assert result['memory_cost']['by_purpose']['consolidation']['calls']==1


def test_legacy_search_facade_does_not_silently_downgrade_v2(memory_app):
    app,run,_=memory_app;m=run['data']['manifest']
    with pytest.raises(RepoError) as captured:app.runtime.knowledge.search(m['knowledge_snapshot_digest'],m['knowledge_index_digest'],'query')
    assert captured.value.code=='ASYNC_MEMORY_SEARCH_REQUIRED'


def test_duplicate_evidence_uses_latest_version_and_preserves_reviewed_activation(memory_app):
    app,run,source=memory_app;first=proposal(app,run,'CSV_EMPTY_HEADER parser observation');activate(app,first)
    for _ in range(2):
        later=app.new_task('Record independent evidence',source)
        claim=proposal(app,later,'CSV_EMPTY_HEADER parser observation')
        app.memory.consolidator.commit([claim])
    rows=[r for r in app.memory.repo.values('memory_versions') if r['claim_id']==first.claim_id]
    assert max(r['version'] for r in rows)==3
    assert app.memory.repo.get('memory_control','claim:'+first.claim_id)=={'version':3,'lifecycle':'ACTIVE'}


def test_consolidation_similarity_is_only_a_review_relation(memory_app):
    app,run,source=memory_app;first=proposal(app,run,'CSV_EMPTY_HEADER parser observation');activate(app,first)
    later=app.new_task('Review related parser evidence',source)
    second=proposal(app,later,'CSV_EMPTY_HEADER parser diagnostic detail')
    related=app.memory.consolidator.compare_semantic(later['id'],[second])
    assert related and related[0]['relation']=='RELATED_REVIEW_REQUIRED'
    assert related[0]['mode']=='lexical_only' and related[0]['automatic_merge'] is False
    assert not app.memory.repo.get('memory_control','claim:'+second.claim_id)


@pytest.mark.parametrize('validity,status',[('HISTORICAL','UNKNOWN'),('STALE','FALSE'),('CONTRADICTED','FALSE')])
def test_old_or_contradicted_observation_cannot_be_a_current_fact(memory_app,validity,status):
    app,run,_=memory_app;key,view=app.memory.reducer.reduce(run['id']);intent=app.memory.reducer.intent(run['id'],key,view)
    assert applicability({'validity':validity,'epistemic_status':'OBSERVED'},intent,{})[0]==status


@pytest.mark.asyncio
async def test_explicit_language_correction_supersedes_without_resurrecting_old_preference(tmp_path):
    source=tmp_path/'source';source.mkdir()
    app=RepoCode(tmp_path/'host',ScriptedProvider(),'local',memory_profile=MemoryProfile(mode='lexical_only',auto_activate_preferences=True))
    run=app.new_task('Preference: language=English',source)
    first=app.memory.history.records(run['id'])[0]
    def entry(row):return {'kind':'USER_PREFERENCE','subject':'language','text':row['message']['content'],'quote':row['message']['content'],'source_ids':[row['ref']['source_id']]}
    p=ScriptedProvider([Assistant(text=json.dumps({'claims':[entry(first)]}))])
    old=await app.memory.consolidator.extract(run['id'],p);app.memory.consolidator.commit(old)
    record(app,run,'user','Correction: language=Chinese');latest=app.memory.history.records(run['id'])[-1]
    p=ScriptedProvider([Assistant(text=json.dumps({'claims':[entry(latest),entry(first)]}))])
    changes=await app.memory.consolidator.extract(run['id'],p);app.memory.consolidator.commit(changes)
    assert app.memory.repo.get('memory_control','claim:'+old[0].claim_id)['lifecycle']=='DEPRECATED'
    assert app.memory.repo.get('memory_control','claim:'+changes[0].claim_id)['lifecycle']=='ACTIVE'
    assert any(x['relation']=='SUPERSEDES' for x in app.memory.repo.values('memory_relations'))


def test_cli_temperature_matches_the_verified_protocol_profile():
    from types import SimpleNamespace
    from repo_code.providers import model_configuration
    from repo_code.cli import parser
    args=parser().parse_args(['--temperature','0','list'])
    left=OpenAIProvider('fixture',client=SimpleNamespace(),temperature=0)
    right=OpenAIProvider('fixture',client=SimpleNamespace(),temperature=args.temperature)
    assert model_configuration(left)==model_configuration(right)


@pytest.mark.asyncio
async def test_pinned_memory_policy_has_bounded_real_effect(memory_app):
    app,run,source=memory_app;claim=proposal(app,run,'CSV_EMPTY_HEADER parser observation');activate(app,claim)
    later=app.new_task('CSV_EMPTY_HEADER parser observation',source)
    _,enabled,manifest=await app.memory.prepare_context(later['id'],[],memory_policy={'top_k':5})
    assert json.loads(enabled[0]['content'])['retrieved_evidence']
    _,disabled,disabled_manifest=await app.memory.prepare_context(later['id'],[],memory_policy={'top_k':0,'authorized_scopes':['other']})
    assert json.loads(disabled[0]['content'])['retrieved_evidence']==[]
    assert manifest['knowledge_snapshot_digest']==disabled_manifest['knowledge_snapshot_digest']
    with pytest.raises(RepoError,match='INVALID_MEMORY_POLICY'):await app.memory.prepare_context(later['id'],[],memory_policy={'top_k':'unbounded'})


@pytest.mark.asyncio
async def test_pinned_source_expansion_does_not_authorize_edit_with_historical_hash(memory_app):
    app,run,source=memory_app
    from conftest import tool
    record(app,run,'assistant','',tool_calls=[{'id':'pinned-read','name':'read','arguments':{'path':'a.py'}}])
    req,receipt=await tool(app,run,'read',{'path':'a.py'},call_id='pinned-read');app.broker.append_result(req,receipt)
    row=app.memory.history.records(run['id'])[-1]
    claim=app.memory.consolidator.propose(run['id'],[{'kind':'FACT','text':'Observed value in a.py'}],[row['ref']['source_id']])[0];activate(app,claim)
    later=app.new_task('Review prior authorized evidence',source)
    expanded=app.memory.history.expand(later['id'],row['ref']['source_id'])
    assert 'value=1' in expanded['content'] and expanded['historical']
    with pytest.raises(RepoError) as error:
        app.broker.register_call(later['id'],'t',ToolCall(id='edit',name='edit',arguments={'path':'a.py','old_text':'1','new_text':'2','expected_version':receipt['result']['version']}))
    assert error.value.code=='READ_REQUIRED'


@pytest.mark.asyncio
async def test_revoked_source_artifact_invalidates_derived_summary(memory_app):
    app,run,_=memory_app
    from conftest import tool
    _,receipt=await tool(app,run,'read',{'path':'a.py'})
    # Match the real assistant/tool protocol surrounding this injected read.
    app.store.append_message(run['id'],{'role':'assistant','content':'','tool_calls':[{'id':'s','name':'read','arguments':{'path':'a.py'}}]})
    app.store.append_message(run['id'],{'role':'tool','tool_call_id':'s','content':json.dumps(receipt['result'])})
    key,_=await compact(app,run)
    app.memory.repo.revoke(receipt['result']['artifact'])
    with pytest.raises(RepoError,match='MEMORY_SOURCE_REVOKED'):app.memory.repo.check([key])


def test_semantic_inputs_redact_quoted_credentials_and_reject_secret_laundering(memory_app):
    app,run,_=memory_app
    marker='FAKE_PRIVATE_TOKEN_12345'
    record(app,run,'tool',json.dumps({'content':'1: {"api_key":"'+marker+'"}'}),tool_call_id='fake-source')
    records=app.memory.history.records(run['id'])
    assert marker not in json.dumps(records) and '[REDACTED]' in json.dumps(records)
    reference=records[0]['ref']['source_id']
    with pytest.raises(RepoError,match='SENSITIVE_MEMORY_CONTENT'):
        app.memory.consolidator.propose(run['id'],[{'kind':'FACT','text':'The observed value is '+marker}],[reference])
    app.store.put('plan',run['id'],{'steps':['Inspect '+marker]})
    key,view=app.memory.reducer.reduce(run['id']);intent=app.memory.reducer.intent(run['id'],key,view,marker)
    assert marker not in json.dumps(view) and marker not in intent.semantic_query


def test_learning_inherits_pinned_inputs_even_when_citations_omit_them(memory_app):
    app,run,source=memory_app;first=proposal(app,run,'First independent project observation');activate(app,first)
    later=app.new_task('Generate another observation',source)
    second=proposal(app,later,'Different observation with a clean citation');app.memory.consolidator.commit([second])
    current=app.new_task('Read derived knowledge',source)
    app.memory.repo.revoke(first.claim_id)
    version='memory-version:'+second.claim_id+':1'
    with pytest.raises(RepoError,match='MEMORY_SOURCE_REVOKED'):app.memory.repo.check([version])


@pytest.mark.asyncio
async def test_pinned_file_and_verification_selectors_expand_and_revoke(memory_app):
    app,run,source=memory_app
    (source/'project.json').write_text('{"base":35}')
    (Path(run['data']['workspace'])/'project.json').write_text('{"base":35}')
    from conftest import tool
    await tool(app,run,'read',{'path':'project.json'})
    file_claim=app.memory.consolidator.file_observations(run['id'])[0];activate(app,file_claim)
    app.runtime.end(run['id'],'PARTIAL','selector fixture')
    episode=app.memory.consolidator.terminal_claim(run['id'])[0];activate(app,episode)
    later=app.new_task('Inspect source-bound history',source)
    file_view=app.memory.history.expand(later['id'],file_claim.support_refs[0].source_id)
    event_view=app.memory.history.expand(later['id'],episode.support_refs[0].source_id)
    assert 'base' in file_view['content'] and '35' in file_view['content']
    assert 'PARTIAL' in event_view['content'] and not event_view['grants_edit_authority']
    app.memory.repo.revoke(file_claim.file_dependencies['project.json'])
    with pytest.raises(RepoError,match='MEMORY_SOURCE_REVOKED'):app.memory.history.expand(later['id'],file_claim.support_refs[0].source_id)


def test_encoder_pipe_failure_is_explicit_and_never_a_lexical_fallback(monkeypatch):
    from types import SimpleNamespace
    from repo_code.memory.encoders import LocalEncoder
    encoder=LocalEncoder({})
    def fail(_):raise BrokenPipeError()
    encoder.process=SimpleNamespace(stdin=SimpleNamespace(write=fail,close=lambda:None),wait=lambda timeout:0)
    monkeypatch.setattr(encoder,'_start',lambda:None)
    with pytest.raises(RepoError,match='ENCODER_PROCESS_FAILED'):encoder.embed(['authorized input'])
    assert encoder.process is None


def test_memory_schema_fences_old_writers_and_preserves_legacy_manifest(tmp_path):
    source=tmp_path/'source';source.mkdir()
    legacy=RepoCode(tmp_path/'host',ScriptedProvider(),'local')
    run=legacy.new_task('Legacy task',source);manifest=run['data']['manifest']
    with pytest.raises(RepoError,match='MEMORY_UPGRADE_REQUIRES_STOPPED_LEGACY_RUNS'):
        RepoCode(tmp_path/'host',ScriptedProvider(),'local',memory_profile=MemoryProfile(mode='lexical_only'))
    legacy.runtime.end(run['id'],'PARTIAL','Stopped before offline upgrade')
    upgraded=RepoCode(tmp_path/'host',ScriptedProvider(),'local',memory_profile=MemoryProfile(mode='lexical_only'))
    assert upgraded.runtime.run(run['id'])['data']['manifest']==manifest
    from repo_code.storage import Store
    reopened=Store(tmp_path/'host')
    with reopened.tx() as c:assert c.execute('PRAGMA user_version').fetchone()[0]==17
    assert reopened.messages(run['id'])
    upgraded.close()


@pytest.mark.asyncio
async def test_missing_summary_head_stops_instead_of_silently_dropping_prefix(memory_app):
    app,run,_=memory_app
    app.memory.repo.put('memory_control','summary:'+run['id'],{'id':'missing-node','revision':1,'cutoff':1})
    with pytest.raises(RepoError,match='SUMMARY_HEAD_MISSING'):await app.memory.prepare_context(run['id'],[])
    with pytest.raises(RepoError,match='SUMMARY_HEAD_MISSING'):await compact(app,run)


@pytest.mark.asyncio
async def test_standard_trials_keep_v2_but_disable_cross_task_learning(memory_app,monkeypatch):
    from repo_code.demo import task_fixture
    from repo_code.evaluation import TrialRunner
    app,_,_=memory_app
    closed=[]
    class SharedEncoder:
        identity='test-only-unused-lexical-worker'
        def close(self):closed.append(True)
    app.memory.encoder=SharedEncoder()
    observed=[]
    async def execute(trial,run_id,expose_control=False):
        run=trial.runtime.run(run_id);observed.append(run)
        assert trial.memory is not None and not trial.memory.profile.learning_enabled
        assert trial.memory.encoder is app.memory.encoder
        assert trial.store.read_json(run['data']['manifest']['knowledge_snapshot_digest'])['visibility']=='TRUSTED_ONLY'
        await trial.memory.on_correction(run_id)
        trial.runtime.end(run_id,'PARTIAL','Trial plumbing fixture')
        await trial.memory.on_terminal(run_id)
        return trial.agent.report(run_id)
    monkeypatch.setattr(RepoCode,'execute',execute)
    before=app.memory.repo.values('memory_versions')
    result=await TrialRunner(app,lambda task:ScriptedProvider()).run(task_fixture('memory-profile-trial'),app.releases.active()['digest'],'profile-trial')
    assert result['state']=='PARTIAL' and result['manifest']['memory_schema_version']==2
    assert observed[0]['data']['manifest']['scope']=='eval:profile-trial'
    assert app.memory.repo.values('memory_versions')==before
    assert not app.memory.repo.values('memory_jobs') and closed==[]
    assert 'memory_cost' in result


@pytest.mark.asyncio
async def test_trusted_partial_stage_is_idempotent_history_and_never_delivery(memory_app):
    app,run,_=memory_app
    from repo_code.models import PreparedDecision
    intent='test-only-stage-intent'
    app.store.put('intent',intent,{'run_id':run['id'],'source_digest':app.runtime.workspace(run).snapshot()})
    app.store.put('evidence','unit-stage-evidence',{'id':'unit-stage-evidence','check_id':'one-of-two','status':'PASSED'})
    decision=PreparedDecision(intent_digest=intent,verified=False,reason='Other required check failed',evidence_ids=['unit-stage-evidence'])
    decision=decision.model_copy(update={'signature':app.store.sign(decision.model_dump(mode='json',exclude={'signature'}))})
    app.store.put('decision',intent,decision)
    await app.memory.on_verified_stage(run['id'],decision)
    await app.memory.on_verified_stage(run['id'],decision)
    claims=app.memory.repo.values('memory_versions')
    assert len(claims)==1 and claims[0]['kind']=='EPISODE' and claims[0]['validity']=='HISTORICAL'
    assert '"task_delivery_verified":false' in claims[0]['text']
    assert len(app.memory.repo.values('memory_jobs'))==1
    assert app.runtime.run(run['id'])['state']=='RUNNING' and app.agent.report(run['id'])['delivery'] is None
    with pytest.raises(RepoError,match='UNTRUSTED_DECISION'):
        await app.memory.on_verified_stage(run['id'],decision.model_copy(update={'verified':True}))
