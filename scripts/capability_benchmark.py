#!/usr/bin/env python3
"""Explicit real-model capability trials with frozen external checks and artifacts.

No generated program executes on the Host. Keep each attempt in a fresh state
and output directory. Model messages contain only synthetic task workspaces.
"""
from __future__ import annotations
import argparse
import asyncio
import contextlib
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
import time

from repo_code.models import Contract,Budget
from repo_code.providers import OpenAIProvider
from repo_code.service import RepoCode
from repo_code.util import RepoError,atomic_write,digest,redact_data,redact
from repo_code.workspace import Workspace

ROOT=Path(__file__).resolve().parents[1]


def write(path,value):atomic_write(path,json.dumps(redact_data(value),ensure_ascii=False,indent=2).encode())


class RecordedProvider:
    def __init__(self,inner,report,save,log,wall_seconds):
        self.inner,self.report,self.save,self.log=inner,report,save,log
        self.wall_seconds=wall_seconds
        self.model,self.base_url=inner.model,inner.base_url
        self.reasoning_effort=inner.reasoning_effort
        self.task_id=None

    async def complete(self,*args,**kwargs):
        self.report['attempted_adapter_calls']+=1;self.save()
        number=self.report['attempted_adapter_calls'];started=time.monotonic()
        self.log({'event':'model.start','task':self.task_id,'call':number})
        try:
            try:
                reply=await asyncio.wait_for(self.inner.complete(*args,**kwargs),timeout=self.wall_seconds)
            except TimeoutError:
                raise RepoError('MODEL_REQUEST_WALL_TIMEOUT') from None
            self.report['usage_by_call'].append({'task':self.task_id,'call':number,'usage':reply.usage})
            self.log({'event':'model.complete','task':self.task_id,'call':number,
                      'tools':[t.name for t in reply.tool_calls],'seconds':round(time.monotonic()-started,2)})
            return reply
        except Exception as exc:
            error={'event':'model.failed','task':self.task_id,'call':number,'code':getattr(exc,'code',type(exc).__name__)}
            if exc.__context__ is not None:
                error['upstream_error_type']=type(exc.__context__).__name__
                status=getattr(exc.__context__,'status_code',None)
                if isinstance(status,int):error['http_status']=status
                body=getattr(exc.__context__,'body',None)
                if isinstance(body,(dict,str)):
                    details=body.get('error',body) if isinstance(body,dict) else body
                    message=str(details.get('message','')) if isinstance(details,dict) else str(details)
                    key=getattr(self.inner.client,'api_key','')
                    if key:message=message.replace(key,'[REDACTED]')
                    error['upstream_message']=redact(message)[:1000]
                    if isinstance(details,dict):error['upstream_code']=details.get('code')
            self.log(error)
            raise
        finally:self.save()


def observations(app,run_id):
    with contextlib.closing(app.store.connect()) as conn:
        rows=conn.execute('SELECT request,result,state FROM operations WHERE run_id=? ORDER BY created',(run_id,)).fetchall()
    operations=[]
    for row in rows:
        request=json.loads(row['request']);result=json.loads(row['result']) if row['result'] else {}
        operations.append({'tool':request['tool_name'],'arguments':request['arguments'],'state':row['state'],'result':result})
    return operations


async def trial(app,task,output):
    folder=output/task['id'];folder.mkdir(parents=True)
    original=folder/'input';original.mkdir()
    for path,text in task['files'].items():atomic_write(original/path,text.encode())
    contract=Contract.model_validate(task['contract'])
    write(folder/'frozen-contract.json',contract.model_dump(mode='json'))
    write(folder/'task.json',{k:task[k] for k in ['id','category','goal','budget']})
    before=Workspace(original,app.store).snapshot()
    run=app.new_task(task['goal'],original,contract,Budget.model_validate(task['budget']),
                     authorize_write=True,authorize_process=True)
    run_id=run['id'];started=time.monotonic()
    try:
        result=await app.execute(run_id)
    except Exception as exc:
        result=app.agent.report(run_id)
        result.update(infrastructure_error=getattr(exc,'code',type(exc).__name__))
    finally:
        stopped=app.sandbox.stop_run(run_id)
    operations=observations(app,run_id)
    invalid_calls=[json.loads(m['content']) for m in app.store.messages(run_id) if m.get('role')=='tool' and m.get('is_error') and json.loads(m['content']).get('code')=='INVALID_ARGUMENT']
    process=[op for op in operations if op['tool']=='process.run']
    pytest_runs=[op for op in process if any('pytest' in str(arg) for arg in op['arguments'].get('argv',[]))]
    reproduced=any(op['result'].get('exit_code')==1 for op in pytest_runs)
    tests_passed=any(op['result'].get('exit_code')==0 for op in pytest_runs)
    verified=bool(result['state']=='SUCCEEDED' and result['delivery'])
    workflow=bool(tests_passed and process)
    if task['category']=='testing_debugging':workflow &= reproduced
    unchanged=Workspace(original,app.store).snapshot()==before
    checks=[]
    with contextlib.closing(app.store.connect()) as conn:
        for row in conn.execute("SELECT data FROM objects WHERE kind='evidence'"):
            ev=json.loads(row[0])
            if ev['run_id']==run_id:
                checks.append({**ev,'observations':app.store.read_json(ev['report_digest'])})
    write(folder/'result.json',result);write(folder/'operations.json',operations)
    write(folder/'evidence.json',checks);write(folder/'messages.json',app.store.messages(run_id))
    shutil.copytree(result['workspace'],folder/'output',ignore=shutil.ignore_patterns('__pycache__','.pytest_cache'))
    record={'task_id':task['id'],'category':task['category'],'run_id':run_id,'state':result['state'],
            'reason':result.get('reason'),'passed':bool(verified and workflow and unchanged and stopped),
            'delivery_verified':verified,'workflow_passed':workflow,'original_unchanged':unchanged,
            'resources_stopped':stopped,'pytest_fail_reproduced':reproduced,'pytest_pass_observed':tests_passed,
            'model_calls':result['budget_used']['model_calls'],'tool_calls':result['budget_used']['tool_calls'],
            'process_calls':len(process),'tool_failures':sum(op['state']=='FAILED' for op in operations),
            'invalid_tool_calls':len(invalid_calls),
            'usage':result['usage'],'seconds':round(time.monotonic()-started,3),
            'artifact_directory':str(folder.resolve()),'manifest':result['manifest'],
            'infrastructure_error':result.get('infrastructure_error')}
    write(folder/'summary.json',record)
    return record


async def execute(args,config,tasks):
    for path in [args.state,args.output]:
        if path.exists() and any(path.iterdir()):raise RepoError('FRESH_DIRECTORY_REQUIRED',str(path))
    args.output.mkdir(parents=True,exist_ok=True)
    report={'kind':'LIVE_CAPABILITY_EVALUATION','started_at':datetime.now(timezone.utc).isoformat(),
            'status':'RUNNING','model':config['model'],'reasoning_effort':config['reasoning_effort'],
            'base_url':config['base_url'],'image':config['image'],'dataset_digest':digest(tasks),
            'attempted_adapter_calls':0,'usage_by_call':[],'trials':[],
            'request_wall_seconds':args.request_wall_seconds,'sdk_retries':0,
            'scope':'Five fixed Python tasks; not a general success-rate or RSI benchmark.'}
    save=lambda:write(args.output/'report.json',report)
    def log(item):
        item={'at':datetime.now(timezone.utc).isoformat(),**item}
        with (args.output/'progress.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(item,ensure_ascii=False)+'\n')
        print(json.dumps(item,ensure_ascii=False),flush=True)
    inner=OpenAIProvider(config['model'],base_url=config['base_url'],api_key_env=config['api_key_env'],
                         reasoning_effort=config['reasoning_effort'],timeout=90)
    inner.client.max_retries=0
    provider=RecordedProvider(inner,report,save,log,args.request_wall_seconds)
    try:
        app=RepoCode(args.state,provider,sandbox='docker',image=config['image'])
        for task in tasks:
            provider.task_id=task['id'];log({'event':'task.start','task':task['id']})
            record=await trial(app,task,args.output)
            report['trials'].append(record);save()
            log({'event':'task.complete','task':task['id'],'passed':record['passed'],'state':record['state'],
                 'verified':record['delivery_verified'],'workflow':record['workflow_passed']})
        report.update(status='COMPLETED',passed=sum(t['passed'] for t in report['trials']),
                      total=len(tasks),completed_at=datetime.now(timezone.utc).isoformat())
        save()
        return 0 if report['passed']==len(tasks) else 1
    except Exception as exc:
        report.update(status='FAILED',error_code=getattr(exc,'code',type(exc).__name__))
        save();raise
    finally:await inner.client.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=ROOT/'examples/sandbox/validation-config.json')
    p.add_argument('--dataset',type=Path,default=ROOT/'examples/capability_tasks.json')
    p.add_argument('--task',action='append',help='Select only these task IDs; repeatable')
    p.add_argument('--state',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--live',action='store_true',help='Explicitly authorize paid real-model calls')
    p.add_argument('--request-wall-seconds',type=float,default=180,help='Total wall limit per model request; SDK retries are disabled')
    args=p.parse_args(argv)
    if args.request_wall_seconds<=0:p.error('request wall seconds must be positive')
    config=json.loads(args.config.read_text(encoding='utf-8'))
    if not args.live:p.error('--live is required to execute real-model tasks')
    if not os.getenv(config['api_key_env']):p.error('API credential environment variable is missing')
    tasks=json.loads(args.dataset.read_text(encoding='utf-8'))
    if args.task:
        known={t['id'] for t in tasks}
        if set(args.task)-known:p.error('unknown task ID')
        tasks=[t for t in tasks if t['id'] in args.task]
    return asyncio.run(execute(args,config,tasks))


if __name__=='__main__':raise SystemExit(main())
