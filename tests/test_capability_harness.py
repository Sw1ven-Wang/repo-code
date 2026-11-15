"""Paid-call guards and total request timeout for the live benchmark harness."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from repo_code.util import RepoError

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('capability_benchmark',ROOT/'scripts/capability_benchmark.py')
harness=importlib.util.module_from_spec(spec);spec.loader.exec_module(harness)


def test_live_execution_requires_explicit_flag(monkeypatch,tmp_path):
    monkeypatch.setattr(harness,'OpenAIProvider',lambda *a,**kw:pytest.fail('must not construct a paid client'))
    with pytest.raises(SystemExit) as exc:
        harness.main(['--state',str(tmp_path/'state'),'--output',str(tmp_path/'out')])
    assert exc.value.code==2 and not (tmp_path/'state').exists()


@pytest.mark.asyncio
async def test_streaming_request_cannot_exceed_total_wall_limit():
    async def pending(*args,**kwargs):await asyncio.sleep(30)
    report={'attempted_adapter_calls':0,'usage_by_call':[]};logs=[]
    inner=NS(model='m',base_url='https://relay.invalid/v1',reasoning_effort='medium',complete=pending)
    provider=harness.RecordedProvider(inner,report,lambda:None,logs.append,0.01)
    with pytest.raises(RepoError) as exc:await provider.complete('s',[],[])
    assert exc.value.code=='MODEL_REQUEST_WALL_TIMEOUT'
    assert report['attempted_adapter_calls']==1 and not report['usage_by_call']
    assert logs[-1]['event']=='model.failed'


@pytest.mark.asyncio
async def test_remote_diagnostic_redacts_exact_nonstandard_key():
    key='a-private-token-that-does-not-use-an-sk-prefix'
    class RemoteError(Exception):
        status_code=400
        body={'error':{'message':'Invalid account '+key,'code':'upstream_invalid'}}
    async def failed(*args,**kwargs):
        try:raise RemoteError()
        except RemoteError:raise RepoError('MODEL_ERROR') from None
    report={'attempted_adapter_calls':0,'usage_by_call':[]};logs=[]
    inner=NS(model='m',base_url=None,reasoning_effort='medium',complete=failed,client=NS(api_key=key))
    provider=harness.RecordedProvider(inner,report,lambda:None,logs.append,1)
    with pytest.raises(RepoError):await provider.complete('s',[],[])
    assert key not in str(logs) and logs[-1]['http_status']==400
    assert '[REDACTED]' in logs[-1]['upstream_message']
