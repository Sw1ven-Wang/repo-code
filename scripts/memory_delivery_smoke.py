"""Small paid DeepSeek integration check, including post-task auxiliary usage."""
import argparse
import asyncio
import json
from pathlib import Path
from evaluate_memory_v2 import provider,write
from repo_code.memory.models import MemoryProfile
from repo_code.models import Contract,Check,Requirement,Budget
from repo_code.acceptance import freeze_contract
from repo_code.service import RepoCode
from repo_code.util import digest

ROOT=Path(__file__).resolve().parents[1]


async def main_async(args):
    out=args.output;out.mkdir(parents=True,exist_ok=False)
    src=out/'input';src.mkdir();(src/'main.py').write_text('def answer():\n    return 0\n');(src/'project.json').write_text('{"base":35,"offset":7}\n')
    profile=MemoryProfile.model_validate(json.loads(args.profile.read_text())).model_copy(update={'auto_activate_observations':True})
    image=json.loads((ROOT/'examples/sandbox/validation-config.json').read_text())['image'];p=provider(out)
    app=RepoCode(out/'host',p,'docker',image,memory_profile=profile)
    goal='Read project.json and main.py. Repair main.answer() so it returns base + offset using the project configuration. Preserve project.json. Add and run a focused test, then finish.'
    c=freeze_contract(Contract(requirements=[Requirement(id='answer',description=goal)],checks=[Check(id='behavior',kind='function',definition={'module':'main','function':'answer','expected':42})],required_checks=['behavior'],coverage={'answer':['behavior']},protected_paths=['project.json']),'memory-final-integration')
    run=app.new_task(goal,src,c,Budget(model_calls=12,tool_calls=30,wall_seconds=300),True,True)
    try:
        result=await app.execute(run['id']);write(out/'result.json',result)
        rows=app.memory.repo.values('context_requests')
        reconstruction=all(row['request_ref']==digest(app.store.read_json(row['request_ref'])) for row in rows)
        usage=all(row['usage_status']=='REPORTED' for row in rows)
        old=run['data']['manifest']['knowledge_snapshot_digest'];new=app.new_task('Inspect frozen consolidated project facts',src)
        hits,trace=await app.memory.search(new['id'],'project.json base offset')
        app.runtime.end(new['id'],'PARTIAL','read-only knowledge integration probe')
        accounted=sum(row['calls'] for row in result['memory_cost']['by_purpose'].values())
        checks={'delivery_succeeded':result['state']=='SUCCEEDED','request_reconstruction':reconstruction,'request_usage_reported':usage,
            'current_pin_unchanged':app.runtime.run(run['id'])['data']['manifest']['knowledge_snapshot_digest']==old,
            'next_run_has_consolidated_facts':any(h['kind']=='FACT' and h['applicability']=='TRUE' for h in hits),
            'all_paid_calls_accounted':accounted==len(p.calls),'usage':result['memory_cost'],'requests':len(rows),'paid_calls':len(p.calls)}
        write(out/'checks.json',checks);print(json.dumps(checks,indent=2))
        assert all(checks[k] for k in ['delivery_succeeded','request_reconstruction','request_usage_reported','current_pin_unchanged','next_run_has_consolidated_facts','all_paid_calls_accounted'])
    finally:app.sandbox.stop_run(run['id']);app.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--profile',type=Path,default=ROOT/'examples/memory/deepseek-experimental.json');p.add_argument('--output',type=Path,required=True)
    asyncio.run(main_async(p.parse_args()))


if __name__=='__main__':main()
