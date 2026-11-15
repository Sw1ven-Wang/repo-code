import json
import pytest
from repo_code.models import Candidate
from repo_code.governance import HoldoutVault
from repo_code.meta import MetaEvaluator
from repo_code.demo import task_fixture,optimizer_fixture
from repo_code.util import RepoError
from repo_code.evolved import EvolvedWorker
from repo_code.providers import ScriptedProvider
from repo_code.models import Assistant


def problem(name):
    return {'id':name,'description':'Avoid repeated deterministic failures','evidence':{
        'pattern':{'symptom':'ineffective_retry','error_family':'ModuleNotFoundError'},
        'events':[{'type':'tool.operation_finished','data':{'tool_name':'process.run','action_digest':'same','result':{'is_error':True,'relevant_state':'same','stderr':'ModuleNotFoundError: fixture'}}}]},
        'budget':{'model_calls':100,'wall_seconds':180},
        'dev_tasks':[task_fixture(name+'-dev')],
        'score_tasks':[task_fixture(name+'-score')]}

@pytest.mark.asyncio
async def test_full_nested_meta_comparison_and_default_disabled(app):
    c=app.releases.candidate('meta-fixture',{'l2_optimizer':{'kind':'config','max_candidates':1,'prefer_evidence':True}},'Reduce wasted optimizer search on the fixture',[],level=2)
    vault=HoldoutVault(app.store)
    cohorts=[vault.register(role,role,[problem(role)],synthetic=True) for role in ('meta_dev','meta_promotion','meta_final')]
    seen=[]
    def factory(public,purpose):
        seen.append((public,purpose))
        assert 'score_tasks' not in public and 'contract' not in public
        return optimizer_fixture(public,purpose)
    ev=MetaEvaluator(app,vault,factory)
    result=await ev.qualify(c.id,*cohorts)
    assert result['status']=='PASS',result
    app.releases.approve(c.id,'test-meta-operator')
    with pytest.raises(RepoError,match='META_SELF_MODIFY_DISABLED'):app.releases.activate(c.id)
    app.store.put('operator_config','meta',{'enabled':True,'reviewer':'test-only'})
    app.store.put('operator_config','integration',{'enabled':True,'reviewer':'test-fixture'})
    app.releases.activate(c.id)
    episodes=app.store.values('meta_private')
    assert len(episodes)==4
    assert all(e.get('audit_archive_digest') for e in episodes)
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(app.store.read_blob(episodes[0]['audit_archive_digest']))) as archive:
        assert archive.testzip() is None and 'runtime.db' in archive.namelist()
    assert all(e['fresh_instance'] and e['all_model_calls']>0 and len(e['attempts'])>0 for e in episodes)
    assert all(e['protocol']=='nested-l2-propose-l1-search-l0-trials-v1' for e in episodes)
    assert any(len(e['attempts'])==2 for e in episodes)
    assert app.store.get('gate',__import__('repo_code.util',fromlist=['digest']).digest([c.id,'META']))['payload']['passed']

@pytest.mark.asyncio
async def test_evolved_optimizer_can_request_model_but_not_tools(app):
    source='''import json
def handle(p):
    if "model_result" not in p:
        return {"model_request":{"system":"Propose a bounded search policy","messages":[{"role":"user","content":"choose"}]},"state":{"stage":1}}
    return {"result":json.loads(p["model_result"]["text"])}
'''
    result=await EvolvedWorker(app.store,app.sandbox).converse({'kind':'python','source':source},{'operation':'search_config'},ScriptedProvider([Assistant(text='{"max_candidates":1}')]))
    assert result['max_candidates']==1
    bad='def handle(p):return {"model_request":{"system":"s","messages":[],"tools":["shell"]}}'
    with pytest.raises(RepoError,match='WORKER_CAPABILITY_DENIED'):
        await EvolvedWorker(app.store,app.sandbox).converse({'kind':'python','source':bad},{'operation':'search_config'},ScriptedProvider())

@pytest.mark.asyncio
async def test_meta_model_budget_includes_failed_candidates(app):
    vault=HoldoutVault(app.store);p=problem('budget');p['budget']['model_calls']=1
    ev=MetaEvaluator(app,vault,optimizer_fixture)
    result=await ev.episode(p,app.releases.active()['digest'],'unit',False)
    assert result['status']=='META_TOTAL_BUDGET' and result['all_model_calls']==1
    assert result['score'] is None
