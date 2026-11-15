"""Trusted official Harness driver. Never imported by model-facing task code."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
import docker
from swebench.harness.test_spec.test_spec import make_test_spec

ROOT=Path(os.environ.get('REPO_CODE_EVAL_ROOT','/eval')); OUT=ROOT/'pilot2'

def write(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2)+'\n')

def images():
    rows=json.loads((ROOT/'trusted/dev_rows.json').read_text())
    client=docker.from_env(timeout=1800); records={}
    for row in rows:
        spec=make_test_spec(row,namespace='swebench')
        print('IMAGE',row['instance_id'],spec.instance_image_key,flush=True)
        try: image=client.images.get(spec.instance_image_key)
        except docker.errors.ImageNotFound: image=client.images.pull(spec.instance_image_key)
        records[row['instance_id']]={'name':spec.instance_image_key,'id':image.id,
            'repo_digests':image.attrs.get('RepoDigests',[]),'size':image.attrs['Size']}
        write(OUT/'config/images.json',records)
        print('READY',row['instance_id'],image.id,flush=True)
    write(OUT/'config/dependencies.json',dict(sorted((d.metadata['Name'],d.version) for d in importlib.metadata.distributions() if d.metadata['Name'])))

def grade(kind, predictions=None):
    ids=(OUT/'frozen/dev_pilot_ids.txt').read_text().split()
    assert len(ids)==2
    if predictions is None: predictions=ROOT/'trusted'/f'{kind}_predictions.jsonl'
    run_id='rc_devpilot_'+kind+'_'+datetime.now(timezone.utc).strftime('%Y%m%d')+'_'+uuid.uuid4().hex[:10]
    dest=OUT/'official-logs'/run_id
    dest.mkdir(parents=True)
    cmd=[sys.executable,'-m','swebench.harness.run_evaluation',
        '--dataset_name','princeton-nlp/SWE-bench_Lite','--split','dev',
        '--predictions_path',str(predictions),'--instance_ids',*ids,
        '--max_workers','1','--cache_level','base','--timeout','1800',
        '--run_id',run_id,'--report_dir',str(dest)]
    environment=OUT/'config/harness_environment.json'
    if environment.exists():cmd.extend(['--namespace',json.loads(environment.read_text())['namespace']])
    env={**os.environ,'HF_HUB_OFFLINE':'1','HF_DATASETS_OFFLINE':'1'}
    env.pop('DEEPSEEK_API_KEY_REPO',None)
    write(dest/'command.json',{'argv':cmd,'dataset_revision':json.loads((OUT/'frozen/selection_manifest.json').read_text())['dataset_revision'],'purpose':kind})
    print('OFFICIAL_START',run_id,flush=True)
    with (dest/'harness.stdout.log').open('w') as log:
        rc=subprocess.run(cmd,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT).returncode
    return collect(kind,run_id,rc)

def collect(kind,run_id,rc=0):
    ids=(OUT/'frozen/dev_pilot_ids.txt').read_text().split()
    dest=OUT/'official-logs'/run_id
    logs=ROOT/'logs/run_evaluation'/run_id
    if logs.exists(): shutil.copytree(logs,dest/'per-instance',dirs_exist_ok=True)
    reports=list(dest.glob('*.json'))+list(ROOT.glob('*.'+run_id+'.json'))
    report=next((p for p in reports if p.name!='command.json' and 'resolved_ids' in json.loads(p.read_text())),None)
    if report is None:
        raise RuntimeError('OFFICIAL_REPORT_MISSING:'+run_id+':'+str(rc))
    result=json.loads(report.read_text()); write(dest/'results.json',result)
    write(OUT/'config'/f'{kind}_official.json',{'run_id':run_id,'exit_code':rc,'results':str(dest/'results.json')})
    print('OFFICIAL_DONE',json.dumps({'run_id':run_id,'rc':rc,'resolved':result.get('resolved_ids'),
        'errors':result.get('error_ids'),'empty':result.get('empty_patch_ids'),'unresolved':result.get('unresolved_ids')}),flush=True)
    if rc or result.get('error_ids'): raise RuntimeError('BLOCKED_ENV:'+run_id)
    if kind=='gold' and set(result['resolved_ids'])!=set(ids): raise RuntimeError('GOLD_CONTROL_FAILED:'+run_id)
    if kind in ('empty','baseline') and result['resolved_ids']: raise RuntimeError('NEGATIVE_CONTROL_FAILED:'+run_id)
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['images','gold','empty','baseline','agent','collect']);p.add_argument('--kind');p.add_argument('--run-id')
    a=p.parse_args()
    if a.phase=='collect':return collect(a.kind,a.run_id)
    if a.phase=='images': return images()
    if a.phase=='baseline':
        ids=(OUT/'frozen/dev_pilot_ids.txt').read_text().split()
        patch='diff --git a/_repo_code_negative_control.txt b/_repo_code_negative_control.txt\nnew file mode 100644\n--- /dev/null\n+++ b/_repo_code_negative_control.txt\n@@ -0,0 +1 @@\n+Baseline control; no implementation changes.\n'
        (ROOT/'trusted/baseline_predictions.jsonl').write_text(''.join(json.dumps({'instance_id':i,'model_name_or_path':'CONTROL_baseline','model_patch':patch})+'\n' for i in ids))
    grade(a.phase,OUT/'results/dev_predictions.jsonl' if a.phase=='agent' else None)

if __name__=='__main__':main()
