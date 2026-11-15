"""Validate reference implementations and reject incomplete inputs without model calls."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile

from create_capability_tasks import build
from repo_code.models import Contract
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.util import atomic_write

ROOT=Path(__file__).resolve().parents[1]


async def validate(args):
    config=json.loads((ROOT/'examples/sandbox/validation-config.json').read_text(encoding='utf-8'))
    tasks,references=build()
    if args.state.exists() and any(args.state.iterdir()):raise ValueError('Use a fresh oracle-validation state directory')
    app=RepoCode(args.state,ScriptedProvider(),sandbox='docker',image=config['image'])
    records=[]
    for task in tasks:
        for kind,files,wanted in [('reference',{**task['files'],**references[task['id']]},True),('initial',task['files'],False)]:
            with tempfile.TemporaryDirectory(dir=app.store.root) as temporary:
                work=Path(temporary)
                for path,text in files.items():atomic_write(work/path,text.encode())
                contract=Contract.model_validate(task['contract'])
                run=app.new_task('Offline oracle validation',work,contract)
                decision=await app.grader.evaluate(app.runtime.prepare(run['id']),contract)
                result={'task':task['id'],'kind':kind,'verified':decision.verified,'expected_verified':wanted,'correct':decision.verified is wanted}
                records.append(result)
                atomic_write(args.report,json.dumps(records,ensure_ascii=False,indent=2).encode())
                print(json.dumps(result,ensure_ascii=False),flush=True)
                if not result['correct']:return 1
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    return asyncio.run(validate(parser.parse_args()))


if __name__=='__main__':raise SystemExit(main())
