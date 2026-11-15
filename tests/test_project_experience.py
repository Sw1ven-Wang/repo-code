import pytest
from repo_code.util import RepoError
from test_session_experience import prepared,route

@pytest.mark.asyncio
async def test_project_reuse_new_run_restart_quota_and_revocation(tmp_path):
    app,flow,d,s,sid,gid,run,result=await prepared(tmp_path)
    try:
        active=app.releases.active()
        project=s.create_project(d['scope'],tmp_path/'followup',max_tasks=2)
        grant=s.admit(project,result['contract_ref'],result['candidate'])
        r=app.new_task('Inspect transformation metadata',tmp_path/'followup',session_id=project,task_family=d['task_family'])
        assert route(flow,r,d)['guidance'][0]['authority']=='PROJECT_CONDITIONAL_ADVISORY'
        from repo_code.service import RepoCode
        from repo_code.providers import ScriptedProvider
        reopened=RepoCode(app.store.root,ScriptedProvider(),sandbox='local')
        try:
            assert reopened.session_experiences().entries(r['id'])
            r2=reopened.new_task('Inspect',tmp_path/'followup',session_id=project,task_family=d['task_family'])
            assert reopened.session_experiences().entries(r2['id'])
        finally:reopened.close()
        assert s.create_project(d['scope'],tmp_path/'followup',max_tasks=2)==project
        with pytest.raises(RepoError,match='REVIEW_REQUIRED'):
            app.new_task('Inspect',tmp_path/'followup',session_id=project,task_family=d['task_family'])
        other=tmp_path/'other';other.mkdir()
        with pytest.raises(RepoError,match='WORKSPACE_MISMATCH'):
            app.new_task('Inspect',other,session_id=project,task_family=d['task_family'])
        s.revoke(grant,'Observed counterexample')
        assert not s.entries(r['id'])
        assert s.admit(project,result['contract_ref'],result['candidate'])==grant
        assert not s.entries(r['id']) and app.releases.active()==active
    finally:app.close()
