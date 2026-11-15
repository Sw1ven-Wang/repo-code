"""Known incompatibility must dominate unknown or historical metadata."""
import pytest
from repo_code.memory.retrieval import applicability
from repo_code.util import RepoError
from test_memory_v2_contracts import memory_app,proposal,activate,state


@pytest.mark.parametrize('historical',[False,True])
def test_RET_02_known_version_mismatch_dominates_missing_precondition(memory_app,historical):
    app,run,_=memory_app;k,s=app.memory.reducer.reduce(run['id']);i=app.memory.reducer.intent(run['id'],k,s)
    i=i.model_copy(update={'environment':{'django':'5'}})
    value={'preconditions':{'python':'3.13','django':'4'},'validity':'HISTORICAL' if historical else 'CURRENT'}
    assert applicability(value,i,{})==('FALSE','DEPENDENCY_MISMATCH')


def test_known_file_mismatch_dominates_an_unobserved_file(memory_app):
    app,run,_=memory_app;k,s=app.memory.reducer.reduce(run['id']);i=app.memory.reducer.intent(run['id'],k,s)
    assert applicability({'file_dependencies':{'missing':'a','present':'old'}},i,{'present':'new'})==('FALSE','HISTORICAL_FILE_VERSION')


@pytest.mark.asyncio
async def test_zero_budget_does_not_fall_back_to_default_or_cached_hits(memory_app):
    app,run,source=memory_app;c=proposal(app,run,'CSV_EMPTY_HEADER diagnostic');activate(app,c)
    later=app.new_task('CSV_EMPTY_HEADER',source)
    hits,_=await app.memory.search(later['id'],'CSV_EMPTY_HEADER',token_budget=3000);assert hits
    hits,_=await app.memory.search(later['id'],'CSV_EMPTY_HEADER',token_budget=0);assert not hits


def test_RET_04_verify_and_resume_have_explicit_intents(memory_app):
    app,run,_=memory_app
    app.store.put('plan',run['id'],{'steps':['Verify the full test suite']})
    k,s=app.memory.reducer.reduce(run['id']);assert app.memory.reducer.intent(run['id'],k,s).phase=='VERIFY'
    app.runtime.recover(run['id']);app.runtime.resume(run['id'])
    k,s=app.memory.reducer.reduce(run['id']);assert app.memory.reducer.intent(run['id'],k,s).phase=='RESUME'


@pytest.mark.asyncio
async def test_invalid_host_limits_are_rejected(memory_app):
    app,run,_=memory_app
    with pytest.raises(RepoError,match='INVALID_MEMORY_POLICY'):await app.memory.search(run['id'],'query',limit=-1)


def test_global_goal_mentioning_tests_does_not_mean_verification_phase(memory_app):
    app,_,source=memory_app;run=app.new_task('Implement the feature and run tests',source)
    k,s=app.memory.reducer.reduce(run['id'])
    assert app.memory.reducer.intent(run['id'],k,s).phase=='PLAN'
