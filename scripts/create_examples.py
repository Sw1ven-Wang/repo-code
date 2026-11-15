"""Regenerate reviewed demonstration contracts (not a production acceptance oracle)."""
import json
from pathlib import Path
from repo_code.models import Contract,Requirement,Check
from repo_code.acceptance import freeze_contract

root=Path(__file__).resolve().parents[1]/'examples'
root.mkdir(exist_ok=True)
def dump(name,value):
    (root/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

draft=Contract(requirements=[Requirement(id='answer',description='main.answer() returns the integer 42')],checks=[
    Check(id='answer',kind='function',definition={'module':'main','function':'answer','cases':[{'id':'answer-is-42','expected':42}]}),
    Check(id='syntax',kind='static_python',definition={})],required_checks=['answer','syntax'],global_required_checks=['syntax'],coverage={'answer':['answer']},interface={'language':'Python','module':'main','function':'answer','arguments':[],'return':'integer 42'})
dump('answer.draft.json',draft.model_dump(mode='json'))
dump('answer.contract.json',freeze_contract(draft,'reviewed-offline-demo-fixture').model_dump(mode='json'))
def tool(id,name,**args):return {'tool_calls':[{'id':id,'name':name,'arguments':args}]}
dump('answer.script.json',[
    tool('create-main','create',path='main.py',content='def answer():\n    return 0\n'),
    {'text':'Request the first verification; this does not assert success.'},
    tool('read-main','read',path='main.py'),
    tool('correct-main','edit',path='main.py',old_text='return 0',new_text='return 42'),
    tool('run-main','process.run',argv=['python','-c','from main import answer; print(answer())']),
    {'text':'The implementation is corrected. Request the external verification and delivery commit.'},
])
api=Contract(requirements=[Requirement(id='api',description='GET /value/{n} returns integer 2*n; unknown routes return HTTP404')],checks=[Check(id='http',kind='asgi',definition={'module':'main','app':'app','cases':[{'id':'normal','path':'/value/21','expected_status':200,'expected_body':{'value':42}},{'id':'negative','path':'/value/-3','expected_status':200,'expected_body':{'value':-6}},{'id':'missing','path':'/missing','expected_status':404,'expected_body':{'detail':'Not Found'}}]}),Check(id='syntax',kind='static_python')],required_checks=['http','syntax'],global_required_checks=['syntax'],coverage={'api':['http']},interface={'transport':'ASGI','app':'main.app'})
dump('fastapi.contract.json',freeze_contract(api,'reviewed-demo-api-curator').model_dump(mode='json'))
dump('retry-policy.changes.json',{'retry_policy':{'kind':'config','max_identical_failures':2,'require_new_evidence':True}})
