"""Offline validation of live test infrastructure; these are not live API trials."""
import json
import os
from pathlib import Path
import subprocess
import sys

from repo_code.models import Contract
from repo_code.acceptance import validate_contract

ROOT=Path(__file__).resolve().parents[1]


def test_five_live_tasks_have_frozen_external_contracts():
    tasks=json.loads((ROOT/'examples/live_benchmark_tasks.json').read_text())
    assert len(tasks)==5 and len({t['id'] for t in tasks})==5
    for task in tasks:
        contract=Contract.model_validate(task['contract'])
        assert validate_contract(contract,require_frozen=True)
        assert contract.required_checks==['behavior','syntax']
        assert len(contract.checks[0].definition['cases'])>=3
        assert task['budget']['model_calls']<=16


def test_preflight_without_credentials_does_not_run_fake_or_live(tmp_path):
    env={k:v for k,v in os.environ.items() if k not in {'OPENAI_API_KEY','ANTHROPIC_API_KEY'}}
    env['PYTHONPATH']=str(ROOT/'src')
    report=tmp_path/'preflight.json';state=tmp_path/'state'
    command=[sys.executable,str(ROOT/'scripts/live_benchmark.py'),'--provider','openai','--model','operator-specified-model',
             '--sandbox','native','--state',str(state),'--report',str(report),'--preflight-only']
    result=subprocess.run(command,env=env,text=True,capture_output=True,timeout=30)
    assert result.returncode==2,result.stdout+result.stderr
    record=json.loads(report.read_text())
    assert record['status']=='BLOCKED' and record['provider_contacted'] is False
    assert record['completed_trials']==0 and record['trials']==[] and record['dataset_tasks']==5
    assert not state.exists()

import pytest
from repo_code.models import Budget
from repo_code.util import atomic_write

REFERENCE_SOLUTIONS={
    'bugfix-average':{'stats.py':'def average(values):\n    return sum(values)/len(values) if values else None\n'},
    'greenfield-csv':{'report.py':'''import csv,io
def summarize(csv_text):
    rows=list(csv.DictReader(io.StringIO(csv_text)))
    return {"rows":len(rows),"total":sum(float(r["amount"]) for r in rows)}
'''},
    'multi-file-cart':{'pricing.py':'def line_total(item):\n    return item["cents"]*item["quantity"]\n',
       'checkout.py':'from pricing import line_total\ndef total(items,discount_percent=0):\n    return sum(line_total(x) for x in items)*(100-discount_percent)//100\n'},
    'async-batching':{'batches.py':'async def collect(items,size):\n    return [items[i:i+size] for i in range(0,len(items),size)]\n'},
    'nested-config':{'config.py':'''def merge(base,override):
    result=dict(base)
    for key,value in override.items():
        result[key]=merge(result[key],value) if isinstance(value,dict) and isinstance(result.get(key),dict) else value
    return result
'''},
}


@pytest.mark.asyncio
@pytest.mark.parametrize('task_id',list(REFERENCE_SOLUTIONS))
async def test_live_task_oracles_accept_reference_implementations(app,tmp_path,task_id):
    """Validate fixture oracles with actual isolated execution, not a live model."""
    task=next(t for t in json.loads((ROOT/'examples/live_benchmark_tasks.json').read_text()) if t['id']==task_id)
    workspace=tmp_path/'oracle-source';workspace.mkdir()
    for name,content in {**task['files'],**REFERENCE_SOLUTIONS[task_id]}.items():
        atomic_write(workspace/name,content.encode())
    contract=Contract.model_validate(task['contract'])
    run=app.new_task(task['goal'],workspace,contract,Budget(model_calls=1))
    intent=app.runtime.prepare(run['id'])
    decision=await app.grader.evaluate(intent,contract)
    assert decision.verified,decision.reason
    assert app.runtime.commit(intent,decision)['status']=='COMMITTED'
