"""Trusted non-dispatch is explicit; an empty Usage or budget code proves nothing."""
import pytest
from repo_code.models import Assistant
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.util import RepoError,ProviderNotDispatched
from repo_code.memory.models import MemoryProfile


@pytest.mark.asyncio
@pytest.mark.parametrize('proved',[False,True])
async def test_main_call_host_denial_vs_unknown_provider_failure(tmp_path,proved):
    source=tmp_path/'source';source.mkdir()
    error=(ProviderNotDispatched if proved else RepoError)('BUDGET_EXHAUSTED')
    app=RepoCode(tmp_path/'state',ScriptedProvider([error]),'local',memory_profile=MemoryProfile(mode='lexical_only',learning_enabled=False))
    try:
        run=app.new_task('Do not dispatch if admission fails',source)
        result=await app.execute(run['id'])
        row=app.memory.repo.values('memory_budget')[0]
        assert row['state']==('NOT_DISPATCHED' if proved else 'USAGE_UNKNOWN')
        assert (row['charged_tokens']==0) is proved
        assert result['memory_cost']['usage_complete'] is proved
        assert result['memory_cost']['not_dispatched_reservations']==int(proved)
        assert result['state']=='PARTIAL' and result['reason']=='BUDGET_EXHAUSTED'
        assert not app.runtime.unresolved(run['id'])
    finally:app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('purpose',['compaction','consolidation'])
@pytest.mark.parametrize('proved',[False,True])
async def test_auxiliary_call_uses_same_dispatch_proof(tmp_path,purpose,proved):
    source=tmp_path/'source';source.mkdir()
    app=RepoCode(tmp_path/'state',ScriptedProvider(),'local',memory_profile=MemoryProfile(mode='lexical_only'))
    try:
        run=app.new_task('Preserve public behavior',source);rid=run['id']
        error=(ProviderNotDispatched if proved else RepoError)('BUDGET_EXHAUSTED')
        provider=ScriptedProvider([error])
        if purpose=='compaction':
            app.memory.compactor.provider=provider
            records=app.memory.history.records(rid);_,state=app.memory.reducer.reduce(rid)
            with pytest.raises(RepoError,match='BUDGET_EXHAUSTED'):
                await app.memory.compactor.compact(rid,records[-1]['seq'],state)
        else:
            # The consolidation extractor has the same native reservation boundary.
            with pytest.raises(RepoError,match='BUDGET_EXHAUSTED'):
                await app.memory.consolidator.extract(rid,provider)
        row=app.memory.repo.values('memory_budget')[0]
        assert row['purpose']==purpose
        assert row['state']==('NOT_DISPATCHED' if proved else 'USAGE_UNKNOWN')
        assert (row['charged_tokens']==0) is proved
    finally:app.close()


@pytest.mark.asyncio
async def test_compaction_host_budget_rejection_is_not_a_summary_failure(tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir()
    app=RepoCode(tmp_path/'state',ScriptedProvider(),'local',memory_profile=MemoryProfile(mode='lexical_only',learning_enabled=False))
    try:
        run=app.new_task('Keep the request',source);rid=run['id']
        for i in range(20):app.store.append_message(rid,{'role':'assistant','content':'mandatory historical proposal '*600})
        calls=[]
        async def denied(*args):
            calls.append(1);raise ProviderNotDispatched('BUDGET_EXHAUSTED','Cannot reserve next call')
        monkeypatch.setattr(app.memory.compactor,'compact',denied)
        with pytest.raises(ProviderNotDispatched,match='Cannot reserve'):
            await app.memory.prepare_context(rid,[],policy={'max_tokens':8000})
        assert len(calls)==1
        assert not any(x.get('fallback') for x in app.memory.repo.values('memory_control'))
    finally:app.close()
