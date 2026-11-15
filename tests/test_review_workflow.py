"""Offline contracts for the failures identified by review1.1/1.2."""
import json
from copy import deepcopy
import pytest
from repo_code.models import Assistant, Budget, ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.memory.models import MemoryProfile
from repo_code.context import groups
from repo_code.progress import runtime_feedback
from repo_code.task_progress import TaskProgress
from repo_code.history_archive import folded_archive
from repo_code.commands import git_inspections, no_match


def setup(tmp_path, memory=False, provider=None):
    source=tmp_path/'source';source.mkdir();(source/'a.py').write_text('value=0\n'*120)
    profile=MemoryProfile(mode='lexical_only',learning_enabled=False,max_tokens=16000) if memory else None
    app=RepoCode(tmp_path/'state',provider or ScriptedProvider([{'text':'Ready'}]),'local',memory_profile=profile)
    run=app.new_task('Implement then check the current request',source,authorize_write=True,authorize_process=True,
                     budget=Budget(model_calls=40,tool_calls=100,child_calls=0))
    async def process(rid,folder,argv,stdin,timeout,*args,**kwargs):
        check=any('pytest' in str(x) for x in argv)
        result={'stdout':'3 passed in 0.01s\n' if check else 'a.py\n','stderr':'','exit_code':0,
                'cancelled':False,'timeout':False,'output_snapshot':app.runtime.workspace(app.runtime.run(rid)).snapshot(),
                'environment_digest':app.sandbox.environment_digest}
        return result
    app.sandbox.run=process
    return app,run


async def tool(app,run,name,args,uid):
    call=ToolCall(id=uid,name=name,arguments=args)
    req=app.broker.register_call(run['id'],uid,call)
    app.store.append_message(run['id'],{'role':'assistant','content':'','tool_calls':[call.model_dump()]})
    receipt=await app.broker.submit(req);app.broker.append_result(req,receipt)
    return req,receipt


@pytest.mark.asyncio
@pytest.mark.parametrize('memory',[False,True])
async def test_same_version_validation_survives_noise_and_folding(tmp_path,memory):
    app,run=setup(tmp_path,memory)
    try:
        await tool(app,run,'read',{'path':'a.py'},'read')
        await tool(app,run,'edit',{'path':'a.py','old_text':'value=0\n'*120,'new_text':'value=1\n'*120},'write')
        await tool(app,run,'process.run',{'argv':['python','-m','pytest']},'check')
        for i in range(7):await tool(app,run,'process.run',{'argv':['ls','-'+'a'*(i+1)]},'noise'+str(i))
        fresh=app.runtime.run(run['id']);ledger=runtime_feedback(app.store,app.runtime,fresh)['work_ledger']
        assert ledger['changed_paths']==['a.py'] and ledger['last_write']['operation']
        assert ledger['verification']['argv_summary'].find('pytest')>=0
        assert ledger['verification']['result_summary']=='3 passed' and ledger['verification']['validity']=='CURRENT'
        if memory:
            _,state=app.memory.reducer.reduce(run['id'])
            assert any(x.get('verification') for x in state['host_observations'])
            system,messages,_=await app.memory.prepare_context(run['id'],[])
        else:system,messages=app.agent.context.build(run['id'],[],{'max_tokens':10000})
        assert '3 passed' in system and 'last_write' in system
        groups(messages)
        await tool(app,run,'edit',{'path':'a.py','old_text':'value=1\n'*120,'new_text':'value=2\n'*120},'next-write')
        ledger=runtime_feedback(app.store,app.runtime,app.runtime.run(run['id']))['work_ledger']
        assert ledger['verification']['validity']=='HISTORICAL'
        assert not app.agent.report(run['id'])['delivery']
    finally:app.close()


@pytest.mark.asyncio
async def test_read_coverage_allows_new_half_and_cross_tool_recovery(tmp_path):
    app,run=setup(tmp_path)
    try:
        tracker=TaskProgress(app.store,app.runtime)
        first=await tool(app,run,'read',{'path':'a.py','start':1,'lines':60},'first')
        assert tracker.batch(run['id'],'first',[first])['new_evidence']
        repeat=await tool(app,run,'read',{'path':'a.py','start':1,'lines':40},'repeat')
        assert not tracker.batch(run['id'],'repeat',[repeat])['new_evidence']
        second=await tool(app,run,'read',{'path':'a.py','start':61,'lines':60},'second')
        assert tracker.batch(run['id'],'second',[second])['new_evidence']
        cat=await tool(app,run,'process.run',{'argv':['cat','a.py']},'cat')
        assert not tracker.batch(run['id'],'cat',[cat])['new_evidence']
    finally:app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('memory',[False,True])
async def test_mixed_noise_does_not_reset_agent_stagnation(tmp_path,memory):
    replies=[{'tool_calls':[{'id':'r'+str(i),'name':'read','arguments':{'path':'a.py','lines':10}},
                           {'id':'ls'+str(i),'name':'process.run','arguments':{'argv':['ls','-'+'a'*(i%3+1)]}}]} for i in range(20)]
    app,run=setup(tmp_path,memory,ScriptedProvider(replies))
    try:
        result=await app.execute(run['id'])
        assert result['state']=='PARTIAL' and result['reason']=='NO_PROGRESS'
        assert result['budget_used']['model_calls']<15 and result['delivery'] is None
        assert not app.runtime.unresolved(run['id']);groups(app.store.messages(run['id']))
    finally:app.close()


@pytest.mark.asyncio
async def test_variant_checks_stop_partial_and_retain_candidate(tmp_path):
    replies=[{'tool_calls':[{'id':'write','name':'create','arguments':{'path':'fixed.py','content':'value=1\n'}}]}]
    replies.extend({'tool_calls':[{'id':'check'+str(i),'name':'process.run','arguments':{'argv':['pytest'],'timeout':10+i}}]} for i in range(20))
    app,run=setup(tmp_path,provider=ScriptedProvider(replies))
    try:
        result=await app.execute(run['id'])
        assert result['state']=='PARTIAL' and result['reason']=='STAGNATION_AFTER_WRITE'
        assert result['budget_used']['model_calls']<16 and result['delivery'] is None
        assert app.runtime.workspace(app.runtime.run(run['id'])).path('fixed.py').read_text()=='value=1\n'
        assert not app.runtime.unresolved(run['id']);groups(app.store.messages(run['id']))
    finally:app.close()


def test_archive_is_linear_readable_and_stable_per_group(tmp_path):
    app,run=setup(tmp_path)
    try:
        first=[{'role':'assistant','content':'','native_blocks':[{'encrypted_content':'OPAQUE_NATIVE_SENTINEL'}],
                'tool_calls':[{'id':'edit','name':'edit','arguments':{'path':'a.py','old_text':'0','new_text':'1'}}]},
               {'role':'tool','tool_call_id':'edit','content':json.dumps({'changed_paths':['a.py'],'inspection_progress':{'noise':'x'*2000}})}]
        raw=deepcopy(first);ref=folded_archive(app.store,run['id'],[first]);index=app.store.read_json(ref)
        group_ref=index['groups'][0]['group_artifact']
        details=app.store.read_blob(group_ref).decode()
        assert 'edit' in details and 'inspection_progress' not in details and 'OPAQUE_NATIVE_SENTINEL' not in details
        recovery=[{'role':'assistant','tool_calls':[{'id':'recover','name':'artifact.read','arguments':{'artifact':ref}}]},
                  {'role':'tool','tool_call_id':'recover','content':json.dumps({'source_artifact':ref,'content':details*100})}]
        later=app.store.read_json(folded_archive(app.store,run['id'],[first,recovery]))
        assert any(x['group_artifact']==group_ref for x in later['groups'])
        assert len(json.dumps(later))<2000 and first==raw
    finally:app.close()


@pytest.mark.asyncio
async def test_diff_is_scoped_cache_free_and_binary_aware(tmp_path):
    app,run=setup(tmp_path)
    try:
        assert (await tool(app,run,'workspace.diff',{},'empty'))[1]['result']['changed_paths']==[]
        await tool(app,run,'create',{'path':'fixed.py','content':'answer=42\n'},'create')
        ws=app.runtime.workspace(app.runtime.run(run['id']))
        (ws.root/'__pycache__').mkdir();(ws.root/'__pycache__'/'noise.pyc').write_bytes(b'noise')
        (ws.root/'binary').write_bytes(b'\0\xff')
        result=(await tool(app,run,'workspace.diff',{},'diff'))[1]['result']
        assert result['changed_paths']==['binary','fixed.py'] and '+answer=42' in result['diff']
        assert result['files'][0]['text_diff']=='BINARY_OR_LARGE_FILE_METADATA_ONLY'
        denied=(await tool(app,run,'workspace.diff',{'path':'../outside'},'escape'))[1]['result']
        assert denied['is_error'] and denied['code']=='PATH_DENIED'
    finally:app.close()


@pytest.mark.asyncio
async def test_new_diff_page_is_progress_but_repeat_and_reworded_plan_are_not(tmp_path):
    app,run=setup(tmp_path)
    try:
        await tool(app,run,'create',{'path':'long.py','content':'x'*60000+'\n'},'write')
        tracker=TaskProgress(app.store,app.runtime)
        page=await tool(app,run,'workspace.diff',{'path':'long.py','length':1000},'first')
        assert tracker.batch(run['id'],'first',[page])['new_evidence']
        page=await tool(app,run,'workspace.diff',{'path':'long.py','offset':1000,'length':1000},'second')
        assert tracker.batch(run['id'],'second',[page])['new_evidence']
        page=await tool(app,run,'workspace.diff',{'path':'long.py','length':500},'repeat')
        assert not tracker.batch(run['id'],'repeat',[page])['new_evidence']
        for i in range(2):
            plan=await tool(app,run,'plan.update',{'steps':['reworded plan '+str(i)]},'plan'+str(i))
            assert tracker.batch(run['id'],'plan'+str(i),[plan])['new_evidence'] is (i==0)
    finally:app.close()


@pytest.mark.asyncio
async def test_flaky_retry_allowance_is_bounded_across_timeout_variants(tmp_path):
    app,run=setup(tmp_path)
    try:
        tracker=TaskProgress(app.store,app.runtime)
        for i in range(4):
            pair=await tool(app,run,'process.run',{'argv':['pytest'],'timeout':10+i,'retry_reason':'External service restarted attempt '+str(i)},str(i))
            progress=tracker.batch(run['id'],str(i),[pair])
            assert progress['new_evidence'] is (i<2)
    finally:app.close()


@pytest.mark.asyncio
async def test_swe_git_feedback_and_tool_capabilities(tmp_path):
    app,run=setup(tmp_path)
    try:
        app.sandbox.mode='swebench'
        names=app.broker.available_tools(app.runtime.run(run['id']))
        assert not names & {'mcp.call','mcp.discover','delegate','process.start','process.status','process.stop'}
        assert {'process.run','workspace.diff','edit'}<=names
        for i,argv in enumerate([['git','diff'],['bash','-lc','git status && git diff --stat']]):
            result=(await tool(app,run,'process.run',{'argv':argv},str(i)))[1]['result']
            assert result['code']=='GIT_METADATA_UNAVAILABLE' and not result['executed']
    finally:app.close()


@pytest.mark.parametrize('argv,expected',[
    (['git','diff'],['diff']),(['bash','-lc','git status && git diff --stat'],['status','diff']),
    (['echo','git diff'],[]),(['python','-c','print("git diff")'],[]),(['git','-C','/tmp','--no-pager','status'],['status'])])
def test_git_argv_is_structural(argv,expected):assert git_inspections(argv)==expected


def test_grep_no_match_is_not_a_test_failure():
    assert no_match(['grep','missing','a.py'],{'exit_code':1,'stdout':'','stderr':''})
    assert not no_match(['pytest'],{'exit_code':1,'stdout':'','stderr':''})
    assert not no_match(['grep','[','a.py'],{'exit_code':2,'stderr':'invalid pattern'})
