#!/usr/bin/env python3
"""Opt-in real relay tests without executing model-generated programs.

Uses synthetic text and a temporary project only. A diagnostic L0 run is not
trusted sandbox acceptance. Credentials stay in the selected environment variable.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from datetime import datetime, timezone
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import urlsplit

from repo_code.models import Budget
from repo_code.providers import OpenAIProvider
from repo_code.service import RepoCode
from repo_code.tool_models import definitions
from repo_code.util import RepoError, atomic_write


def require(condition, code):
    if not condition:raise RepoError(code)


def preflight(args):
    endpoint=args.base_url or os.getenv('OPENAI_BASE_URL')
    try:
        url=urlsplit(endpoint or '')
        safe=bool(url.scheme in {'http','https'} and url.hostname and url.port != 0
                  and not (url.username or url.password or url.query or url.fragment))
    except ValueError:safe=False
    checks={'sdk_installed':importlib.util.find_spec('openai') is not None,
            'credential_configured':bool(os.getenv(args.api_key_env)),
            'explicit_model':bool(args.model),'endpoint_safe_to_record':safe}
    return {'kind':'LIVE_API_INTEGRATION','started_at':datetime.now(timezone.utc).isoformat(),
            'status':'READY' if all(checks.values()) else 'BLOCKED','checks':checks,
            'base_url':endpoint if safe else None,'model':args.model,
            'reasoning_effort':args.reasoning_effort,'credential_env_name':args.api_key_env,
            'provider_contacted':False,'adapter_calls':0,'tests':[],
            'trusted_sandbox_acceptance':'NOT_RUN','usage':[],
            'scope':'Streaming, full project tool schemas, tool-result round trip, optional L0 file-only diagnostic; no benchmark or RSI claim.'}


class RecordedProvider:
    """Count attempted adapter calls and usage; never retain credentials or prompts."""
    def __init__(self, inner, report, save_report=None):
        self.inner,self.report,self.save_report=inner,report,save_report
        self.model,self.base_url=inner.model,inner.base_url
        self.reasoning_effort=inner.reasoning_effort

    async def complete(self, *args, **kwargs):
        self.report['adapter_calls']+=1
        self.report['provider_contacted']=True
        self.report['status']='RUNNING'
        if self.save_report:self.save_report()
        result=await self.inner.complete(*args, **kwargs)
        self.report['usage'].append(result.usage)
        if self.save_report:self.save_report()
        return result


async def streaming_text(provider):
    marker='API_OK_'+secrets.token_hex(6)+'_中文'
    deltas=[]
    reply=await provider.complete('Follow the exact output instruction. No explanation.',
        [{'role':'user','content':'Reply with exactly this text: '+marker}],[],deltas.append)
    require(reply.text.strip()==marker and not reply.tool_calls,'TEXT_MISMATCH')
    require(bool(deltas) and ''.join(deltas)==reply.text,'STREAM_CALLBACK_MISMATCH')
    require(reply.stop_reason=='stop','TEXT_STOP_REASON')
    return {'stream_chunks':len(deltas),'unicode_preserved':True,
            'usage_available':bool(reply.usage),'stop_reason':reply.stop_reason}


async def tool_round_trip(provider):
    messages=[{'role':'user','content':'Call the read tool exactly once with path="fixture.txt". '
               'Do not guess the file contents. After receiving the tool result, reply with its marker only.'}]
    reply=await provider.complete('Use the requested tool. Return no other tool calls.',messages,definitions())
    require(len(reply.tool_calls)==1,'EXPECTED_SINGLE_TOOL_CALL')
    call=reply.tool_calls[0]
    require(call.name=='read' and call.arguments.get('path')=='fixture.txt','TOOL_ARGUMENT_MISMATCH')
    require(reply.stop_reason=='tool_calls','TOOL_STOP_REASON')
    marker='TOOL_OK_'+secrets.token_hex(10)+'_回传'
    messages.extend([{'role':'assistant','content':reply.text,
                      'tool_calls':[x.model_dump() for x in reply.tool_calls]},
                     {'role':'tool','tool_call_id':call.id,'content':json.dumps({'marker':marker},ensure_ascii=False)}])
    deltas=[]
    final=await provider.complete('Reply with the marker from the tool result only.',messages,[],deltas.append)
    require(final.text.strip()==marker and not final.tool_calls,'TOOL_ROUND_TRIP_MISMATCH')
    require(''.join(deltas)==final.text and bool(deltas),'TOOL_RESULT_STREAM_MISMATCH')
    return {'schema_count':len(definitions()),'tool_name':call.name,'arguments_valid':True,
            'tool_result_consumed':True,'unicode_preserved':True,'usage_available':bool(final.usage)}


async def l0_file_diagnostic(provider):
    # Generated code is parsed only, never imported or executed on the Host.
    with tempfile.TemporaryDirectory(prefix='repo-code-api-') as temp:
        root=Path(temp);source=root/'source';source.mkdir()
        original='def answer():\n    return 0\n'
        (source/'main.py').write_text(original,encoding='utf-8')
        app=RepoCode(root/'state',provider,sandbox='local')
        task=app.new_task('Read main.py, then edit answer() so it returns integer 42. '
            'Use read and edit tools. Do not run any process, delegate, or call MCP. '
            'After editing, finish with a short final message. This is a file-only diagnostic.',
            source,budget=Budget(model_calls=5,tool_calls=8,wall_seconds=180,child_calls=0),
            authorize_write=True,authorize_process=False)
        result=await app.execute(task['id'])
        events=app.store.events(task['id'],trusted=True,limit=10000)
        operations=[e['data'] for e in events if e['type']=='tool.operation_finished']
        tools=[e['tool_name'] for e in operations]
        # Only this disposable synthetic project's evidence is recorded.
        provider.report['l0_diagnostic']={'run_state':result['state'],'reason':result['reason'],
            'tools':tools,'tool_failures':[{'tool':e['tool_name'],'code':e['result'].get('code')}
                 for e in operations if e['result'].get('is_error')],
            'generated_source':(Path(result['workspace'])/'main.py').read_text(encoding='utf-8'),
            'model_calls':result['budget_used']['model_calls']}
        if provider.save_report:provider.save_report()
        require(result['state']=='PARTIAL' and result['delivery'] is None,'DIAGNOSTIC_STATE_MISMATCH')
        require(result['reason']=='EXPLORATION_ONLY: no frozen external acceptance','DIAGNOSTIC_REASON_MISMATCH')
        text=(Path(result['workspace'])/'main.py').read_text(encoding='utf-8')
        tree=ast.parse(text)
        require(len(tree.body)==1 and isinstance(tree.body[0],ast.FunctionDef),'GENERATED_SOURCE_SHAPE')
        fn=tree.body[0]
        require(fn.name=='answer' and len(fn.body)==1 and isinstance(fn.body[0],ast.Return),'GENERATED_FUNCTION_SHAPE')
        value=fn.body[0].value
        require(isinstance(value,ast.Constant) and type(value.value) is int and value.value==42,'GENERATED_RETURN_VALUE')
        require((source/'main.py').read_text(encoding='utf-8')==original,'ORIGIN_MODIFIED')
        require('read' in tools and 'edit' in tools,'EXPECTED_L0_FILE_TOOLS')
        require(not app.store.values('lease'),'UNEXPECTED_PROCESS_EXECUTION')
        return {'run_state':result['state'],'reason':result['reason'],'tools':tools,
                'model_calls':result['budget_used']['model_calls'],'usage':result['usage'],
                'source_ast_checked':True,'origin_unchanged':True,'delivery_verified':False,
                'generated_program_executed':False,'sandbox':'local (explicit file-only diagnostic)'}


def save(args,report):
    atomic_write(args.report,json.dumps(report,ensure_ascii=False,indent=2).encode('utf-8'))


async def execute(args,report):
    inner=OpenAIProvider(args.model,base_url=report['base_url'],api_key_env=args.api_key_env,
                         reasoning_effort=args.reasoning_effort,timeout=60)
    provider=RecordedProvider(inner,report,lambda:save(args,report))
    report['sdk_version']=importlib.metadata.version('openai')
    try:
        cases=[('streaming_text',streaming_text),('tool_round_trip',tool_round_trip)]
        if args.agent_diagnostic:cases.append(('l0_file_diagnostic',l0_file_diagnostic))
        if args.case:
            available={'streaming_text':streaming_text,'tool_round_trip':tool_round_trip,
                       'l0_file_diagnostic':l0_file_diagnostic}
            cases=[(name,available[name]) for name in dict.fromkeys(args.case)]
        for name,case in cases:
            started=time.monotonic()
            try:
                details=await asyncio.wait_for(case(provider),timeout=210)
                record={'name':name,'status':'PASSED','details':details}
            except Exception as exc:
                # Do not serialize remote errors, HTTP bodies, headers or tracebacks.
                record={'name':name,'status':'FAILED','error_code':getattr(exc,'code',type(exc).__name__)}
                cause=exc.__context__
                if cause is not None:
                    record['upstream_error_type']=type(cause).__name__
                    status=getattr(cause,'status_code',None)
                    if isinstance(status,int):record['http_status']=status
            record['wall_seconds']=round(time.monotonic()-started,3)
            report['tests'].append(record)
            report['status']='RUNNING'
            save(args,report)
            print(json.dumps(record,ensure_ascii=False),flush=True)
            if record['status']=='FAILED':break
    finally:
        await inner.client.close()
    report['status']='PASSED' if all(t['status']=='PASSED' for t in report['tests']) else 'FAILED'
    report['completed_at']=datetime.now(timezone.utc).isoformat()
    save(args,report)
    return 0 if report['status']=='PASSED' else 1


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',required=True)
    parser.add_argument('--base-url')
    parser.add_argument('--api-key-env',default='OPENAI_API_KEY')
    parser.add_argument('--reasoning-effort',choices=['none','minimal','low','medium','high','xhigh'])
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--live',action='store_true',help='Explicitly allow paid API calls')
    parser.add_argument('--agent-diagnostic',action='store_true',help='Also exercise L0 read/edit in a disposable project')
    parser.add_argument('--case',action='append',choices=['streaming_text','tool_round_trip','l0_file_diagnostic'],
                        help='Run only selected cases (repeatable); useful for targeted retests')
    args=parser.parse_args(argv)
    report=preflight(args)
    save(args,report)
    if report['status']=='BLOCKED' or not args.live:
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 2 if report['status']=='BLOCKED' else 0
    try:return asyncio.run(execute(args,report))
    except Exception as exc:
        report.update(status='FAILED',error_code=getattr(exc,'code',type(exc).__name__))
        save(args,report)
        return 1


if __name__=='__main__':raise SystemExit(main())
