"""New DEV continual-learning experiment; never consumes formal holdouts.

Two independent four-task streams, v1/v2.1 with persistent/fresh-state arms.
No manual memory drafting, review, oracle feedback, or code carry-over.
"""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

from evaluate_memory_v2 import provider, write
from memory_repair_dev import BudgetAuditProvider
from memory_task_trials import WindowedContext
from repo_code.acceptance import freeze_contract, delivery_verified
from repo_code.memory.encoders import LocalEncoder
from repo_code.memory.models import MemoryProfile
from repo_code.models import Budget, Check, Contract, Requirement, VerificationIntent, Evidence
from repo_code.providers import ScriptedProvider, model_configuration
from repo_code.service import RepoCode
from repo_code.util import digest

ROOT = Path(__file__).resolve().parents[1]
ARMS = ['v1_on', 'v21_on', 'v1_off', 'v21_off']


def source_lock():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for base in ['src', 'scripts'] for p in sorted((ROOT/base).rglob('*.py'))}


def bank():
    tasks = []
    for stream, field, group, scale, rounding in [
        ('invoice', 'price', 'currency', 100, 'ROUND_HALF_UP'),
        ('metering', 'reading', 'sensor', 1000, 'ROUND_HALF_EVEN')]:
        for stage, api in enumerate(['normalize', 'aggregate', 'render', 'reconcile'], 1):
            policy = dict(amount_field=field, group_field=group, scale=scale,
                          rounding=rounding, group_case='upper' if stream == 'invoice' else 'lower', revision=1)
            if stage == 4:
                policy.update(scale=scale*10, rounding='ROUND_HALF_DOWN', revision=2)
            normal = '''Normalization rules: load authoritative policy.json. Each row has id, revision, status plus the configured amount_field and group_field. Strip id and group; apply policy group_case. id and group are nonempty strings; revision is a nonnegative int excluding bool; status is active or cancelled; amount is a finite nonnegative Decimal string. Skip malformed rows and count each as invalid, including invalid duplicate rows. Select greatest revision per id, last valid row wins ties; a selected cancelled row removes that id. Convert amount * scale to integer units with policy rounding, once per selected row. Sort surviving rows by id, each exactly {id, group, units}. Empty input is valid. Do not mutate inputs.'''
            specific = {
                'normalize': 'Implement normalize(rows) -> {rows: normalized_rows, invalid: count}.',
                'aggregate': 'Implement aggregate(rows) -> {totals: {group: summed_units}, invalid: count, count: surviving_row_count}. Sort totals by group.',
                'render': 'Implement render(rows) returning deterministic JSON text for {revision: policy revision, records: normalized_rows, invalid: count}. Use ensure_ascii=False, sort_keys=True, separators=(",",":"); no trailing newline.',
                'reconcile': 'The current policy revision supersedes historical settings. Implement reconcile(before, after) -> {delta: {group: after_total-minus-before_total}, invalid: sum_of_both_invalid_counts}. Include every group occurring on either side, even zero delta. Sort delta keys.'}[api]
            goal = f'{stream} service task {stage}. {specific} {normal} Read relevant files before edits; run the existing tests, add focused tests and update README.md; finish by running pytest. policy.json is protected. Prior task code and conversations are not available; scoped memory, when present, is historical evidence to check against current policy.'
            core = '''import json
from pathlib import Path
from decimal import Decimal, InvalidOperation
def _normalize(rows):
    p=json.loads(Path('policy.json').read_text()); chosen={}; invalid=0
    for r in rows:
        try:
            if not isinstance(r,dict): raise ValueError()
            ident=r['id']; g=r[p['group_field']]; rev=r['revision']; status=r['status']; amount=r[p['amount_field']]
            if not isinstance(ident,str) or not isinstance(g,str) or not ident.strip() or not g.strip(): raise ValueError()
            if type(rev) is not int or rev<0 or status not in ('active','cancelled') or not isinstance(amount,str): raise ValueError()
            d=Decimal(amount)
            if not d.is_finite() or d<0: raise ValueError()
            ident=ident.strip(); g=getattr(g.strip(),p['group_case'])()
            units=int((d*p['scale']).quantize(Decimal('1'),rounding=p['rounding']))
            if ident not in chosen or rev>=chosen[ident][0]: chosen[ident]=(rev,status,g,units)
        except (KeyError,TypeError,ValueError,InvalidOperation,AttributeError): invalid+=1
    return {'rows':[{'id':i,'group':v[2],'units':v[3]} for i,v in sorted(chosen.items()) if v[1]=='active'],'invalid':invalid}
def normalize(rows): return _normalize(rows)
def aggregate(rows):
    n=_normalize(rows); t={}
    for r in n['rows']: t[r['group']]=t.get(r['group'],0)+r['units']
    return {'totals':dict(sorted(t.items())),'invalid':n['invalid'],'count':len(n['rows'])}
def render(rows):
    n=_normalize(rows); p=json.loads(Path('policy.json').read_text())
    return json.dumps({'revision':p['revision'],'records':n['rows'],'invalid':n['invalid']},ensure_ascii=False,sort_keys=True,separators=(',',':'))
def reconcile(before,after):
    a=aggregate(before); b=aggregate(after)
    return {'delta':{k:b['totals'].get(k,0)-a['totals'].get(k,0) for k in sorted(set(a['totals'])|set(b['totals']))},'invalid':a['invalid']+b['invalid']}
'''
            oracle = ('import copy,json,types\nfrom worker import '+api+' as target\nreference=types.ModuleType("reference")\nexec('+repr(core)+',reference.__dict__)\n'
                      'p=json.load(open("policy.json")); f=p["amount_field"]; g=p["group_field"]\n'
                      'def row(i,a="1.005",rev=0,status="active",group=" West "): return {"id":i,f:a,g:group,"revision":rev,"status":status}\n'
                      'cases=[[],[row(" a ")],[row("a"),row("a","7",1,"cancelled"),row("b","2.345",group="EAST")],'
                      '[row("x","1.005"),row("x","2.675"),row("c","0.0005",group="中文")],'
                      '[row("bad","NaN"),row("neg","-1"),row("inf","Infinity"),row("r","1",True),{},None,row(" ")],'
                      '[row("a","2",2),row("a","8",1),row("b","0"),row("c","3",0,"cancelled"),row("a","NaN",3)]]\n'
                      'for rows in cases:\n original=copy.deepcopy(rows)\n args=(rows,list(reversed(rows))+[row("new","0.0015",group="north")]) if '+repr(api)+'=="reconcile" else (rows,)\n expected=getattr(reference,'+repr(api)+')(*copy.deepcopy(args))\n actual=target(*args)\n assert actual==expected,(actual,expected)\n assert rows==original\n'
                      'print("PASS")')
            contract = freeze_contract(Contract(requirements=[Requirement(id='behavior', description=goal)],
                checks=[Check(id='external', kind='cli', definition={'argv':['python','-c',oracle], 'stdout':'PASS\n'})],
                required_checks=['external'], coverage={'behavior':['external']}, protected_paths=['policy.json']), 'continual-frozen-author')
            test = f'from worker import {api}\ndef test_empty():\n    assert {api}([],[]) == {{"delta":{{}},"invalid":0}}\n' if api == 'reconcile' else (
                f'from worker import {api}\ndef test_empty():\n    assert {api}([]) == '+repr({'normalize':{'rows':[], 'invalid':0},'aggregate':{'totals':{},'invalid':0,'count':0},'render':json.dumps({'revision':policy['revision'],'records':[],'invalid':0},ensure_ascii=False,sort_keys=True,separators=(',',':'))}[api])+'\n')
            tasks.append({'id':f'{stream}-{stage}-{api}', 'stream':stream,'stage':stage,'api':api,'goal':goal,
                'policy':policy,'files':{'policy.json':json.dumps(policy,sort_keys=True)+'\n',
                    'worker.py':f'def {api}('+('before,after' if api=='reconcile' else 'rows')+'):\n    raise NotImplementedError("pending")\n',
                    'tests/test_worker.py':test,'README.md':'Project policy is in policy.json. Public module worker.py. Run python -m pytest.\n'},
                'contract':contract.model_dump(mode='json'),'gold':core,'oracle':oracle})
    return tasks


def summarize(rows):
    out = {}
    for arm in ARMS:
        subset=[r for r in rows if r['arm']==arm]
        out[arm]={}
        for name, selected in [('all',subset),('acquisition',[r for r in subset if r['stage']==1]),
                               ('transfer',[r for r in subset if r['stage'] in [2,3]]),('policy_change',[r for r in subset if r['stage']==4])]:
            out[arm][name]={'runs':len(selected),'passed':sum(r['passed'] for r in selected),
                'model_calls':sum(r['cost']['model_calls'] for r in selected),'tool_calls':sum(r['cost']['tool_calls'] for r in selected),
                'input_tokens':sum(r['cost']['input_tokens'] for r in selected),'output_tokens':sum(r['cost']['output_tokens'] for r in selected),
                'unknown_usage':sum(r['cost']['unknown_usage'] for r in selected),'auxiliary_calls':sum(r['cost']['auxiliary_calls'] for r in selected),
                'prior_fact_recall_runs':sum(bool(r['recall']['effective_prior_fact_ids']) for r in selected),
                'first_request_fact_recall_runs':sum(bool(r['recall']['first_request_prior_fact_ids']) for r in selected),
                'available_prior_facts':sum(r['recall']['available_current_prior_facts'] for r in selected),
                'seconds':sum(r['seconds'] for r in selected)}
    return out


class RecallAudit(BudgetAuditProvider):
    """Capture evidence actually submitted to the model, not just retriever hits."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs); self.contexts=[]
    async def complete(self,system,messages,tools,*args,**kwargs):
        hits=[]
        for message in messages:
            if message.get('role')!='user':continue
            try:body=json.loads(message.get('content',''))
            except (ValueError,TypeError):continue
            if isinstance(body,dict) and body.get('type')=='memory_context_data':hits+=body.get('retrieved_evidence',[])
        if tools:
            self.contexts.append({'ordinal':len(self.contexts)+1,'hits':hits,'system_digest':digest(system)})
            write(self.output/'effective-retrieval.json',self.contexts)
        return await super().complete(system,messages,tools,*args,**kwargs)


async def run(args):
    args.output.mkdir(parents=True,exist_ok=False)
    tasks=bank(); images=json.loads((ROOT/'examples/sandbox/validation-config.json').read_text())
    unlimited=getattr(args,'unlimited_budget',False)
    token_cap=None if unlimited else 500000
    tool_cap=10000 if unlimited else 100
    wall_seconds=86400 if unlimited else 600
    profile=MemoryProfile.model_validate(json.loads((ROOT/'examples/memory/deepseek-experimental.json').read_text())).model_copy(update={
        'learning_enabled':True,'auto_activate_observations':True,'auto_activate_preferences':False,'auxiliary_calls':None,'total_token_budget':token_cap})
    work=Path(tempfile.mkdtemp(prefix='mcl-',dir=ROOT/'.venv'))
    locked=source_lock()
    write(args.output/'task-bank.json',tasks)
    write(args.output/'evaluation-lock.json',{'date':'2026-11-02','cohort':'continual-new-dev-20261102','class':'AUTHORED_PROGRAMMING_TASKS_REAL_DEEPSEEK',
        'tasks_digest':digest(tasks),'source_files':locked,'profile':profile.model_dump(mode='json'), 'arms':ARMS,
        'arm_order':'task odd: v1_on,v21_on,v1_off,v21_off; task even reversed',
        'streams':2,'tasks_per_stream':4,'shared_scope':'stable input path per arm per stream',
        'learning':'v21_on automatic Host observations active; semantic proposals remain candidates; v1_on native persistent reviewed-knowledge mechanism, no automatic writer',
        'ablation':'off arms fresh state each task; v21_off learning_enabled=False, same other parameters',
        'no_code_or_message_carryover':True,'manual_memory_seed_or_activation':False,'holdout_claims':False,'publication':False,
        'window':16000,'output':4096,'safety':500,'model_call_cap':None,'per_run_token_cap':token_cap,'tool_cap':tool_cap,'wall_seconds':wall_seconds,
        'unlimited_budget':unlimited,'budget_authorization':'User 2026-11-02: 不限预算 做好统计' if unlimited else 'prior bounded experiment configuration',
        'concurrent_arms':getattr(args,'concurrency',1),'stage_barrier':True,
        'resource_limits_preserved':'Sandbox, single request window/output, per-process timeout, protocol Task total 1000 tools, 24h watchdog when unlimited; no fee or cumulative Token cap.',
        'private_state_base':str(work),'success':'automatic protected external signed contract delivery only',
        'recall':'prior-run current policy fact present in actual main request; not inferred from successful task',
        'falsification':'revision2 old file facts must be excluded; original outcomes and all auxiliary errors retained',
        'limitations':['two streams, no population significance','v1 has no native automatic consolidation','all tasks can be solved from current files; no forced memory-only hidden answers']})
    # Validate all authored failing fixtures and gold implementations in real Docker.
    preflight=RepoCode(work/'preflight',ScriptedProvider(),'docker',images['image'])
    fixture=[]
    try:
        for task in tasks:
            src=work/'fixture';src.mkdir(exist_ok=True)
            for name,content in task['files'].items():
                p=src/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content,encoding='utf-8')
            for kind in ['broken','gold']:
                (src/'worker.py').write_text(task['gold'] if kind=='gold' else task['files']['worker.py'],encoding='utf-8')
                r=preflight.new_task('Fixture validation',src)
                try:result=await preflight.sandbox.run(r['id'],preflight.runtime.workspace(r).root,['python','-c',task['oracle']],timeout=25)
                finally:preflight.sandbox.stop_run(r['id']);preflight.runtime.end(r['id'],'BLOCKED','fixture finished')
                fixture.append({'task':task['id'],'kind':kind,**result});write(args.output/'fixture-validation.json',fixture)
                assert (result['exit_code']==0)==(kind=='gold'), (task['id'],kind,result)
        print('All 8 broken fixtures fail and all 8 gold references pass real Docker checks',flush=True)
    finally:preflight.close()
    encoder=LocalEncoder(profile.encoder); rows=[]; release_set=set()
    try:
        semaphore=asyncio.Semaphore(getattr(args,'concurrency',1))
        async def execute_trial(task,arm):
            async with semaphore:
                assert source_lock()==locked,'source changed after freeze'
                folder=args.output/task['id']/arm; folder.mkdir(parents=True)
                p=RecallAudit(provider(folder).inner,folder,None,token_cap)
                is_v2=arm.startswith('v21'); learning=arm.endswith('_on')
                state=work/task['stream']/arm/('host' if learning else f'host-{task["stage"]}')
                source=work/task['stream']/arm/'input'
                if source.exists():shutil.rmtree(source)
                source.mkdir(parents=True)
                for path,text in task['files'].items():
                    f=source/path;f.parent.mkdir(parents=True,exist_ok=True);f.write_text(text,encoding='utf-8')
                selected=profile.model_copy(update={'learning_enabled':learning}) if is_v2 else None
                app=RepoCode(state,p,'docker',images['image'],memory_profile=selected,memory_encoder=encoder if is_v2 else None)
                if not is_v2:app.agent.context=WindowedContext(app.store,app.runtime,app.agent.context.request_serializer,app.agent.context.output_reserve,app.agent.context.input_scale)
                before=app.memory.repo.values('memory_versions') if is_v2 else app.store.values('knowledge')
                run=app.new_task(task['goal'],source,Contract.model_validate(task['contract']),Budget(model_calls=None,tool_calls=tool_cap,wall_seconds=wall_seconds),True,True)
                release_set.add(run['data']['manifest']['release_digest'])
                assert len(release_set)==1,'arm release mismatch'
                started=time.monotonic();error=None
                try:
                    try:report=await app.execute(run['id'])
                    except Exception as exc:
                        error=getattr(exc,'code',type(exc).__name__);report=app.agent.report(run['id'])
                    report=app.agent.report(run['id'])
                    stop=app.sandbox.stop_run(run['id'])
                    passed=False;verified=None
                    if report['delivery']:
                        delivery=report['delivery'];intent=VerificationIntent.model_validate(app.store.get('intent',delivery['intent_digest']))
                        evidence=[Evidence.model_validate(app.store.get('evidence',eid)) for eid in delivery['evidence_ids']]
                        passed,why=delivery_verified(intent,Contract.model_validate(task['contract']),{e.check_id:e for e in evidence},delivery.get('artifact_digest'),app.store)
                        verified={'valid':passed,'reason':why,'evidence':[e.model_dump(mode='json') for e in evidence]}
                    effective={h['id'] for ctx in p.contexts for h in ctx['hits']}
                    first={h['id'] for h in p.contexts[0]['hits']} if p.contexts else set()
                    prior={x['claim_id']:x for x in before if is_v2 and x.get('kind')=='FACT' and any(r['run_id']!=run['id'] for r in x.get('support_refs',[]))}
                    version=app.runtime.workspace(app.runtime.run(run['id'])).read('policy.json')['version']
                    current={key for key,x in prior.items() if x.get('file_dependencies',{}).get('policy.json')==version and app.memory.repo.get('memory_control','claim:'+key)=={'version':x['version'],'lifecycle':'ACTIVE'}}
                    stale=set(prior)-current
                    traces=[x for x in app.memory.repo.values('retrieval_traces') if x['run_id']==run['id']] if is_v2 else []
                    calls=p.calls; tokens=lambda key,alias:sum((c.get('usage',{}).get(key,c.get('usage',{}).get(alias,0)) or 0) for c in calls)
                    after=app.memory.repo.values('memory_versions') if is_v2 else app.store.values('knowledge')
                    failures=[x for x in app.memory.repo.values('memory_control') if x.get('run_id')==run['id'] and x.get('code')] if is_v2 else []
                    result={'task':task['id'],'stream':task['stream'],'stage':task['stage'],'arm':arm,'run_id':run['id'],'state_root':str(state),
                        'state':report['state'],'reason':report['reason'],'error':error,'passed':bool(passed and report['state']=='SUCCEEDED'),
                        'report':report,'verified':verified,'seconds':time.monotonic()-started,'resources_stopped':stop,
                        'memory_before':len(before),'memory_after':len(after),'auxiliary_failures':failures,
                        'cost':{'model_calls':len(calls),'auxiliary_calls':sum(c['purpose']!='main' for c in calls),
                            'tool_calls':report['budget_used']['tool_calls'],'input_tokens':tokens('prompt_tokens','input_tokens'),
                            'output_tokens':tokens('completion_tokens','output_tokens'),'unknown_usage':sum(not c.get('usage') for c in calls)},
                        'recall':{'available_current_prior_facts':len(current),'effective_prior_fact_ids':sorted(effective&current),
                            'first_request_prior_fact_ids':sorted(first&current),'stale_prior_fact_ids':sorted(stale),
                            'stale_fact_ids_exposed':sorted(effective&stale),'effective_all_ids':sorted(effective),'trace_count':len(traces)}}
                    write(folder/'events.json',app.store.events(run['id'],trusted=True,limit=10000));write(folder/'memory-after.json',after)
                    write(folder/'retrieval-traces.json',traces);write(folder/'result.json',result)
                    rows.append(result);write(args.output/'results.json',rows);write(args.output/'summary.json',summarize(rows))
                    print(json.dumps({k:result[k] for k in ['task','arm','state','passed','cost','recall','auxiliary_failures']},ensure_ascii=False),flush=True)
                finally:app.sandbox.stop_run(run['id']);app.close();await p.inner.client.close()
        for task in tasks:
            order=ARMS if task['stage']%2 else list(reversed(ARMS))
            await asyncio.gather(*(execute_trial(task,arm) for arm in order))
    finally:encoder.close()
    write(args.output/'completion.json',{'completed':len(rows)==32,'runs':len(rows),'source_unchanged':source_lock()==locked,'release':list(release_set),'formal_holdouts_consumed':False})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--unlimited-budget',action='store_true',help='Explicit operator opt-in: no cumulative token/call/fee cap; retain protocol and sandbox limits')
    parser.add_argument('--concurrency',type=int,choices=[1,2,4],default=1,help='Independent arms only; each next task waits for all arms')
    asyncio.run(run(parser.parse_args()))
