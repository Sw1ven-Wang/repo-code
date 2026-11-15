import pytest

from repo_code.context import ContextEngine,estimate_tokens,groups
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.util import RepoError


def make_history(tmp_path):
    source=tmp_path/'source';source.mkdir()
    app=RepoCode(tmp_path/'host',ScriptedProvider(),sandbox='local')
    run=app.new_task('Long public specification: '+'x'*4500,source)
    for i in range(20):
        app.store.append_message(run['id'],{'role':'assistant','content':'','reasoning_content':'r'*100,
            'tool_calls':[{'id':str(i),'name':'read','arguments':{}}]})
        app.store.append_message(run['id'],{'role':'tool','tool_call_id':str(i),'content':'{"line":"escaped\\n"}\n'*120})
    return app,run


@pytest.mark.parametrize('guidance',['','\nApproved guidance: '+'q'*1800])
def test_fold_accounts_for_long_summary_and_final_guidance(tmp_path,guidance):
    app,run=make_history(tmp_path)
    original=app.store.messages(run['id'])
    system,messages=ContextEngine(app.store,app.runtime).build(run['id'],[],
        {'max_tokens':6000,'reserve_tokens':1024},system_suffix=guidance)
    assert estimate_tokens([system,messages,[]])+1024<=6000
    assert system.endswith(guidance)
    assert app.store.get('projection',run['id'])['folded_groups']>0
    assert app.store.messages(run['id'])==original
    groups(messages)


def test_oversized_complete_tool_group_still_fails_closed(tmp_path):
    app,run=make_history(tmp_path)
    app.store.append_message(run['id'],{'role':'assistant','tool_calls':[{'id':'huge','name':'read','arguments':{}}]})
    app.store.append_message(run['id'],{'role':'tool','tool_call_id':'huge','content':'x'*50000})
    with pytest.raises(RepoError,match='last complete group'):
        ContextEngine(app.store,app.runtime).build(run['id'],[],{'max_tokens':6000,'reserve_tokens':1024})
