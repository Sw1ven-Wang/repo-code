"""Three genuine learning tasks, frozen knowledge, three held-out transfer pairs."""
import argparse
import asyncio
import json
from pathlib import Path
import shutil
import time
from evaluate_memory_v2 import provider,write
from repo_code.memory.models import MemoryProfile
from repo_code.memory.encoders import LocalEncoder
from repo_code.memory.working_state import semantic_message
from repo_code.service import RepoCode
from repo_code.models import Budget,Contract,Check,Requirement
from repo_code.acceptance import freeze_contract
from repo_code.util import digest,atomic_write
from repo_code.knowledge import project_scope

ROOT=Path(__file__).resolve().parents[1]
SAMPLES=[
    {'id':'money','config':{'rounding':'ROUND_HALF_UP','currency':'CNY'},
     'goal':'Implement refunds(rows) in transfer.py: each row is a decimal amount string; convert each refund separately to integer cents using the project rounding setting, then return their sum. Reject negative and nonfinite amounts with ValueError. Read current source before editing; add and run tests.',
     'check':"from transfer import refunds\nassert refunds(['1.005','2.675'])==369\nassert refunds([])==0\nfor x in ['NaN','-1']:\n try:refunds([x])\n except ValueError:pass\n else:raise AssertionError(x)"},
    {'id':'delimited','config':{'delimiter':';','encoding':'utf-8-sig'},
     'goal':'Implement totals(text) in transfer.py: rows have no header and contain item plus integer quantity separated by the project delimiter. Sum repeated items, ignore blank lines, and handle a leading Unicode BOM. Preserve identifier case. Add and run tests.',
     'check':"from transfer import totals\nassert totals('\\ufeffa;2\\nb;3\\na;4\\n')=={'a':6,'b':3}\nassert totals('\\n')=={}\nassert totals('A;1\\na;2')=={'A':1,'a':2}"},
    {'id':'timezone','config':{'timezone':'UTC','precision':'seconds'},
     'goal':'Implement normalize(values) in transfer.py: accept a list of timezone-aware ISO timestamps, including Z and positive or negative offsets; return timestamps in the project timezone as YYYY-MM-DDTHH:MM:SSZ. Reject naive timestamps with ValueError. Add and run tests.',
     'check':"from transfer import normalize\nassert normalize(['2026-01-02T08:00:00+08:00','2026-01-01T19:00:00-05:00'])==['2026-01-02T00:00:00Z']*2\nassert normalize([])==[]\ntry:normalize(['2026-01-01T00:00:00'])\nexcept ValueError:pass\nelse:raise AssertionError('naive')"}]


def contract(goal,code):
    return freeze_contract(Contract(requirements=[Requirement(id='R',description=goal)],checks=[Check(id='check',kind='cli',definition={'argv':['python','-c',code+"\nprint('PASS')"],'stdout':'PASS\n'})],required_checks=['check'],coverage={'R':['check']}),'memory-transfer-frozen')


async def execute(app,run,folder):
    started=time.monotonic();error=None
    try:result=await app.execute(run['id'])
    except Exception as exc:error=getattr(exc,'code',type(exc).__name__);result=app.agent.report(run['id'])
    row={'run_id':run['id'],'state':result['state'],'reason':result.get('reason'),'verified':result['state']=='SUCCEEDED',
         'seconds':time.monotonic()-started,'error':error,'result':result}
    write(folder/'result.json',row)
    write(folder/'messages-visible.json',[semantic_message(m) for m in app.store.messages(run['id'])])
    return row


async def main_async(args):
    args.output.mkdir(parents=True,exist_ok=False)
    profile=MemoryProfile.model_validate(json.loads(args.profile.read_text())).model_copy(update={'auto_activate_observations':True,'semantic_consolidation':True})
    image=json.loads((ROOT/'examples/sandbox/validation-config.json').read_text())['image']
    encoder=LocalEncoder(profile.encoder);p=provider(args.output);rows=[]
    write(args.output/'frozen-protocol.json',{'samples':SAMPLES,'learning_tasks':3,'transfer_pairs':3,'profile':profile.model_dump(),
        'learning_domain':'DEV','transfer_domain':'FINAL','transfer_writeback':False,'scope_mapping':'same physical source directory within each project',
        'same_model_and_budget':True,'negative_results_retained':True})
    try:
        for sample in SAMPLES:
            folder=args.output/sample['id'];source=folder/'source';source.mkdir(parents=True)
            atomic_write(source/'project.json',json.dumps(sample['config']).encode())
            atomic_write(source/'learn.py',b'def project_settings():\n    raise NotImplementedError\n')
            app=RepoCode(folder/'host',p,'docker',image,memory_profile=profile,memory_encoder=encoder)
            scope=project_scope(source);empty=app.memory.pin(scope)
            goal='Read project.json and implement learn.project_settings() to return the decoded project configuration dictionary. Keep the file unchanged. Add and run tests.'
            oracle="from learn import project_settings\nassert project_settings()=="+repr(sample['config'])
            run=app.new_task(goal,source,contract(goal,oracle),Budget(model_calls=12,tool_calls=30,wall_seconds=300),True,True)
            learning=await execute(app,run,folder/'learning')
            frozen=app.memory.pin(scope);versions=len(app.memory.repo.values('memory_versions'))
            write(folder/'frozen-knowledge.json',{'empty':empty,'learned':frozen,'versions':versions,
                 'entries':app.store.read_json(frozen[0])['items'],'learning_verified':learning['verified']})
            atomic_write(source/'transfer.py',b'# Implement the requested transfer interface.\n')
            for arm,pin in [('without_added_memory',empty),('with_consolidated_memory',frozen)]:
                run=app.new_task(sample['goal'],source,contract(sample['goal'],sample['check']),Budget(model_calls=12,tool_calls=30,wall_seconds=300),True,True,
                                 visibility='FINAL',knowledge_pin=pin)
                result=await execute(app,run,folder/arm)
                row={'sample':sample['id'],'arm':arm,**{k:v for k,v in result.items() if k!='result'},'knowledge_unchanged':len(app.memory.repo.values('memory_versions'))==versions}
                rows.append(row);write(args.output/'summary.json',rows)
            app.sandbox.stop_run(run['id'])
    finally:encoder.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--profile',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    asyncio.run(main_async(p.parse_args()))


if __name__=='__main__':main()
