"""Deterministic offline integration demo, explicitly NOT model-quality evidence."""
from __future__ import annotations
import json
from pathlib import Path
from .models import Assistant,ToolCall,Contract,Check,Requirement,Budget
from .providers import ScriptedProvider
from .service import RepoCode
from .acceptance import freeze_contract
from .observation import Observer
from .governance import HoldoutVault
from .evaluation import SuiteEvaluator
from .evolution import EvolutionEngine
from .delivery import Delivery
from .util import RepoError,atomic_write

FIXTURE_ERROR=['python','-c',"raise ModuleNotFoundError('offline fixture: no installed module')"]

def task_fixture(name):
    contract=freeze_contract(Contract(requirements=[Requirement(id='R',description='Implement main.answer() returning 42')],
        checks=[Check(id='answer',kind='function',definition={'module':'main','function':'answer','cases':[{'id':'42','expected':42}]})],
        required_checks=['answer'],coverage={'R':['answer']}),'offline-demo-curator')
    return {'id':name,'goal':'Create main.py with answer() returning 42 after diagnosing the fixture failure.','files':{},'contract':contract.model_dump(mode='json'),'budget':{'model_calls':4,'tool_calls':12,'wall_seconds':60}}

def coding_fixture(public=None):
    def respond(system,messages,tools,i):
        # Simulated model has one deliberate behavior defect for protocol testing.
        # It sees ONLY public prompts, not candidate IDs, hidden answers or scores.
        created=any(m.get('role')=='assistant' and any(t['name']=='create' for t in m.get('tool_calls',[])) for m in messages)
        if created:return Assistant(text='Request external verification.')
        hint=any('Runtime recovery hint:' in str(m.get('content','')) for m in messages)
        if hint:return Assistant(tool_calls=[ToolCall(id='create-'+str(i),name='create',arguments={'path':'main.py','content':'def answer():\n    return 42\n'})])
        return Assistant(tool_calls=[ToolCall(id='retry-'+str(i),name='process.run',arguments={'argv':FIXTURE_ERROR})])
    return ScriptedProvider(handler=respond)

def optimizer_fixture(public=None,purpose='optimizer'):
    if purpose=='coding':return coding_fixture(public)
    def respond(system,messages,tools,i):
        payload=json.loads(messages[-1]['content'])
        if 'FROZEN optimizer instance' in system:
            cfg=payload['meta_strategy']
            opts=[{'max_candidates':1,'max_rounds':1,'order':'evidence_first'}]
            if not cfg.get('prefer_evidence'):opts.insert(0,{'max_candidates':1,'max_rounds':1,'order':'unhelpful'})
            return Assistant(text=json.dumps({'optimizers':opts}))
        if 'Diagnose ONLY' in system:
            events=payload['events'];ids=[x['event_id'] for x in events if x['type']=='tool.operation_finished']
            return Assistant(text=json.dumps({'conclusion':'testable','hypothesis':'Identical deterministic failure is retried without new evidence','targets':['retry_policy'],'evidence_ids':ids[-2:],'negative_probes':['Do not forbid transient retries']}))
        if 'Generate targeted' in system:
            good=payload['diagnosis'].get('search_strategy')!='unhelpful'
            return Assistant(text=json.dumps({'candidates':[{'operation':'REPLACE','changes':{'retry_policy':{'kind':'config','max_identical_failures':2,'require_new_evidence':good}},'hypothesis':'Ask for new evidence after repeated deterministic failure','growth_justification':''}]}))
        raise RepoError('DEMO_UNEXPECTED_PROMPT')
    return ScriptedProvider(handler=respond)

async def run_demo(state_root,output,sandbox='auto',image=None):
    output=Path(output).absolute()
    if output.exists() and any(output.iterdir()):raise RepoError('DEMO_OUTPUT_MUST_BE_EMPTY')
    output.mkdir(parents=True,exist_ok=True)
    app=RepoCode(state_root,coding_fixture(),sandbox,image)
    app.store.put('operator_config','integration',{'enabled':True,'reviewer':'EXPLICIT OFFLINE DEMO ONLY'})
    fixture=task_fixture('source-demo');contract=Contract.model_validate(fixture['contract']);sources=[]
    for i in range(2):
        work=output/('source-'+str(i));work.mkdir()
        app.agent.provider=app.broker.provider=coding_fixture()
        run=app.new_task(fixture['goal'],work,contract,Budget.model_validate(fixture['budget']),True,True)
        result=await app.execute(run['id']);sources.append(result)
    observer=Observer(app.store);patterns=observer.consume()
    if not patterns:raise RepoError('DEMO_PATTERN_MISSING')
    pattern=patterns[0];triage=observer.triage(pattern)
    vault=HoldoutVault(app.store)
    dev=vault.register('demo-dev','dev',[task_fixture('dev-fixture')],synthetic=True)
    promotion=vault.register('demo-promotion','promotion',[task_fixture('promotion-fixture')],synthetic=True)
    final=vault.register('demo-final','final',[task_fixture('final-fixture')],synthetic=True)
    evaluator=SuiteEvaluator(app,vault,coding_fixture)
    engine=EvolutionEngine(app,optimizer_fixture(),evaluator)
    search=await engine.search(pattern,dev,max_candidates=1,max_rounds=1)
    candidate=search['selected']
    if not candidate:raise RepoError('DEMO_NO_CANDIDATE')
    qualification=await evaluator.qualify(candidate,dev,promotion,final)
    if qualification['status']!='PASS':raise RepoError('DEMO_GATE_REJECTED')
    app.releases.approve(candidate,'offline-demo-operator');active=app.releases.activate(candidate)
    final_source=output/'final-source';final_source.mkdir()
    app.agent.provider=app.broker.provider=coding_fixture()
    run=app.new_task(fixture['goal'],final_source,contract,Budget.model_validate(fixture['budget']),True,True)
    delivered=await app.execute(run['id'])
    if delivered['state']!='SUCCEEDED':raise RepoError('DEMO_DELIVERY_FAILED')
    (output/'generated').mkdir();Delivery(app).export(run['id'],output/'generated')
    report={'kind':'offline_scripted_provider_integration_NOT_model_benchmark','source_runs':[r['state'] for r in sources],
        'pattern':pattern,'triage':triage,'search':search,'qualification':qualification,'release':active,'delivery':delivered,
        'real_effects':['filesystem','isolated processes','external grader','SQLite CAS','private holdout routing'],
        'not_proven':['real model improvement','statistical generalization','Docker or Windows isolation']}
    atomic_write(output/'demo-report.json',json.dumps(report,ensure_ascii=False,indent=2).encode())
    return report
