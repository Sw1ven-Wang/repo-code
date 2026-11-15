#!/usr/bin/env python3
"""Optional paid smoke. Never run by offline CI or silently substituted by FakeProvider."""
import argparse
import asyncio
import json
from pathlib import Path
from repo_code.service import RepoCode
from repo_code.providers import OpenAIProvider,AnthropicProvider
from repo_code.models import Contract,Budget

async def run(args):
    options={'base_url':args.base_url}
    if args.api_key_env:options['api_key_env']=args.api_key_env
    if args.reasoning_effort:
        if args.provider!='openai':raise ValueError('reasoning effort requires the OpenAI-compatible provider')
        options['reasoning_effort']=args.reasoning_effort
    provider=(OpenAIProvider if args.provider=='openai' else AnthropicProvider)(args.model,**options)
    try:
        app=RepoCode(args.state,provider,args.sandbox,args.image,context_compaction=args.context_compaction)
        workspace=Path(args.workspace)
        if workspace.exists() and any(workspace.iterdir()):raise ValueError('Use a new/empty smoke workspace')
        workspace.mkdir(parents=True,exist_ok=True)
        contract=Contract.model_validate_json((Path(__file__).resolve().parents[1]/'examples/answer.contract.json').read_bytes())
        task=app.new_task('Create main.py with answer() returning integer 42. Run it and request verification.',workspace,contract,Budget(model_calls=8,wall_seconds=120),True,True)
        result=await app.execute(task['id'],expose_control=True)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result['state']=='SUCCEEDED' else 1
    finally:
        await provider.client.close()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--i-understand-api-costs',action='store_true',required=True)
    p.add_argument('--provider',choices=['openai','anthropic'],required=True);p.add_argument('--model',required=True)
    p.add_argument('--base-url');p.add_argument('--state',type=Path,required=True);p.add_argument('--workspace',type=Path,required=True)
    p.add_argument('--api-key-env');p.add_argument('--reasoning-effort',choices=['none','minimal','low','medium','high','xhigh'])
    p.add_argument('--context-compaction',choices=['legacy','live-experimental'],default='legacy')
    p.add_argument('--sandbox',choices=['native','docker'],required=True);p.add_argument('--image')
    return asyncio.run(run(p.parse_args()))
if __name__=='__main__':raise SystemExit(main())
