import pytest
from repo_code.context import ContextEngine,groups
from repo_code.knowledge import Knowledge,project_scope
from repo_code.util import RepoError,digest
from repo_code.governance import Lineage
from conftest import new_run

@pytest.mark.parametrize('history',[
    [{'role':'tool','tool_call_id':'x','content':'bad'}],
    [{'role':'assistant','content':'','tool_calls':[{'id':'x'}]}],
    [{'role':'assistant','tool_calls':[{'id':'x'},{'id':'x'}]},{'role':'tool','tool_call_id':'x'},{'role':'tool','tool_call_id':'x'}],
    [{'role':'assistant','tool_calls':[{'id':'x'}]},{'role':'user','content':'interrupt'}],
])
def test_incomplete_groups_fail_closed(history):
    with pytest.raises(RepoError):groups(history)

def test_group_calls_with_results():
    x=[{'role':'assistant','tool_calls':[{'id':'a'},{'id':'b'}]},{'role':'tool','tool_call_id':'b'},{'role':'tool','tool_call_id':'a'}]
    assert groups(x)==[x]

def test_knowledge_pin_is_not_live(app,source,contract):
    k=app.runtime.knowledge;a=k.draft('memory','answer method','Python answer check',project_scope(source),['user-feedback']);k.activate(a['id'],'operator')
    run=new_run(app,source,contract);m=run['data']['manifest']
    b=k.draft('memory','answer new','New Python answer policy',project_scope(source),['user-feedback']);k.activate(b['id'],'operator')
    old=k.search(m['knowledge_snapshot_digest'],m['knowledge_index_digest'],'Python')
    assert [x['id'] for x in old]==[a['id']]
    current=k.pin(project_scope(source));assert len(k.search(*current,'Python'))==2

def test_revoke_overrides_pinned_snapshot(app,source,contract):
    k=app.runtime.knowledge;a=k.draft('skill','python answer','Use real checks',project_scope(source),['t']);k.activate(a['id'],'operator')
    run=new_run(app,source,contract);k.revoke(a['id'])
    with pytest.raises(RepoError,match='KNOWLEDGE_REVOKED'):app.runtime.assert_executable(app.runtime.run(run['id']))

def test_index_mismatch_rejected(app):
    k=app.runtime.knowledge;a=k.pin('a');b=k.pin('b')
    with pytest.raises(RepoError,match='KNOWLEDGE_INDEX_MISMATCH'):k.search(a[0],b[1],'query')

def test_memory_scope_and_hidden_eval_personal_exclusion(app):
    k=app.runtime.knowledge
    for scope in ['user','project','other']:
        item=k.draft('memory','python '+scope,'python','%s'%scope,['feedback']);k.activate(item['id'],'operator')
    assert len(k.search(*k.pin('project'),'python'))==2
    assert k.search(*k.pin('eval:hidden',include_user=False),'python')==[]

def test_merge_needs_compatible_scope_and_review(app):
    k=app.runtime.knowledge;a=k.draft('skill','a','content','one',['a']);b=k.draft('skill','b','content','two',['b'])
    with pytest.raises(RepoError,match='INCOMPATIBLE_SKILL_SCOPE'):k.merge([a['id'],b['id']],'merged','body','operator')
    c=k.draft('skill','c','content','one',['c']);new=k.merge([a['id'],c['id']],'merged','body','operator')
    assert new['status']=='CANDIDATE'

def test_fold_preserves_raw_history_and_archive_acl(app,source,contract):
    run=new_run(app,source,contract)
    for i in range(20):
        app.store.append_message(run['id'],{'role':'assistant','content':'','tool_calls':[{'id':str(i),'name':'read','arguments':{}}]})
        app.store.append_message(run['id'],{'role':'tool','tool_call_id':str(i),'content':'x'*2000})
    before=len(app.store.messages(run['id']))
    system,msgs=ContextEngine(app.store,app.runtime).build(run['id'],[],{'max_tokens':6000,'reserve_tokens':1000})
    projection=app.store.get('projection',run['id'])
    assert projection['folded_groups']>0 and projection['summary_ref']
    assert len(app.store.messages(run['id']))==before
    groups(msgs)
    assert app.store.get('run_artifact',digest([run['id'],projection['summary_ref']]))
    assert len(app.store.read_json(projection['summary_ref']))>0

def test_knowledge_lineage_carries_holdout_exposure(app):
    line=Lineage(app.store);line.expose('source','cohort-a','family-a')
    k=app.runtime.knowledge;a=k.draft('skill','s','b','project',['source']);k.activate(a['id'],'operator')
    snapshot,index=k.pin('project')
    assert line.is_exposed(snapshot,'cohort-a','family-a')
