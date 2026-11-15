"""Offline protocol diagnostics and stream ownership; no real inference."""
import asyncio
import hashlib
import json
from types import SimpleNamespace as NS

import pytest
from repo_code.chatgpt_provider import ChatGPTPlanProvider
from repo_code.util import RepoError, ProviderNotDispatched
from test_chatgpt_provider import API, Stream, done, plan, TOOLS


@pytest.mark.asyncio
async def test_sse_error_keeps_transport_id_without_raw_message_or_headers():
    secret = 'Bearer never-log-this-token'
    stream = Stream([{'type': 'error', 'code': 'invalid_prompt', 'message': secret}])
    stream.response = NS(status_code=200, headers={'x-request-id': 'req_safe_123', 'authorization': secret})
    p, api = plan(stream)
    with pytest.raises(RepoError) as caught:
        await p.complete('s', [], TOOLS)
    error = caught.value
    assert error.code == 'invalid_prompt'
    assert not isinstance(error, ProviderNotDispatched)
    assert error.provider_details['request_id'] == 'req_safe_123'
    assert error.provider_details['http_status_code'] == 200
    assert error.provider_details['message_sha256'] == hashlib.sha256(secret.encode()).hexdigest()
    assert error.usage == {}  # Missing Usage is never proof of zero use.
    assert 'never-log-this-token' not in str(error) + json.dumps(error.provider_details)
    assert len(api.requests) == 1 and stream.closed


@pytest.mark.asyncio
async def test_failed_terminal_keeps_http_metadata_and_real_usage():
    event = done([], 'failed', {'input_tokens': 31, 'output_tokens': 2})
    event['response']['error'] = {'code': 'invalid_prompt', 'message': 'do not publish raw text'}
    stream = Stream([event]); stream.response = NS(status_code=200, headers={'x-request-id': 'req_failed'})
    p, _ = plan(stream)
    with pytest.raises(RepoError) as caught:
        await p.complete('s', [], [])
    assert caught.value.provider_details['request_id'] == 'req_failed'
    assert caught.value.provider_details['terminal_event'] == 'response.failed'
    assert caught.value.usage == {'input_tokens': 31, 'output_tokens': 2}


def test_safe_error_records_indexed_param_but_rejects_arbitrary_text():
    safe = ChatGPTPlanProvider._safe_error(NS(body={'error': {'code': 'invalid_prompt',
        'type': 'invalid_request_error', 'param': 'input[16].call_id', 'message': 'private'}}, request_id='req_ok'))
    assert safe.provider_details['api_param'] == 'input[16].call_id'
    unsafe = ChatGPTPlanProvider._safe_error(NS(body={'error': {'type': 'invalid_request_error',
        'param': 'Bearer private', 'message': 'private'}}, request_id='Bearer private'))
    assert 'api_param' not in unsafe.provider_details and unsafe.provider_details['request_id'] is None


class OwnedIterator:
    def __init__(self, events, order):
        self.events, self.order, self.i = events, order, 0
    def __aiter__(self): return self
    async def __anext__(self):
        if self.i >= len(self.events): raise StopAsyncIteration
        value = self.events[self.i]; self.i += 1; return value
    async def aclose(self): self.order.append('iterator')


class OwnedStream:
    def __init__(self, events):
        self.order = []; self.iterator = OwnedIterator(events, self.order)
    def __aiter__(self): return self.iterator
    async def close(self): self.order.append('transport')


@pytest.mark.asyncio
async def test_iterator_closed_before_transport_on_failed_event():
    stream = OwnedStream([{'type': 'error', 'code': 'invalid_prompt'}])
    p, _ = plan(stream)
    with pytest.raises(RepoError): await p.complete('s', [], [])
    assert stream.order == ['iterator', 'transport']


@pytest.mark.asyncio
async def test_iterator_closed_before_transport_on_success():
    stream = OwnedStream([done()]); p, _ = plan(stream)
    assert (await p.complete('s', [], [])).text == 'Acknowledged.'
    assert stream.order == ['iterator', 'transport']


@pytest.mark.asyncio
async def test_cancelled_next_closes_iterator_and_preserves_cancellation():
    entered = asyncio.Event()
    class Waiting(OwnedIterator):
        async def __anext__(self):
            entered.set(); await asyncio.Event().wait()
    stream = OwnedStream([]); stream.iterator = Waiting([], stream.order)
    p, _ = plan(stream); task = asyncio.create_task(p.complete('s', [], []))
    await entered.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert stream.order == ['iterator', 'transport']


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_replace_primary_api_failure():
    class BrokenClose(OwnedStream):
        async def close(self): raise RuntimeError('cleanup failed')
    p, _ = plan(BrokenClose([{'type': 'error', 'code': 'invalid_prompt'}]))
    with pytest.raises(RepoError) as caught: await p.complete('s', [], [])
    assert caught.value.code == 'invalid_prompt'


@pytest.mark.asyncio
async def test_real_sdk_sse_failure_is_sanitized_and_fails_once():
    import httpx
    from openai import AsyncOpenAI
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        event = {'type': 'response.failed', 'response': {'id': 'resp_offline', 'status': 'failed',
                 'error': {'code': 'invalid_prompt', 'message': 'private response message'}}}
        body = 'event: response.failed\ndata: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n'
        return httpx.Response(200, headers={'content-type': 'text/event-stream', 'x-request-id': 'req_sdk_fixture'}, text=body)
    client = AsyncOpenAI(api_key='offline-test-fixture', base_url='https://fixture.invalid/v1', max_retries=0,
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        p = ChatGPTPlanProvider('fixed-model', auth=NS(), client=client)
        with pytest.raises(RepoError) as caught: await p.complete('s', [], [])
        assert caught.value.provider_details['request_id'] == 'req_sdk_fixture'
        assert caught.value.code == 'invalid_prompt' and caught.value.usage == {}
        assert len(calls) == 1
    finally: await client.close()


@pytest.mark.asyncio
async def test_protocol_failures_are_not_automatically_retried():
    p, api = plan(Stream([{'type': 'error', 'code': 'invalid_prompt'}]), Stream([done()]))
    with pytest.raises(RepoError): await p.complete('s', [], [])
    assert len(api.requests) == 1 and len(api.streams) == 1
