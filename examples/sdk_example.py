"""API-key-free SDK smoke: real files/processes, explicit scripted model."""
import argparse
import asyncio
import json
from pathlib import Path
from repo_code.service import RepoCode
from repo_code.models import Contract,Budget
from repo_code.providers import ScriptedProvider
from repo_code.delivery import Delivery

async def run(args):
    base=Path(__file__).resolve().parent
    args.workspace.mkdir(parents=True,exist_ok=True)
    if any(args.workspace.iterdir()):raise ValueError('Use an empty demonstration workspace')
    p=ScriptedProvider(json.loads((base/'answer.script.json').read_text(encoding='utf-8')))
    app=RepoCode(args.state,p,args.sandbox,args.image)
    contract=Contract.model_validate_json((base/'answer.contract.json').read_bytes())
    run=app.new_task('Create main.answer() returning 42; execute and verify.',args.workspace,contract,Budget(model_calls=8),True,True)
    report=await app.execute(run['id'],expose_control=True)
    if report['state']=='SUCCEEDED':Delivery(app).export(run['id'],args.output)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['state']=='SUCCEEDED' else 1

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state',type=Path,required=True);p.add_argument('--workspace',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--sandbox',choices=['native','docker'],required=True);p.add_argument('--image')
    raise SystemExit(asyncio.run(run(p.parse_args())))
