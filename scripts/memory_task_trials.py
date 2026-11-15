"""Four paired real coding tasks and separate three-pair transfer experiment."""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import time
from evaluate_memory_v2 import AuditProvider, provider, write
from repo_code.acceptance import freeze_contract
from repo_code.context import ContextEngine
from repo_code.models import Budget, Check, Contract, Requirement
from repo_code.memory.models import MemoryProfile
from repo_code.memory.encoders import LocalEncoder
from repo_code.memory.working_state import semantic_message
from repo_code.service import RepoCode
from repo_code.util import digest, atomic_write,RepoError

ROOT=Path(__file__).resolve().parents[1]


def tasks():
    common='Read the existing implementation and relevant logs, run the failing tests before editing, fix the implementation and add focused tests. Preserve public APIs. Run the full project tests and explain the outcome. '
    result=[]
    def add(name,goal,files,check):
        contract=freeze_contract(Contract(requirements=[Requirement(id='behavior',description=goal)],checks=[Check(id='external',kind='cli',definition={'argv':['python','-c',check+"\nprint('PASS')"],'stdout':'PASS\n'})],required_checks=['external'],coverage={'behavior':['external']}),'memory-experiment-frozen')
        result.append({'id':name,'goal':common+goal,'files':files,'contract':contract.model_dump(mode='json')})
    add('event-log-debug','Repair the event replay service: duplicate event IDs are ignored, failed batches roll back completely, account balances never become negative. Inspect incident.jsonl to explain the observed partial-commit bug. Values are integer cents; keep replay(events) -> {account:balance}.',{
        'replay/__init__.py':'from .service import replay\n',
        'replay/store.py':'class Store:\n    def __init__(self): self.balances={}; self.seen=set()\n    def apply(self,e):\n        self.balances[e["account"]]=self.balances.get(e["account"],0)+e["amount"]\n        self.seen.add(e["id"])\n',
        'replay/service.py':'from .store import Store\ndef replay(events):\n    s=Store()\n    for batch in events:\n        for e in batch:\n            s.apply(e)\n    return s.balances\n',
        'tests/test_replay.py':'from replay import replay\ndef test_dedup():\n    e={"id":"a","account":"x","amount":5}\n    assert replay([[e,e]])=={"x":5}\ndef test_rollback():\n    assert replay([[{"id":"a","account":"x","amount":5}],[{"id":"b","account":"x","amount":2},{"id":"c","account":"x","amount":-20}]])=={"x":5}\n',
        'incident.jsonl':'\n'.join(json.dumps({'batch':i,'event':f'e{i}','account':f'a{i%4}','attempted_delta':(-i if i%9==0 else i),'status':'ROLLBACK_REQUIRED' if i%9==0 else 'APPLIED','diagnostic':'a later debit in this batch failed; earlier deltas must not persist'}) for i in range(100))+'\n'},
        "from replay import replay\na={'id':'a','account':'x','amount':10}; b={'id':'b','account':'x','amount':5}; c={'id':'c','account':'x','amount':-20}\nassert replay([[a,a],[b,c],[b]])=={'x':15}\nassert replay([])=={}\nassert replay([[{'id':'z','account':'x','amount':-1}]])=={}")
    add('cross-module-csv','Implement the agreed CSV import interface across csvpipe modules. decode(bytes) handles UTF-8 BOM; parse(text) validates id and Decimal amount (ROUND_HALF_UP integer cents); latest(rows) retains highest revision per id, last wins ties; summarize(bytes) excludes cancelled latest records, returns sorted currency totals, invalid_rows counts every malformed row. Keep every named function and add CLI python -m csvpipe that reads stdin bytes and prints JSON.',{
        'csvpipe/__init__.py':'from .service import summarize\n','csvpipe/codec.py':'def decode(data): return data.decode("utf-8")\n',
        'csvpipe/parser.py':'import csv,io\ndef parse(text):\n    return list(csv.DictReader(io.StringIO(text)))\n',
        'csvpipe/revisions.py':'def latest(rows): return rows\n',
        'csvpipe/service.py':'from .codec import decode\nfrom .parser import parse\nfrom .revisions import latest\ndef summarize(data): return {"totals_cents":{},"invalid_rows":0}\n',
        'csvpipe/__main__.py':'import json,sys\nfrom .service import summarize\nprint(json.dumps(summarize(sys.stdin.buffer.read())))\n',
        'tests/test_import.py':'from csvpipe import summarize\ndef test_bom():\n    assert summarize("\\ufeffid,revision,amount,currency,status\\na,1,1.005,CNY,active\\n".encode())=={"totals_cents":{"CNY":101},"invalid_rows":0}\n',
        'README.md':'Public schema: id,revision,amount,currency,status. id nonempty; revision nonnegative integer; finite nonnegative decimal amount; currency CNY or USD; status active or cancelled. parse returns (valid_rows, invalid_count); latest accepts valid_rows and returns selected rows. summarize returns totals_cents and invalid_rows.\n'},
        "from csvpipe import summarize\ns='\\ufeffid,revision,amount,currency,status\\na,1,10.005,CNY,active\\na,2,99,CNY,cancelled\\nb,0,1.005,USD,active\\nb,0,2.345,USD,active\\nc,1,NaN,CNY,active\\nd,bad,1,CNY,active\\n'\nassert summarize(s.encode())=={'totals_cents':{'USD':235},'invalid_rows':2}")
    add('historical-cache-refactor','Fix TTL cache semantics and refactor across key, cache and service modules. Keys preserve type distinctions (True, 1 and "1" differ); nested dict order is irrelevant; expiry at now >= deadline; ttl=0 disables storage; values and inputs are defensively copied. Preserve get(key,now), set(key,value,ttl,now), get_or_compute(cache,key,fn,ttl,now). Read earlier-decisions.md, and revisit its non-string key requirement after implementing expiration.',{
        'cachelib/__init__.py':'from .cache import Cache\nfrom .service import get_or_compute\n',
        'cachelib/keys.py':'import json\ndef key(value): return json.dumps(value,sort_keys=True)\n',
        'cachelib/cache.py':'from .keys import key\nclass Cache:\n    def __init__(self): self.data={}\n    def set(self,k,v,ttl,now): self.data[key(k)]=(v,now+ttl)\n    def get(self,k,now):\n        pair=self.data.get(key(k))\n        if pair and now<=pair[1]: return pair[0]\n        return None\n',
        'cachelib/service.py':'def get_or_compute(cache,key,fn,ttl,now):\n    value=cache.get(key,now)\n    if value: return value\n    value=fn();cache.set(key,value,ttl,now);return value\n',
        'earlier-decisions.md':'API decision: boolean, integer and string keys must remain separate, including within nested lists and dictionaries. This is required for compatibility. A cached 0 is a cache hit. None remains the missing-value sentinel.\n',
        'tests/test_cache.py':'from cachelib import Cache,get_or_compute\ndef test_boundary():\n    c=Cache();c.set("a",1,2,0);assert c.get("a",2) is None\ndef test_copy():\n    c=Cache();v=[1];c.set("a",v,2,0);v.append(2);assert c.get("a",1)==[1]\n'},
        "from cachelib import Cache,get_or_compute\nc=Cache();c.set(True,'bool',3,0);c.set(1,'int',3,0);c.set('1','str',3,0)\nassert [c.get(k,1) for k in [True,1,'1']]==['bool','int','str']\nc.set('x',[1],3,0);v=c.get('x',1);v.append(2);assert c.get('x',1)==[1]\nc.set('z',0,3,0);assert get_or_compute(c,'z',lambda:99,3,1)==0\nassert c.get('x',3) is None\nc.set('zero',42,0,0);assert c.get('zero',0) is None")
    add('dependency-invalidates-history','Replace legacy fee computation after a rounding dependency policy change. dependency-policy.json is authoritative for current behavior; history.md describes an obsolete environment and must not override it. Convert Decimal strings to integer cents using ROUND_HALF_UP, reject nonfinite and negative amounts, calculate percentage discounts before rounding once, preserve fee(amount,discount="0") and batch(rows). Add tests that explain why the historical passing examples are obsolete.',{
        'pricing/__init__.py':'from .fees import fee,batch\n',
        'pricing/convert.py':'def cents(amount): return round(float(amount)*100)\n',
        'pricing/fees.py':'from .convert import cents\ndef fee(amount,discount="0"):\n    return cents(amount)-cents(amount)*float(discount)/100\ndef batch(rows): return [fee(r["amount"],r.get("discount","0")) for r in rows]\n',
        'dependency-policy.json':'{"revision":2,"money":"Decimal","rounding":"ROUND_HALF_UP","discount_range":[0,100],"round_once":true}\n',
        'history.md':'Historical check, dependency policy revision 1: Python float round gave fee("1.005") == 100. This was a historical observation, not a current requirement. Policy revision 2 supersedes it.\n',
        'tests/test_fees.py':'from pricing import fee\ndef test_half_cent(): assert fee("1.005")==101\ndef test_discount(): assert fee("1.005","50")==50\n'},
        "from pricing import fee,batch\nassert fee('1.005')==101\nassert fee('2.675')==268\nassert fee('1.005','50')==50\nassert batch([{'amount':'2.675'},{'amount':'1','discount':'100'}])==[268,0]\nfor x in ['NaN','Infinity','-1']:\n try: fee(x)\n except ValueError: pass\n else: raise AssertionError(x)")
    return result


class WindowedContext(ContextEngine):
    def build(self,run_id,tools,policy=None,*args,**kwargs):
        policy={**(policy or {}),'max_tokens':15500,'reserve_tokens':4096}
        return super().build(run_id,tools,policy,*args,**kwargs)


async def trial(output,task,arm,p,profile,encoder,image,state_root=None,preflight=False,visibility='DEV',release=None,model_call_limit=40):
    folder=output/task['id']/arm;source=folder/'input';source.mkdir(parents=True)
    for path,text in task['files'].items():atomic_write(source/path,text.encode())
    app=RepoCode(state_root or folder/'host',p,'docker',image=image,memory_profile=profile if arm=='D' else None,memory_encoder=encoder if arm=='D' else None)
    if arm=='A':app.agent.context=WindowedContext(app.store,app.runtime,app.agent.context.request_serializer,app.agent.context.output_reserve,app.agent.context.input_scale)
    contract=Contract.model_validate(task['contract'])
    run=app.new_task(task['goal'],source,contract,Budget(model_calls=model_call_limit,tool_calls=100,wall_seconds=600),True,True,visibility,release)
    started=time.monotonic();error=None
    infra=None
    try:
        if preflight:
            check="from pathlib import Path; p=Path('/workspace'); list(p.iterdir()); f=p/'._host_preflight'; f.write_text('ok'); assert f.read_text()=='ok'; print('PREFLIGHT_OK')"
            infra=await app.sandbox.run(run['id'],app.runtime.workspace(run).root,['python','-c',check],timeout=20)
            write(folder/'infrastructure-preflight.json',infra)
            if infra['exit_code']!=0 or infra['stdout'].strip()!='PREFLIGHT_OK':
                app.runtime.end(run['id'],'BLOCKED','INFRA_ERROR: workspace preflight failed')
                raise RepoError('INFRA_ERROR')
        report=await app.execute(run['id'])
    except Exception as exc:error=getattr(exc,'code',type(exc).__name__);report=app.agent.report(run['id'])
    finally:stopped=app.sandbox.stop_run(run['id'])
    summaries=app.memory.repo.values('summary_nodes') if arm=='D' else []
    projection=app.store.get('projection',run['id'])
    result={'task':task['id'],'arm':arm,'run_id':run['id'],'state':report['state'],'reason':report.get('reason'),'error':error,
            'verified':report['state']=='SUCCEEDED' and bool(report['delivery']),'seconds':time.monotonic()-started,
            'resources_stopped':stopped,'report':report,'compactions':sum(s['level']=='EPISODE' for s in summaries),
            'legacy_folded_groups':(projection or {}).get('folded_groups',0),
            'memory_budget':app.memory.repo.values('memory_budget') if arm=='D' else []}
    result['state_root']=str(app.store.root);result['infrastructure_preflight_passed']=infra['exit_code']==0 if infra else None
    if error=='INFRA_ERROR':result['state']='INFRA_ERROR'
    write(folder/'result.json',result)
    write(folder/'messages-visible.json',[semantic_message(m) for m in app.store.messages(run['id'])])
    shutil.copytree(report['workspace'],folder/'output',ignore=shutil.ignore_patterns('__pycache__','.pytest_cache'))
    write(folder/'events.json',app.store.events(run['id'],trusted=True,limit=10000))
    return result


async def main_async(args):
    args.output.mkdir(parents=True,exist_ok=False)
    profile=MemoryProfile.model_validate(json.loads(args.profile.read_text(encoding='utf-8'))).model_copy(update={
        'max_tokens':16000,'reserve_tokens':4096,'safety_tokens':500,'auto_activate_observations':True,'auxiliary_calls':16})
    image=json.loads((ROOT/'examples/sandbox/validation-config.json').read_text())['image']
    encoder=LocalEncoder(profile.encoder);p=provider(args.output);frozen=tasks()
    write(args.output/'frozen-tasks.json',frozen)
    write(args.output/'experiment.json',{'task_digest':digest(frozen),'profile':profile.model_dump(),'provider':p.inner.model,
        'thinking':'disabled','arm_order':['A','D'],'max_calls_per_task_including_auxiliary':40,'max_context_tokens_both':16000,
        'active_input_allocation_both':11404,'output_reserve_both':4096,'safety_allowance_both':500,
        'policy_changes_during_experiment':False,'long_context_validity':'Report observed compactions; absence is insufficient long-context evidence.'})
    results=[]
    try:
        for task in frozen:
            for arm in ['A','D']:
                results.append(await trial(args.output,task,arm,p,profile,encoder,image))
                write(args.output/'summary.json',[{k:v for k,v in r.items() if k not in {'report','memory_budget'}} for r in results])
    finally:encoder.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();asyncio.run(main_async(args))


if __name__=='__main__':main()
