#!/usr/bin/env python3
"""Opt-in five-task live-model benchmark. No fake-provider fallback.

Preflight never contacts a provider. Executing tasks requires explicit consent,
a model ID, credentials in an environment variable and an isolated backend.
This is a small smoke/regression set, not a statistical superiority benchmark.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes.util
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

from repo_code.util import RepoError, atomic_write, digest


def preflight(args):
    if args.provider=='openai' and not args.base_url:args.base_url=os.getenv('OPENAI_BASE_URL')
    env_name=args.api_key_env or ('OPENAI_API_KEY' if args.provider=='openai' else 'ANTHROPIC_API_KEY')
    sdk='openai' if args.provider=='openai' else 'anthropic'
    checks={'sdk_installed':importlib.util.find_spec(sdk) is not None,
            'credential_configured':bool(os.getenv(env_name)),
            'explicit_model':bool(args.model)}
    if getattr(args,'reasoning_effort',None):checks['reasoning_effort_supported']=args.provider=='openai'
    if args.sandbox=='native':
        checks.update(linux=sys.platform.startswith('linux'),
                      root=hasattr(os,'geteuid') and os.geteuid()==0,
                      seccomp_library=bool(ctypes.util.find_library('seccomp')))
    else:
        checks.update(docker_executable=bool(shutil.which('docker')),
                      image_digest_pinned=bool(args.image and re.fullmatch(r'[^\s]+@sha256:[0-9a-fA-F]{64}',args.image)))
    # Do not leak embedded credentials/query parameters through reports or traces.
    if args.base_url:
        url=urlsplit(args.base_url)
        checks['endpoint_safe_to_record']=(url.scheme in {'https','http'} and bool(url.hostname)
                                         and not url.username and not url.password and not url.query and not url.fragment)
    blockers=[name for name,ok in checks.items() if not ok]
    return {'status':'READY_FOR_EXPLICIT_LIVE_RUN' if not blockers else 'BLOCKED',
            'checks':checks,'blockers':blockers,'provider':args.provider,'model':args.model,
            'reasoning_effort':getattr(args,'reasoning_effort',None),
            'credential_env_name':env_name,'provider_contacted':False,
            'sandbox_runtime_preflight':'not yet executed','completed_trials':0}


async def execute(args, status, tasks):
    from repo_code.evaluation import TrialRunner
    from repo_code.providers import OpenAIProvider, AnthropicProvider
    from repo_code.service import RepoCode
    cls=OpenAIProvider if args.provider=='openai' else AnthropicProvider
    if args.state.exists() and any(args.state.iterdir()):
        raise RepoError('LIVE_BENCHMARK_REQUIRES_FRESH_STATE')
    options={'base_url':args.base_url,'api_key_env':status['credential_env_name']}
    if getattr(args,'reasoning_effort',None):options['reasoning_effort']=args.reasoning_effort
    provider=cls(args.model,**options)
    try:
        app=RepoCode(args.state,provider,sandbox=args.sandbox,image=args.image,
                     context_compaction=args.context_compaction)
        if not app.sandbox.isolated:raise RepoError('ISOLATION_REQUIRED')
        app.sandbox.prepare()  # Fail closed; Docker availability is not presumed.
        runner=TrialRunner(app,lambda public:provider)
        release=app.releases.active()['digest'];results=[]
        cohort='live-smoke:'+digest(tasks)
        for task in tasks:
            result=await runner.run(task,release,cohort,hidden=False)
            results.append(result)
            # Persist every attempted task, including failures and unavailable usage.
            report={'status':'RUNNING','kind':'LIVE_MODEL_TASK_EVALUATION',
                    'provider':args.provider,'model':args.model,'dataset_digest':digest(tasks),
                    'reasoning_effort':getattr(args,'reasoning_effort',None),
                    'release_digest':release,'trials':results,'completed_trials':len(results),
                    'total_tasks':len(tasks),'provider_contacted':True,
                    'warning':'Small fixed task set; not an independent RSI generalization study.'}
            atomic_write(args.report,json.dumps(report,ensure_ascii=False,indent=2).encode())
        passed=sum(r['passed'] for r in results)
        report.update(status='COMPLETED',passed=passed,failed=len(results)-passed,
                      completion_rate=passed/len(tasks),all_passed=passed==len(tasks))
        atomic_write(args.report,json.dumps(report,ensure_ascii=False,indent=2).encode())
        return report,0 if passed==len(tasks) else 1
    finally:
        close=getattr(provider.client,'close',None)
        if close:
            result=close()
            if hasattr(result,'__await__'):await result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--provider',choices=['openai','anthropic'],required=True)
    p.add_argument('--model',required=True)
    p.add_argument('--api-key-env');p.add_argument('--base-url')
    p.add_argument('--reasoning-effort',choices=['none','minimal','low','medium','high','xhigh'])
    p.add_argument('--context-compaction',choices=['legacy','live-experimental'],default='legacy')
    p.add_argument('--sandbox',choices=['native','docker'],required=True);p.add_argument('--image')
    p.add_argument('--state',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    p.add_argument('--dataset',type=Path,default=Path(__file__).resolve().parents[1]/'examples'/'live_benchmark_tasks.json')
    p.add_argument('--preflight-only',action='store_true')
    p.add_argument('--i-understand-api-costs',action='store_true')
    args=p.parse_args(argv)
    tasks=json.loads(args.dataset.read_text(encoding='utf-8'))
    if not isinstance(tasks,list) or not tasks:p.error('dataset must be a nonempty task list')
    status=preflight(args);status.update(dataset_tasks=len(tasks),dataset_digest=digest(tasks),trials=[])
    args.report.parent.mkdir(parents=True,exist_ok=True)
    if args.preflight_only or status['blockers'] or not args.i_understand_api_costs:
        if not args.preflight_only and not args.i_understand_api_costs:
            status['blockers'].append('explicit_paid_api_consent_required');status['status']='BLOCKED'
        atomic_write(args.report,json.dumps(status,ensure_ascii=False,indent=2).encode())
        print(json.dumps(status,ensure_ascii=False,indent=2))
        return 2 if status['blockers'] else 0
    try:
        report,code=asyncio.run(execute(args,status,tasks))
    except Exception as exc:
        # Preserve task reports written before a later infrastructure failure.
        report=json.loads(args.report.read_text()) if args.report.exists() else status
        report.update(status='BLOCKED_OR_FAILED',error_code=getattr(exc,'code',type(exc).__name__))
        atomic_write(args.report,json.dumps(report,ensure_ascii=False,indent=2).encode());code=2
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return code


if __name__=='__main__':raise SystemExit(main())
