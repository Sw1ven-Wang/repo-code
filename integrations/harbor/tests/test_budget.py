import json
from types import SimpleNamespace as NS

import pytest

from harbor_repo_code.agent import BudgetedDeepSeek
from repo_code.util import RepoError,ProviderNotDispatched


def client(finish='stop',fail=False):
    calls=[]
    class Stream:
        async def __aiter__(self):
            yield NS(choices=[NS(finish_reason=finish,delta=NS(content='done',tool_calls=None))],
                usage=NS(prompt_tokens=5,completion_tokens=3,total_tokens=8))
            if fail:raise RuntimeError('offline transport failure after usage')
        async def close(self):pass
    async def create(**kwargs):calls.append(kwargs);return Stream()
    return NS(chat=NS(completions=NS(create=create))),calls


@pytest.mark.asyncio
async def test_budget_reserves_before_any_provider_call(tmp_path):
    sdk,calls=client()
    provider=BudgetedDeepSeek(10,tmp_path/'usage.json',model='test',client=sdk,max_tokens=20)
    with pytest.raises(ProviderNotDispatched) as exc:
        await provider.complete('s',[],[])
    assert calls==[]
    assert exc.value.code=='TOTAL_TOKEN_BUDGET_EXHAUSTED'
    assert exc.value.provider_details['dispatch_status']=='NOT_DISPATCHED'
    assert provider.records==[] and provider.used_tokens==0


@pytest.mark.asyncio
async def test_success_settles_raw_usage_once(tmp_path):
    sdk,calls=client()
    provider=BudgetedDeepSeek(20000,tmp_path/'usage.json',model='test',client=sdk,max_tokens=20)
    await provider.complete('s',[],[])
    assert provider.used_tokens==8 and len(calls)==1
    assert json.loads((tmp_path/'usage.json').read_text())['calls'][0]['usage']['total_tokens']==8


@pytest.mark.asyncio
async def test_incomplete_response_still_preserves_billable_usage(tmp_path):
    sdk,_=client('length')
    provider=BudgetedDeepSeek(20000,tmp_path/'usage.json',model='test',client=sdk,max_tokens=20)
    reply=await provider.complete('s',[],[])
    # Current native protocol returns truncation + Usage for CodingAgent to
    # reject without admitting partial tool effects; this is a completed API call.
    assert reply.stop_reason=='length' and not reply.tool_calls and not reply.text
    record=json.loads((tmp_path/'usage.json').read_text())
    assert record['used_tokens']==8
    assert record['calls'][0]['status']=='COMPLETED' and record['calls'][0]['usage']['total_tokens']==8


@pytest.mark.asyncio
async def test_transport_failure_preserves_known_billable_usage(tmp_path):
    sdk,_=client(finish=None,fail=True)
    provider=BudgetedDeepSeek(20000,tmp_path/'usage.json',model='test',client=sdk,max_tokens=20)
    with pytest.raises(RepoError,match='offline transport failure'):await provider.complete('s',[],[])
    record=json.loads((tmp_path/'usage.json').read_text())
    assert record['used_tokens']==8
    assert record['calls'][0]['status']=='FAILED' and record['calls'][0]['usage']['total_tokens']==8
