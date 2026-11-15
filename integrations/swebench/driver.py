"""Single-attempt DEV-2 driver. Agent phase requires all offline/official gates."""
import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import time
from adapter import (ROOT,OUT,Environment,App,CostProvider,prepare_container,
    trusted_baseline,patch_from_workspace,save,projected)
from repo_code.models import Budget
from repo_code.providers import ScriptedProvider
from repo_code.util import RepoError,digest
from openai import AsyncOpenAI
from export_policy import load_verified_policy
from regression_fixtures import prove_additions

LABEL='repo-code-v1.5.1__deepseek-flash'
MODEL={'model':'deepseek-flash','base_url':'https://api.deepseek.com','thinking':'enabled',
    'reasoning_effort':'high','max_tokens':8192,'temperature':'omitted','top_p':'omitted',
    'sdk_max_retries':0,'sdk_timeout_seconds':180,'sdk_version':'3.42.0'}
BUDGET={'model_calls':30,'tool_calls':100,'wall_seconds':1200,'child_calls':0}

def public_tasks():
    rows=json.loads((OUT/'frozen/public_tasks.json').read_text())
    manifest=json.loads((OUT/'frozen/selection_manifest.json').read_text())
    assert len(rows)==2 and [r['instance_id'] for r in rows]==[r['instance_id'] for r in manifest['dev_pilot']]
    assert len(manifest['test_20'])==20
    for r,m in zip(rows,manifest['dev_pilot']):
        assert set(r)=={'instance_id','repo','base_commit','problem_statement'}
        assert r['base_commit']==m['base_commit'] and r['repo']==m['repo']
        assert hashlib.sha256(r['problem_statement'].encode()).hexdigest()==m['problem_sha256']
    return rows

def build_digest():
    code=Path(__file__).resolve().parent
    files={n:digest((code/n).read_bytes()) for n in ('adapter.py','driver.py','export_policy.py','regression_fixtures.py',
            'write_policy.py','audited_provider.py','trace_metrics.py')}
    view=OUT/'config/workspace_view.json'
    if view.exists():files['workspace_view.json']=digest(view.read_bytes())
    return digest(files)

def images():return json.loads((OUT/'config/images.json').read_text())

def prompt(row):
    from write_policy import PUBLIC_POLICY_HINT
    policy=Environment.policy()
    network=('Outbound Internet access is enabled for package and dependency downloads. '
        'Do not retrieve benchmark solutions, gold patches, hidden tests or grading results. '
        'The installed repository and its pinned base commit remain the source of truth; do not replace the checkout with a downloaded release. '
        if policy['mode']=='public' else 'Internet access is disabled in this task container; use installed dependencies. ')
    return ('Repository: '+row['repo']+'\nBase commit: '+row['base_commit']+'\n\n'+row['problem_statement']+
        '\n\nImplement the issue in the repository. File tools use paths relative to /testbed; '
        'process.run starts in /testbed in the installed testbed environment. Read before editing. '
        'Keep changes focused on implementation; evaluation configuration is protected. '
        'You may add independent regression tests. Hidden evaluation tests and Git metadata are unavailable. '
        'Delegation, MCP, and asynchronous process.start are unavailable. '+network+'Finish when the patch is ready. '
        'Use workspace.diff instead of git diff/status. '+PUBLIC_POLICY_HINT)

async def preflight():
    records=[];release=None
    for row in public_tasks():
        print('PREFLIGHT',row['instance_id'],flush=True)
        env=None
        try:
            env=await prepare_container(row,images(),'rc-dev-preflight-'+row['instance_id'])
            module=row['repo'].split('/')[-1].replace('-','_')
            if not module.isidentifier():raise RepoError('INVALID_MODULE_NAME')
            probe=await env.exec('python3 -I -c '+__import__('shlex').quote('import importlib;importlib.import_module('+repr(module)+');print("IMPORT_OK")'))
            if probe.return_code:raise RepoError('BLOCKED_ENV_DEPENDENCY_IMPORT',probe.stderr[-1000:])
            app=App(Path('/work/pilot2/preflight')/build_digest().split(':')[-1][:12]/row['instance_id'],ScriptedProvider(),env,images(),build_digest())
            source=app.store.root/'public-source'
            files,meta=await app.sandbox.export(source);app.sandbox.synced=files
            if meta['omitted_symlinks']:raise RepoError('BLOCKED_ENV_SYMLINKS')
            baseline=await asyncio.to_thread(trusted_baseline,row)
            # The task image's tracked baseline bytes must match the public Git commit.
            tracked=__import__('subprocess').check_output(['git','ls-files','-z'],cwd=baseline).decode().split('\0')
            for name in tracked:
                if not name:continue
                a=baseline/name;b=source/name
                if a.is_symlink():raise RepoError('BLOCKED_ENV_SYMLINKS',name)
                if name in env.readonly_assets:
                    asset=env.readonly_assets[name]
                    if a.stat().st_size!=asset['bytes'] or hashlib.sha256(a.read_bytes()).hexdigest()!=asset['sha256']:
                        raise RepoError('READONLY_ASSET_BASELINE_MISMATCH',name)
                    continue
                if not b.is_file() or a.read_bytes()!=b.read_bytes():raise RepoError('BASELINE_SOURCE_MISMATCH',name)
            run=app.new_task(prompt(row),source,budget=Budget(**BUDGET),authorize_write=True,authorize_process=True)
            rd=run['data']['manifest']['release_digest']
            if release is not None and release!=rd:raise RepoError('RELEASE_NOT_FIXED')
            release=rd
            app.runtime.end(run['id'],'PARTIAL','OFFLINE_PREFLIGHT_NO_MODEL_EXECUTION')
            records.append({'instance_id':row['instance_id'],'status':'READY','base_commit':row['base_commit'],
                'files':len(files),'source_snapshot':digest({'files':files}),'release_digest':rd,
                'git_absent_from_workspace':not (source/'.git').exists(),'image':images()[row['instance_id']],
                'readonly_assets':env.readonly_assets,'baseline_source_verified':True,
                'network_policy':env.network_policy})
        except Exception as exc:
            info=None
            if env:
                probe=await env.exec("python3 -I -c "+__import__('shlex').quote("import os,json;from pathlib import Path;root=Path('/testbed');big=[];total=0;count=0\nfor base,dirs,files in os.walk(root):\n dirs[:]=[d for d in dirs if d not in {'.git','.venv','venv','__pycache__','.pytest_cache','.mypy_cache','.ruff_cache','node_modules'}]\n for name in files:\n  p=Path(base)/name\n  if p.is_file() and not p.is_symlink():\n   s=p.stat().st_size;total+=s;count+=1\n   if s>8*1024*1024:big.append({'path':str(p.relative_to(root)),'bytes':s})\nprint(json.dumps({'files':count,'total_bytes':total,'files_over_8MiB':big}))"))
                if not probe.return_code:info=json.loads(probe.stdout)
            records.append({'instance_id':row['instance_id'],'status':'BLOCKED_ENV','error_code':getattr(exc,'code',type(exc).__name__),'error':str(exc)[-2000:],'workspace_diagnostics':info,'model_calls':0})
            print('BLOCKED_ENV',row['instance_id'],str(exc)[-300:],flush=True)
        finally:
            if env:await env.stop()
    if importlib.metadata.version('openai')!=MODEL['sdk_version']:raise RepoError('SDK_NOT_FROZEN')
    manifest={'experiment':'DEV-2 smoke test; not TEST-20 score','model_label':LABEL,'model':MODEL,
        'budget_per_task':BUDGET,'cost_limit_cny':20,'cost_stop_input_cny_per_million':2,
        'cost_stop_output_cny_per_million':8,'cost_mode':'reserve worst-case before each request; settle actual Usage; unknown Usage halts pilot',
        'actual_api_invoice_cost':'N/A unless account billing provides it',
        'pricing_source':'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
        'sdk':importlib.metadata.version('openai'),'harness':'4.1.0',
        'harness_commit':'726c5461e2ef52d83cf1ea2107870a8bb3328d57',
        'source':json.loads((OUT/'config/source_manifest.json').read_text()),
        'adapter_digest':build_digest(),'release_digest':release,'l1_enabled':False,'l2_enabled':False,
        'selection_sha256':hashlib.sha256((OUT/'frozen/selection_manifest.json').read_bytes()).hexdigest(),
        'tasks':records,'preflight_status':'READY' if all(r['status']=='READY' for r in records) else 'BLOCKED_ENV',
        'network_policy':Environment.policy()}
    save(OUT/'config/model_and_release_manifest.json',manifest)
    print('PREFLIGHT_'+manifest['preflight_status'],release,flush=True)

def controls_ready():
    ids={r['instance_id'] for r in public_tasks()}
    for kind in ('gold','empty','baseline'):
        meta=json.loads((OUT/'config'/f'{kind}_official.json').read_text())
        report=json.loads(Path(meta['results']).read_text())
        if meta['exit_code'] or report['error_ids']:raise RepoError('BLOCKED_ENV_OFFICIAL_CONTROL')
        if kind=='gold' and set(report['resolved_ids'])!=ids:raise RepoError('GOLD_CONTROL_FAILED')
        if kind=='empty' and (report['resolved_ids'] or set(report['empty_patch_ids'])!=ids):raise RepoError('EMPTY_CONTROL_FAILED')
        if kind=='baseline' and (report['resolved_ids'] or set(report['unresolved_ids'])!=ids):raise RepoError('BASELINE_CONTROL_FAILED')
    manifest=json.loads((OUT/'config/model_and_release_manifest.json').read_text())
    if manifest.get('preflight_status')!='READY':raise RepoError('BLOCKED_ENV_PREFLIGHT')
    if manifest.get('network_policy')!=Environment.policy():raise RepoError('NETWORK_POLICY_CHANGED_AFTER_PREFLIGHT')
    assert manifest['adapter_digest']==build_digest()
    assert manifest['selection_sha256']==hashlib.sha256((OUT/'frozen/selection_manifest.json').read_bytes()).hexdigest()
    assert json.loads((ROOT/'adapter-tests.json').read_text())['passed']
    for task in manifest['tasks']:
        assert task['image']==images()[task['instance_id']]
    return manifest

async def agent(api_key):
    manifest=controls_ready();ledger=OUT/'results/cost_ledger.json'
    if not ledger.exists():save(ledger,{'limit_cny':20,'charged_upper_cny':0.0,'halt':None})
    for row in public_tasks():
        dest=OUT/'tasks'/row['instance_id'];dest.mkdir(parents=True,exist_ok=True)
        marker=dest/'attempt-started.json'
        with marker.open('x') as file:json.dump({'started':time.time(),'attempt':1},file)
        print('AGENT_START',row['instance_id'],flush=True)
        started=time.monotonic();env=None;app=None;run=None;result=None;failure=None;patch=b'';changed=[]
        client=AsyncOpenAI(api_key=api_key,base_url=MODEL['base_url'],max_retries=0,timeout=180)
        provider=CostProvider(ledger,dest/'api_usage.json',client=client,model=MODEL['model'],
            base_url=MODEL['base_url'],thinking=MODEL['thinking'],reasoning_effort=MODEL['reasoning_effort'],max_tokens=MODEL['max_tokens'])
        try:
            env=await prepare_container(row,images(),'rc-dev-agent-'+row['instance_id'])
            app=App(Path('/work/pilot2/agent-state')/row['instance_id'],provider,env,images(),build_digest(),
                    verifier_policy=load_verified_policy(OUT,row['instance_id'],row['base_commit']))
            source=app.store.root/'public-source'
            files,meta=await app.sandbox.export(source);app.sandbox.synced=files
            expected=next(r for r in manifest['tasks'] if r['instance_id']==row['instance_id'])
            if digest({'files':files})!=expected['source_snapshot']:raise RepoError('SOURCE_CHANGED_AFTER_PREFLIGHT')
            run=app.new_task(prompt(row),source,budget=Budget(**BUDGET),authorize_write=True,authorize_process=True)
            app.broker.authorize_run(run['id'],True,True,False)
            if run['data']['manifest']['release_digest']!=manifest['release_digest']:raise RepoError('RELEASE_CHANGED')
            result=await app.execute(run['id'])
            # PARTIAL is expected without an internal Contract. Preserve all file artifacts.
            if result['state'] in {'NEEDS_RECONCILIATION','CONTROL_PENDING','CANCELLED','WAITING_APPROVAL'}:
                raise RepoError('ADAPTER_'+result['state'])
            if result['state']=='BLOCKED':failure={'code':result.get('reason'),'details':'Original repo-code runtime blocked the run; artifacts retained'}
            await app.sandbox.sync(Path(result['workspace']))
            # Keep the trusted source snapshot as baseline; no gold or judge data is read.
            baseline=trusted_baseline(row)
            policy=load_verified_policy(OUT,row['instance_id'],row['base_commit'])
            save(dest/'trusted-export-policy.json',policy.audit())
            patch,changed=patch_from_workspace(baseline,source,Path(result['workspace']),Path('/work/pilot2/replay')/row['instance_id'],policy,
                prove_additions(source,Path(result['workspace'])))
        except BaseException as exc:
            failure={'code':getattr(exc,'code',type(exc).__name__),'details':str(exc)[:2000]}
        finally:
            if run and app:
                result=result or app.agent.report(run['id'])
                save(dest/'repo-code-result.json',result)
                save(dest/'trajectory.json',app.store.messages(run['id']))
            if env:await env.stop()
            await client.close()
            (dest/'patch.diff').write_bytes(patch)
            (dest/'errors.log').write_text(json.dumps(failure,ensure_ascii=False)+'\n' if failure else '')
            usage=[r.get('usage',{}) for r in provider.records]
            record={'instance_id':row['instance_id'],'attempt':1,'status':'ADAPTER_ERROR' if failure else 'COMPLETED',
                'error':failure,'repo_code_state':result.get('state') if result else None,
                'repo_code_reason':result.get('reason') if result else None,'base_commit':row['base_commit'],
                'release_digest':manifest['release_digest'],'model_calls':len(provider.records),
                'network_policy':Environment.policy(),
                'tool_calls':result['budget_used']['tool_calls'] if result else 0,
                'input_tokens':sum(u.get('prompt_tokens',0) for u in usage),
                'output_tokens':sum(u.get('completion_tokens',0) for u in usage),
                'cache_hit_tokens':sum(u.get('prompt_cache_hit_tokens',0) for u in usage),
                'wall_seconds':time.monotonic()-started,'api_cost_usd':'N/A',
                'cost_upper_cny':sum(r.get('charged_upper_cny',r['reserve_cny']) for r in provider.records),
                'patch_bytes':len(patch),'patch_sha256':hashlib.sha256(patch).hexdigest(),
                'patch_valid':bool(patch) or not bool(failure),'changed_files':changed,'official_resolved':None}
            save(dest/'run.json',record)
            aggregate()
            print('AGENT_DONE',json.dumps(record),flush=True)
        # Provider/protocol/security/runtime errors stop new paid execution, never retry.
        if failure or json.loads(ledger.read_text()).get('halt'):break
    for row in public_tasks():
        dest=OUT/'tasks'/row['instance_id']
        if (dest/'run.json').exists():continue
        dest.mkdir(parents=True,exist_ok=True)
        save(dest/'run.json',{'instance_id':row['instance_id'],'status':'NOT_RUN_STOP_CONDITION',
            'attempt':0,'model_calls':0,'tool_calls':0,'input_tokens':0,'output_tokens':0,
            'wall_seconds':0,'api_cost_usd':'N/A','cost_upper_cny':0,'official_resolved':None})
        (dest/'patch.diff').write_bytes(b'');(dest/'errors.log').write_text('Previous task triggered a pilot stop condition; this task was not attempted.\n')
    aggregate()

def aggregate():
    predictions=[];usage=[]
    for row in public_tasks():
        dest=OUT/'tasks'/row['instance_id']
        if not (dest/'run.json').exists():continue
        record=json.loads((dest/'run.json').read_text());usage.append(record)
        predictions.append({'instance_id':row['instance_id'],'model_name_or_path':LABEL,
            'model_patch':(dest/'patch.diff').read_text()})
    (OUT/'results').mkdir(exist_ok=True)
    (OUT/'results/dev_predictions.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in predictions))
    fields=['instance_id','input_tokens','output_tokens','model_calls','tool_calls','wall_seconds','api_cost_usd','cost_upper_cny','status']
    with (OUT/'results/usage.csv').open('w',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(usage)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['preflight','agent','aggregate']);args=parser.parse_args()
    if args.phase=='preflight':asyncio.run(preflight())
    elif args.phase=='agent':asyncio.run(agent(json.loads(sys.stdin.read())['api_key']))
    else:aggregate()
