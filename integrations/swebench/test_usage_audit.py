"""Offline post-response accounting through the actual API wrapper."""
import pytest
from memory_ad import AuditedProvider
from repo_code.providers import OpenAIProvider
from repo_code.models import Assistant
from repo_code.util import RepoError
from memory_ad import hash_files


@pytest.mark.asyncio
@pytest.mark.parametrize('inputs,outputs,status,hard',[(59415,87,'SAFETY_MARGIN_USED',False),
    (63990,11,'HARD_WINDOW_EXCEEDED',True)])
async def test_completed_response_not_rejected_for_unused_safety_reserve(tmp_path,monkeypatch,inputs,outputs,status,hard):
    async def reply(*args,**kwargs):return Assistant(text='valid',usage={'prompt_tokens':inputs,'completion_tokens':outputs,'total_tokens':inputs+outputs})
    monkeypatch.setattr(OpenAIProvider,'complete',reply)
    p=AuditedProvider(tmp_path,client=object(),model='deepseek-flash',base_url='https://api.deepseek.com',max_tokens=4096)
    if hard:
        with pytest.raises(RepoError) as e:await p.complete('',[],[])
        assert e.value.code=='ACTUAL_CONTEXT_BUDGET_EXCEEDED'
    else:
        result=await p.complete('',[],[])
        assert result.text=='valid' and p.halt is None
    record=p.records[0]
    assert record['usage']['prompt_tokens']==inputs
    assert record['context_audit']['status']==status
    assert record['context_audit']['hard_violation']==hard


def test_freeze_excludes_mutable_test_cache_and_pins_source(tmp_path):
    source=tmp_path/'code.py';source.write_text('version=1')
    cache=tmp_path/'.pytest_cache';cache.mkdir();(cache/'nodeids').write_text('old')
    before=hash_files(tmp_path);(cache/'nodeids').write_text('new')
    assert hash_files(tmp_path)==before
    source.write_text('version=2')
    assert hash_files(tmp_path)!=before
