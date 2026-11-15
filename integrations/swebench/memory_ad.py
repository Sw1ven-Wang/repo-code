"""Frozen, once-per-arm SWE-bench DEV A/D evaluation; trusted controller only.

Run preflight and official gold/empty/baseline controls before `agent`. The API
credential is read from stdin and never forwarded to task-container env/files.
"""
import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from openai import AsyncOpenAI
from adapter import (ROOT, OUT, App, Environment, prepare_container, save,
                     trusted_baseline, patch_from_workspace)
from driver import prompt, public_tasks, images
from export_policy import load_verified_policy
from repo_code.context import ContextEngine
from repo_code.memory.encoders import LocalEncoder
from repo_code.memory.models import MemoryProfile
from repo_code.memory.service import memory_implementation_digest
from repo_code.models import Budget
from repo_code.providers import OpenAIProvider, ScriptedProvider, model_configuration
from repo_code.util import RepoError, digest
from learning_transfer import export_observations,import_observations
from regression_fixtures import prove_additions

CODE=Path(__file__).resolve().parent
LOCK=ROOT/'experiment-lock.json'
ARMS=('A','D-off','D-on')
SHARED_SCOPE='eval:swebench-dev2-memory-learning-64k'


def experiment_spec():
    spec=json.loads((ROOT/'experiment-spec.json').read_text())
    if spec['context_window']!=64000 or spec['arms']!=list(ARMS):raise RepoError('INVALID_EXPERIMENT_SPEC')
    return spec


def hash_files(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file() and not {'__pycache__','.pytest_cache'}&set(p.parts) and p.suffix!='.pyc'}


def profile_for(lock,arm='D-off'):
    return MemoryProfile.model_validate(lock['effective_memory_profiles'][arm])


class FixedLegacyWindow(ContextEngine):
    def build(self,run_id,tools,policy=None,*args,**kwargs):
        return super().build(run_id,tools,{**(policy or {}),'max_tokens':63500,'reserve_tokens':4096},*args,**kwargs)


class AuditedProvider(OpenAIProvider):
    def __init__(self,dest,**kwargs):
        super().__init__(**kwargs);self.dest=dest;self.records=[];self.halt=None
    async def complete(self,system,messages,tools,on_delta=None,*,json_mode=False):
        from repo_code.memory.budget import ContextBudgetCoordinator
        options={'json_mode':True} if json_mode else {}
        coordinator=ContextBudgetCoordinator(self,64000,4096,500,request_options=options)
        budget=coordinator.assert_fits(system,messages,tools)
        if self.halt:raise RepoError('EVALUATION_PROVIDER_STOP',self.halt)
        purpose=('compaction' if system.startswith('Extract compact task-memory') else
                 'consolidation' if system.startswith('Extract up to 8 evidence-bound') else 'main' if tools else 'auxiliary')
        rec={'call':len(self.records)+1,'purpose':purpose,'started':time.time(),
             'request_digest':digest(self.build_request(system,messages,tools,**options)[0]),
             'tool_schema_digest':digest(tools),'estimated_input':budget.estimated_input_total,'status':'STARTED'}
        self.records.append(rec);save(self.dest/'api_usage.json',self.records)
        try:
            reply=await super().complete(system,messages,tools,on_delta,**options)
            rec.update(status='COMPLETED',usage=reply.usage,stop_reason=reply.stop_reason,
                       tool_names=[t.name for t in reply.tool_calls])
            usage=reply.usage
            if not isinstance(usage.get('prompt_tokens'),int) or not isinstance(usage.get('completion_tokens'),int):
                raise RepoError('PROVIDER_USAGE_UNAVAILABLE')
            audit=coordinator.audit_usage(usage,budget);rec['context_audit']=audit
            if audit['hard_violation']:
                raise RepoError('ACTUAL_CONTEXT_BUDGET_EXCEEDED')
            return reply
        except BaseException as exc:
            self.halt=getattr(exc,'code',type(exc).__name__)
            rec.update(status='FAILED',error_code=self.halt)
            rec.setdefault('usage',getattr(exc,'usage',{}))
            raise
        finally:
            rec['finished']=time.time();save(self.dest/'api_usage.json',self.records)


def configure(app,lock,arm):
    # Same complete Release for A and D. Only the memory profile differs.
    candidate=app.releases.candidate('swebench-memory-ad-context',
        {'context_policy':{'kind':'config','max_tokens':64000,'reserve_tokens':4096}},
        'Frozen A/D-off/D-on at 64k; explicit learning scope',['user-memory-ad64-learning-20261102'])
    if arm=='A':
        app.agent.context=FixedLegacyWindow(app.store,app.runtime,app.agent.context.request_serializer,
            app.agent.context.output_reserve,app.agent.context.input_scale,provider=app.provider)
    return candidate.candidate_release_digest


def create_run(app,row,source,release,lock,provider):
    run=app.runtime.create(prompt(row),source,release,budget=Budget(**lock['budget']),
        scope=SHARED_SCOPE,model_config=model_configuration(provider),visibility='DEV')
    app.broker.authorize_run(run['id'],True,True,False)
    return run


def check_lock():
    lock=json.loads(LOCK.read_text())
    if lock['source_files']!=hash_files(ROOT/'source/src'):raise RepoError('FROZEN_SOURCE_CHANGED')
    if lock['integration_files']!=hash_files(CODE):raise RepoError('FROZEN_ADAPTER_CHANGED')
    if lock['memory_implementation_digest']!=memory_implementation_digest():raise RepoError('MEMORY_IMPLEMENTATION_CHANGED')
    if lock['network_policy']!=Environment.policy():raise RepoError('NETWORK_CHANGED')
    if lock['images']!=images():raise RepoError('TASK_IMAGES_CHANGED')
    if lock['experiment_spec_digest']!=digest(experiment_spec()):raise RepoError('EXPERIMENT_SPEC_CHANGED')
    return lock


async def preflight():
    if LOCK.exists():raise RepoError('EXPERIMENT_ALREADY_FROZEN')
    reference=json.loads((ROOT/'reference-lock.json').read_text())
    spec=experiment_spec()
    profile=reference['memory_profile'].copy()
    # Relocate only the host path; LocalEncoder identity excludes models_path.
    profile['encoder']={**profile['encoder'],'models_path':os.environ['MEMORY_MODELS_LINUX_PATH']}
    profile.update(total_token_budget=None,learning_enabled=False,max_tokens=64000,
        implementation_digest=memory_implementation_digest())
    on_profile={**profile,'learning_enabled':True,'auto_activate_observations':spec['auto_activate_host_observations']}
    lock={'experiment':'SWE-bench Lite DEV-2 A/D-off/D-on; six once-only runs at 64k',
          'reference_lock_sha256':hashlib.sha256((ROOT/'reference-lock.json').read_bytes()).hexdigest(),
          'model':reference['model'],'sdk_version':importlib.metadata.version('openai'),
          'budget':{**reference['per_run_budget'],'child_calls':0},'token_budget':None,'monetary_ceiling_cny':None,
          'budget_override':'User removed cumulative DeepSeek token and prior monetary limits. Context/output/tool/wall constraints retained.',
          'effective_memory_profiles':{'D-off':profile,'D-on':on_profile},'memory_implementation_digest':memory_implementation_digest(),
          'reference_memory_implementation_digest':reference['memory_profile']['implementation_digest'],
          'implementation_change_authorization':'User requested context-usage classification and pre-execution budget-denial repairs.',
          'source_files':hash_files(ROOT/'source/src'),'integration_files':hash_files(CODE),
          'images':images(),'network_policy':Environment.policy(),'shared_learning':{'A':False,'D-off':False,'D-on':True},
          'order':[[t['instance_id'],a] for t in public_tasks() for a in ARMS],
          'test_20_executed':False,'l1_enabled':False,'l2_enabled':False,'attempts_per_arm_task':1,
          'context_window_all':64000,'output_reserve_all':4096,'safety_all':500,
          'legacy_window_minus_safety':63500,'old_scores_used_as_control':False,
          'experiment_spec':spec,'experiment_spec_digest':digest(spec),'learning_scope':SHARED_SCOPE,
          'learning_transfer':'Only active native Host observations with scope, digest and public evidence validation; no state/database clone.',
          'harness_version':importlib.metadata.version('swebench'),
          'task_environment_exception':'User selected official task-specific images instead of the reference authored-task sandbox.'}
    encoder=LocalEncoder(profile_for(lock).encoder)
    try:
        lock['encoder_health']=await asyncio.to_thread(encoder.healthcheck)
        vectors=await asyncio.to_thread(encoder.embed,['Memory A/D preflight only'])
        assert len(vectors)==1 and len(vectors[0])==384
    finally:encoder.close()
    records=[];expected_release=None
    for row in public_tasks():
        baseline=await asyncio.to_thread(trusted_baseline,row)
        for arm in ARMS:
            env=None;app=None
            try:
                state=Path('/work/ad64-preflight')/digest(lock['integration_files'])[7:19]/row['instance_id']/arm
                if state.exists():raise RepoError('PREFLIGHT_STATE_ALREADY_EXISTS')
                env=await prepare_container(row,images(),'rc-ad64-preflight-'+row['instance_id']+'-'+arm.lower())
                app=App(state,ScriptedProvider(),env,images(),digest(lock['integration_files']),
                    memory_profile=profile_for(lock,arm) if arm!='A' else None)
                source=state/'public-source';files,meta=await app.sandbox.export(source);app.sandbox.synced=files
                if meta['omitted_symlinks']:raise RepoError('BLOCKED_ENV_SYMLINKS')
                tracked=subprocess.check_output(['git','ls-files','-z'],cwd=baseline).decode().split('\0')
                for name in filter(None,tracked):
                    if name in env.readonly_assets:
                        a=env.readonly_assets[name]
                        assert hashlib.sha256((baseline/name).read_bytes()).hexdigest()==a['sha256']
                    elif not (source/name).is_file() or (baseline/name).read_bytes()!=(source/name).read_bytes():
                        raise RepoError('BASELINE_SOURCE_MISMATCH',name)
                release=configure(app,lock,arm)
                if expected_release and release!=expected_release:raise RepoError('RELEASE_NOT_FIXED')
                expected_release=release
                run=create_run(app,row,source,release,lock,app.provider)
                module=row['repo'].split('/')[-1]
                probe=await env.exec('python -c '+__import__('shlex').quote('import '+module+';print("IMPORT_OK")'))
                if probe.return_code or probe.stdout.strip()!='IMPORT_OK':raise RepoError('BLOCKED_ENV_IMPORT',probe.stderr[-1000:])
                app.runtime.end(run['id'],'PARTIAL','OFFLINE_PREFLIGHT_NO_MODEL')
                records.append({'instance_id':row['instance_id'],'arm':arm,'source_snapshot':digest({'files':files}),
                    'release_digest':release,'isolation':env.inspect_isolation(),'memory_profile_digest':app.memory.profile_digest if app.memory else None})
                print('PREFLIGHT_READY',row['instance_id'],arm,flush=True)
            finally:
                if app:app.close()
                if env:await env.stop()
    lock.update(preflight=records,release_digest=expected_release,status='READY_FOR_OFFICIAL_CONTROLS')
    save(LOCK,lock)


def controls_ready():
    lock=check_lock();ids={t['instance_id'] for t in public_tasks()}
    for kind in ['gold','empty','baseline']:
        spec=json.loads((OUT/'config'/f'{kind}_official.json').read_text())
        report=json.loads(Path(spec['results']).read_text())
        if spec['exit_code'] or report['error_ids']:raise RepoError('BLOCKED_ENV_OFFICIAL_CONTROL')
        if kind=='gold' and set(report['resolved_ids'])!=ids:raise RepoError('GOLD_CONTROL_FAILED')
        if kind=='empty' and set(report['empty_patch_ids'])!=ids:raise RepoError('EMPTY_CONTROL_FAILED')
        if kind=='baseline' and set(report['unresolved_ids'])!=ids:raise RepoError('BASELINE_CONTROL_FAILED')
    for row in public_tasks():load_verified_policy(OUT,row['instance_id'],row['base_commit'])
    return lock


async def agent(api_key):
    lock=controls_ready()
    carry=None
    for row in public_tasks():
        for arm in ARMS:
            check_lock()
            dest=OUT/'tasks'/row['instance_id']/arm;dest.mkdir(parents=True,exist_ok=True)
            with (dest/'attempt-started.json').open('x') as f:json.dump({'attempt':1,'started':time.time()},f)
            state=Path('/work/ad64-state')/row['instance_id']/arm
            if state.exists():raise RepoError('STATE_ROOT_NOT_FRESH')
            client=AsyncOpenAI(api_key=api_key,base_url=lock['model']['endpoint'],max_retries=0,timeout=90)
            m=lock['model'];provider=AuditedProvider(dest,client=client,model=m['model'],base_url=m['endpoint'],
                thinking=m['thinking'],max_tokens=m['max_tokens'],temperature=m['temperature'])
            env=None;app=None;run=None;result=None;error=None;patch=b'';changed=[];started=time.monotonic()
            print('AGENT_START',row['instance_id'],arm,flush=True)
            try:
                env=await prepare_container(row,images(),'rc-ad64-agent-'+row['instance_id']+'-'+arm.lower())
                app=App(state,provider,env,images(),digest(lock['integration_files']),memory_profile=profile_for(lock,arm) if arm!='A' else None,
                        verifier_policy=load_verified_policy(OUT,row['instance_id'],row['base_commit']))
                if arm=='D-on' and carry is not None:
                    imported=import_observations(app,carry['capsule'],carry['digest'],SHARED_SCOPE)
                    save(dest/'learning-import.json',{'claims_imported':imported,'capsule_digest':carry['digest'],
                        'source_run_id':carry['capsule']['source_run_id'],'previous_messages_imported':0,'host_key_imported':False})
                source=state/'public-source';files,_=await app.sandbox.export(source);app.sandbox.synced=files
                expected=next(x for x in lock['preflight'] if x['instance_id']==row['instance_id'] and x['arm']==arm)
                if digest({'files':files})!=expected['source_snapshot']:raise RepoError('SOURCE_CHANGED_AFTER_PREFLIGHT')
                release=configure(app,lock,arm)
                if release!=lock['release_digest']:raise RepoError('RELEASE_CHANGED')
                run=create_run(app,row,source,release,lock,provider)
                save(dest/'initial-manifest.json',run['data']['manifest'])
                result=await app.execute(run['id'])
                await app.sandbox.sync(Path(result['workspace']))
                policy=load_verified_policy(OUT,row['instance_id'],row['base_commit'])
                save(dest/'trusted-export-policy.json',policy.audit())
                patch,changed=patch_from_workspace(trusted_baseline(row),source,Path(result['workspace']),
                    Path('/work/ad64-replay')/(row['instance_id']+'-'+arm),policy,prove_additions(source,Path(result['workspace'])))
                if arm=='D-on' and result['state'] in {'PARTIAL','SUCCEEDED','FAILED','BLOCKED'}:
                    capsule=export_observations(app,run['id']);carry={'capsule':capsule,'digest':digest(capsule)}
                    save(dest/'learning-export.json',{'capsule':capsule,'capsule_digest':carry['digest']})
            except BaseException as exc:
                error={'code':getattr(exc,'code',type(exc).__name__),'details':str(exc)[:1500]}
            finally:
                if run and app:
                    result=result or app.agent.report(run['id'])
                    save(dest/'repo-code-result.json',result)
                    save(dest/'trajectory.json',app.store.messages(run['id']))
                    save(dest/'events.json',app.store.events(run['id'],trusted=True,limit=100000))
                    if app.memory:
                        save(dest/'memory-budget.json',app.memory.repo.values('memory_budget'))
                        save(dest/'summary-nodes.json',app.memory.repo.values('summary_nodes'))
                        save(dest/'memory-versions.json',app.memory.repo.values('memory_versions'))
                        save(dest/'retrieval-traces.json',app.memory.repo.values('retrieval_traces'))
                    else:save(dest/'legacy-projection.json',app.store.get('projection',run['id']))
                if app:app.close()
                if env:await env.stop()
                await client.close()
                (dest/'patch.diff').write_bytes(patch)
                (dest/'errors.log').write_text(json.dumps(error,ensure_ascii=False)+'\n' if error else '')
                usage=[x.get('usage',{}) for x in provider.records]
                record={'instance_id':row['instance_id'],'arm':arm,'attempt':1,'status':'ADAPTER_ERROR' if error else 'COMPLETED',
                    'error':error,'run_id':run['id'] if run else None,'state_root':str(state),'release_digest':lock['release_digest'],
                    'repo_code_state':(result or {}).get('state'),'repo_code_reason':(result or {}).get('reason'),
                    'model_calls':len(provider.records),'main_calls':sum(x['purpose']=='main' for x in provider.records),
                    'tool_calls':(result or {}).get('budget_used',{}).get('tool_calls',0),
                    'input_tokens':sum(x.get('prompt_tokens',0) for x in usage),'output_tokens':sum(x.get('completion_tokens',0) for x in usage),
                    'unknown_usage_calls':sum(not x for x in usage),'wall_seconds':time.monotonic()-started,'api_actual_cost':None,
                    'patch_bytes':len(patch),'patch_sha256':hashlib.sha256(patch).hexdigest(),'changed_files':changed,'patch_valid':error is None,
                    'cross_task_learning':arm=='D-on','memory_enabled':arm!='A','official_resolved':None,'provider_halt':provider.halt,
                    'context_safety_warnings':sum(r.get('context_audit',{}).get('status') in {'SAFETY_MARGIN_USED','RESERVE_SHORTFALL'} for r in provider.records),
                    'actual_context_violations':sum(r.get('context_audit',{}).get('hard_violation',False) for r in provider.records)}
                save(dest/'run.json',record);aggregate();print('AGENT_DONE',json.dumps(record),flush=True)
            if error or provider.halt or record['repo_code_state'] in {'NEEDS_RECONCILIATION','CONTROL_PENDING','WAITING_APPROVAL','BLOCKED'}:
                raise RepoError('PAID_RUN_STOP_CONDITION',str(error or record['repo_code_reason']))


def aggregate():
    rows=[]
    for arm in ARMS:
        predictions=[]
        for row in public_tasks():
            dest=OUT/'tasks'/row['instance_id']/arm
            if not (dest/'run.json').exists():continue
            rows.append(json.loads((dest/'run.json').read_text()))
            predictions.append({'instance_id':row['instance_id'],'model_name_or_path':'repo-code-memory-'+arm+'__deepseek-flash',
                                'model_patch':(dest/'patch.diff').read_text()})
        (OUT/'results'/f'{arm}_predictions.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in predictions))
    save(OUT/'results/progress.json',rows)
    if rows:
        with (OUT/'results/usage.csv').open('w',newline='') as f:
            fields=['instance_id','arm','model_calls','main_calls','tool_calls','input_tokens','output_tokens','unknown_usage_calls','wall_seconds','api_actual_cost','patch_bytes','status']
            w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['preflight','agent','aggregate']);a=p.parse_args()
    if a.phase=='preflight':asyncio.run(preflight())
    elif a.phase=='agent':asyncio.run(agent(json.loads(sys.stdin.read())['api_key']))
    else:aggregate()
