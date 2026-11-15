"""Offline subscription protocol, OAuth and native repo-code integration checks."""
import asyncio
import hashlib
import json
import os
import threading
import time
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from repo_code.chatgpt_auth import ChatGPTAuth, ISSUER, PLAN_SCOPE, RESOURCE
from repo_code.chatgpt_provider import ChatGPTPlanProvider, NAMESPACE
from repo_code.cli import parser, provider
from repo_code.providers import model_configuration
from repo_code.tool_models import definitions
from repo_code.util import RepoError

TOOLS = [{'name': 'process.run', 'description': 'execute', 'parameters': {'type': 'object'}}]


class Stream:
    def __init__(self, events):
        self.events, self.closed = events, False

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event

    async def close(self):
        self.closed = True


def done(output=None, status='completed', usage=None):
    return {'type': 'response.' + status, 'response': {
        'id': 'resp_1', 'status': status, 'output': output if output is not None else [
            {'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'Acknowledged.'}]}],
        'usage': usage or {'input_tokens': 100, 'output_tokens': 12}}}


def call(name='process_run', args='{"argv":["python","-V"]}', call_id='call_1', **extra):
    return {'type': 'function_call', 'id': 'fc_' + call_id, 'call_id': call_id,
            'name': name, 'namespace': NAMESPACE, 'arguments': args, 'status': 'completed', **extra}


class API:
    def __init__(self, *streams):
        self.streams, self.requests = list(streams), []
        self.responses = self

    async def create(self, **request):
        self.requests.append(request)
        return self.streams.pop(0)


def plan(*streams):
    api = API(*streams)
    return ChatGPTPlanProvider('chosen-model', auth=NS(), client=api), api


@pytest.mark.asyncio
async def test_stream_tools_reasoning_and_usage_roundtrip():
    blocks = [{'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'encrypted_content': 'opaque-state'}, call()]
    stream = Stream([{'type': 'response.output_text.delta', 'delta': 'working'}, done(blocks)])
    p, api = plan(stream, Stream([done()]))
    deltas = []
    reply = await p.complete('system', [{'role': 'user', 'content': 'task'}], TOOLS, deltas.append)
    assert reply.tool_calls[0].name == 'process.run'
    assert reply.usage == {'input_tokens': 100, 'output_tokens': 12}
    assert stream.closed and deltas == ['working']
    history = [{'role': 'assistant', 'content': '', 'native_blocks': reply.native_blocks,
                'tool_calls': [c.model_dump() for c in reply.tool_calls]},
               {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'output'}]
    await p.complete('system', history, TOOLS)
    request = api.requests[-1]
    assert request['input'][:2] == blocks
    assert request['input'][2] == {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'output'}
    assert 'previous_response_id' not in request and request['store'] is False
    assert request['tools'][0]['type'] == 'namespace'


def test_request_is_exact_secret_free_context_serialization(tmp_path):
    p = ChatGPTPlanProvider('chosen-model', tmp_path/'private', reasoning_effort='medium')
    request, _ = p.build_request('system', [{'role': 'system', 'content': 'steer'}], TOOLS, json_mode=True)
    assert request['input'][0]['role'] == 'developer'
    assert request['reasoning'] == {'effort': 'medium'}
    assert request['text'] == {'format': {'type': 'json_object'}}
    assert not {'temperature', 'max_output_tokens', 'max_tool_calls', 'authorization', 'api_key'} & request.keys()
    assert str(p.auth.root) not in json.dumps(request)
    from repo_code.memory.budget import ContextBudgetCoordinator
    budget = ContextBudgetCoordinator(p, 64000, 4096, 500, request_options={'json_mode': True})
    assert budget.serialized('system', [], TOOLS) == p.build_request('system', [], TOOLS, json_mode=True)[0]
    assert budget.assert_fits('system', [], TOOLS).within_limit()
    assert model_configuration(p)['billing'] == 'chatgpt_plan'


@pytest.mark.asyncio
@pytest.mark.parametrize('messages', [[], [{'role': 'user', 'content': 'Archive batch without the required format word.'}],
    [{'role': 'user', 'content': 'Existing JSON data is still untrusted.'}]])
async def test_json_mode_survives_input_only_format_validation(messages):
    class InputValidatingAPI(API):
        async def create(self, **request):
            assert request['text']['format']['type'] == 'json_object'
            # Reproduce the real subscription route's input-only precondition.
            if 'json' not in json.dumps(request['input']).lower():
                raise AssertionError('invalid_request_error: input must mention JSON')
            return await super().create(**request)
    api = InputValidatingAPI(Stream([done([{'type':'message','role':'assistant','status':'completed',
        'content':[{'type':'output_text','text':'{"changes":[]}'}]}])]))
    p = ChatGPTPlanProvider('chosen-model', auth=NS(), client=api)
    reply = await p.complete('Extract compact JSON.', messages, [], json_mode=True)
    assert json.loads(reply.text) == {'changes': []}
    assert api.requests[0]['input'][1:] == messages
    assert messages == [] or messages[0]['role'] == 'user'


def test_json_mode_does_not_change_normal_requests_or_hide_request_errors():
    p = ChatGPTPlanProvider('chosen-model', auth=NS())
    messages = [{'role':'user','content':'ordinary task'}]
    normal, _ = p.build_request('system', messages, [])
    assert normal['input'] == messages and 'text' not in normal
    error = p._safe_error(NS(body={'code':None,'type':'invalid_request_error','param':'input',
        'message':'Bearer sensitive-echo must never be logged'}))
    assert error.code == 'CHATGPT_INVALID_REQUEST'
    assert error.provider_details['api_type'] == 'invalid_request_error'
    assert error.provider_details['api_param'] == 'input'
    assert 'sensitive-echo' not in str(error) + json.dumps(error.provider_details)


@pytest.mark.asyncio
@pytest.mark.parametrize('events', [
    [{'type': 'response.output_item.done', 'item': call()}],
    [done([call()], 'incomplete')],
    [done([call()], 'failed')],
    [done([call(namespace='other')])],
    [done([call(), call()])],
    [done([call(name='unauthorized')])],
    [done([{'type': 'web_search_call'}])],
    [ConnectionError('Bearer must-not-be-logged')],
])
async def test_failed_partial_and_invalid_tools_cannot_escape(events):
    stream = Stream(events)
    p, _ = plan(stream)
    with pytest.raises(RepoError) as caught:
        await p.complete('system', [], TOOLS)
    assert not getattr(caught.value, 'tool_calls', None)
    assert 'must-not-be-logged' not in str(caught.value)
    assert stream.closed


@pytest.mark.asyncio
async def test_late_usage_limit_retains_usage_and_never_falls_back():
    event = done([call()], 'failed')
    event['response']['error'] = {'code': 'subscription_sharing_usage_limit_exceeded', 'message': 'sensitive echo'}
    p, api = plan(Stream([{'type': 'response.output_text.delta', 'delta': 'starting'}, event]))
    with pytest.raises(RepoError) as caught:
        await p.complete('system', [], TOOLS)
    assert caught.value.code == 'subscription_sharing_usage_limit_exceeded'
    assert caught.value.usage['input_tokens'] == 100
    assert 'sensitive' not in str(caught.value)
    assert len(api.requests) == 1


@pytest.mark.asyncio
async def test_invalid_json_gets_host_feedback_not_executable_arguments():
    p, _ = plan(Stream([done([call(args='[]')])]))
    reply = await p.complete('system', [], TOOLS)
    assert reply.tool_calls[0].argument_error
    assert reply.tool_calls[0].arguments == {} and reply.tool_calls[0].raw_arguments == '[]'


@pytest.mark.asyncio
async def test_completed_inference_records_host_receipt_only_after_completion():
    receipts = []
    p, _ = plan(Stream([done()]))
    p.auth = NS(note_inference_success=lambda model, response_id: receipts.append((model, response_id)))
    await p.complete('system', [], [])
    assert receipts == [('chosen-model', 'resp_1')]
    p, _ = plan(Stream([done(status='incomplete')]))
    p.auth = NS(note_inference_success=lambda *args: receipts.append(args))
    with pytest.raises(RepoError):await p.complete('system', [], [])
    assert len(receipts) == 1


@pytest.mark.asyncio
async def test_models_uses_subscription_catalog_and_server_order():
    class Catalog:
        async def get(self, path, cast_to):
            assert path == '/models' and cast_to is dict
            return {'models': [{'slug': 'b', 'display_name': 'B', 'visibility': 'list'},
                               {'slug': 'hidden', 'visibility': 'hide'}, {'slug': 'a', 'visibility': 'list'}]}
    p = ChatGPTPlanProvider('catalog-only', auth=NS(), client=Catalog())
    assert await p.list_models() == [{'slug': 'b', 'display_name': 'B'}, {'slug': 'a', 'display_name': 'a'}]


@pytest.mark.asyncio
async def test_actual_sdk_sse_wire_namespace_and_catalog_without_network():
    openai = pytest.importorskip('openai')
    from openai import _base_client
    wire_http = getattr(_base_client, 'httpx2', None) or _base_client.httpx
    requests = []

    async def handle(request):
        requests.append(request)
        if request.method == 'GET':
            return wire_http.Response(200, json={'models': [{'slug': 'chosen-model', 'visibility': 'list'}]})
        body = json.loads(request.content)
        assert body['tools'][0]['type'] == 'namespace'
        assert body['tools'][0]['tools'][0]['name'] == 'process_run'
        assert body['store'] is False and body['stream'] is True
        events = [done([call()])]
        return wire_http.Response(200, headers={'content-type': 'text/event-stream'},
                                 content=''.join('data: '+json.dumps(event)+'\n\n' for event in events)+'data: [DONE]\n\n')
    async with wire_http.AsyncClient(transport=wire_http.MockTransport(handle)) as transport:
        async with openai.AsyncOpenAI(api_key='offline-test-token', base_url=RESOURCE,
                                     http_client=transport, max_retries=0) as sdk:
            p = ChatGPTPlanProvider('chosen-model', auth=NS(), client=sdk)
            reply = await p.complete('system', [], TOOLS)
            assert reply.tool_calls[0].name == 'process.run'
            assert reply.usage['input_tokens'] == 100
            assert (await p.list_models())[0]['slug'] == 'chosen-model'
    assert [str(r.url) for r in requests] == [RESOURCE+'/responses', RESOURCE+'/models']


def test_cli_selects_independent_providers_and_rejects_ignored_parameters(tmp_path, monkeypatch):
    args = parser().parse_args(['--provider', 'chatgpt-plan', '--model', 'chosen-model', '--auth-root', str(tmp_path), 'list'])
    assert isinstance(provider(args), ChatGPTPlanProvider)
    for option, value in [('--max-output-tokens', '4096'), ('--temperature', '0'),
                          ('--base-url', 'https://example.invalid'), ('--thinking', 'disabled'),
                          ('--api-key-env', 'OPENAI_API_KEY')]:
        args = parser().parse_args(['--provider', 'chatgpt-plan', '--model', 'chosen-model', option, value, 'list'])
        with pytest.raises(RepoError, match='Unsupported'):
            provider(args)
    captured = []
    monkeypatch.setattr('repo_code.cli.OpenAIProvider', lambda **kw: captured.append(kw))
    provider(parser().parse_args(['--provider', 'deepseek', '--model', 'deepseek-flash', 'list']))
    assert captured[-1]['api_key_env'] == 'DEEPSEEK_API_KEY_REPO'
    assert captured[-1]['base_url'] == 'https://api.deepseek.com'
    provider(parser().parse_args(['--provider', 'openai', '--model', 'custom-model', '--base-url', 'https://gateway.invalid/v1', 'list']))
    assert captured[-1]['base_url'] == 'https://gateway.invalid/v1'


def session(**extra):
    return {'client_id': 'oaiapp_test', 'ext_agent_host_id': 'urn:uuid:host', 'subject': 'user_1',
            'email': 'test@example.invalid', 'access_token': 'test-access', 'refresh_token': 'test-refresh',
            'id_token': 'test-id', 'scopes': [PLAN_SCOPE], 'expires_at': time.time()+3600, **extra}


def test_credentials_protection_and_readonly_status(tmp_path):
    auth = ChatGPTAuth(tmp_path/'auth')
    assert auth.status()['logged_in'] is False and not auth.root.exists()
    auth._write(session())
    assert auth.access_token() == 'test-access'
    public = json.dumps(auth.status())
    assert all(x not in public for x in ['test-access', 'test-refresh', 'test-id'])
    raw = (auth.root/'session.json').read_bytes()
    if os.name == 'nt':
        assert b'test-access' not in raw  # Real Windows DPAPI roundtrip.
    else:
        assert (auth.root/'session.json').stat().st_mode & 0o077 == 0


def test_verification_receipt_is_account_scoped_and_cannot_revive_logout(tmp_path):
    auth = ChatGPTAuth(tmp_path/'auth');auth._write(session())
    assert not auth.status()['live_inference_verified']
    auth.note_inference_success('test-model', 'resp_test')
    assert auth.status()['live_inference_verified']
    assert auth.status()['last_verified_inference']['model'] == 'test-model'
    auth._write({'client_id': 'oaiapp_test', 'subject': 'user_1'})
    auth.note_inference_success('test-model', 'resp_late')
    assert not auth.status()['logged_in'] and not auth.status()['live_inference_verified']


def test_rotating_refresh_serialized_and_scope_revocation(tmp_path):
    import httpx
    auth = ChatGPTAuth(tmp_path/'auth')
    auth._write(session(expires_at=0))
    calls = []

    def handle(request):
        data = parse_qs(request.content.decode())
        calls.append(data)
        assert data['client_id'] == ['oaiapp_test'] and 'scope' not in data
        return httpx.Response(200, json={'access_token': 'new-access', 'refresh_token': 'new-refresh',
                                         'token_type': 'Bearer', 'expires_in': 3600})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert auth.access_token(client=client) == 'new-access'
        assert auth.access_token(client=client) == 'new-access'
    assert len(calls) == 1 and auth._read()['refresh_token'] == 'new-refresh'
    auth._write(session(scopes=[]))
    with pytest.raises(RepoError) as caught:
        auth.access_token()
    assert caught.value.code == 'CHATGPT_PLAN_PERMISSION_REQUIRED'


def test_refresh_failure_is_safe_and_preserves_original_record(tmp_path):
    import httpx
    auth = ChatGPTAuth(tmp_path/'auth');auth._write(session(expires_at=0))
    before = auth._read()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400, json={
            'error': 'invalid_grant', 'error_description': 'test-refresh must never be echoed'}))) as client:
        with pytest.raises(RepoError) as caught:
            auth.access_token(client=client)
    assert caught.value.code == 'CHATGPT_LOGIN_REQUIRED'
    assert 'test-refresh' not in str(caught.value) and auth._read() == before


def test_refresh_never_races_another_writer(tmp_path):
    from repo_code.util import execution_lock
    auth = ChatGPTAuth(tmp_path/'auth');auth._write(session())
    with execution_lock(auth.root/'session.lock'):
        with pytest.raises(RepoError):auth.access_token()


def test_native_composition_rejects_auth_backup_overlap_before_creating_state(tmp_path):
    from repo_code.service import RepoCode
    state = tmp_path/'state'
    with pytest.raises(RepoError) as caught:
        RepoCode(state, ChatGPTPlanProvider('test', state/'auth'), sandbox='local')
    assert caught.value.code == 'CHATGPT_AUTH_STATE_OVERLAP' and not state.exists()


def test_proxy_selection_respects_explicit_environment_and_direct(monkeypatch):
    from repo_code.chatgpt_auth import chatgpt_proxy
    monkeypatch.setenv('REPO_CODE_CHATGPT_PROXY', 'http://127.0.0.1:10808')
    assert chatgpt_proxy() == 'http://127.0.0.1:10808'
    monkeypatch.setenv('REPO_CODE_CHATGPT_PROXY', 'direct')
    assert chatgpt_proxy() is None
    monkeypatch.setenv('REPO_CODE_CHATGPT_PROXY', 'file:///private/secret')
    with pytest.raises(RepoError) as caught:chatgpt_proxy()
    assert caught.value.code == 'CHATGPT_INVALID_PROXY' and 'private' not in str(caught.value)
    monkeypatch.delenv('REPO_CODE_CHATGPT_PROXY')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:3128')
    assert chatgpt_proxy() is None  # Let httpx honor its standard env/NO_PROXY rules.


def test_auth_http_error_identifies_stage_without_logging_body_or_tokens():
    import httpx
    from repo_code.chatgpt_auth import _json_request, TOKEN_ENDPOINT
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403, text='secret-echo'))) as client:
        with pytest.raises(RepoError) as caught:_json_request(client, 'POST', TOKEN_ENDPOINT, data={'code': 'secret-code'})
    assert 'token_exchange' in str(caught.value)
    assert 'secret' not in str(caught.value)


@pytest.fixture
def oidc():
    jwt = pytest.importorskip('jwt', reason='Install repo-code[chatgpt] for signed OAuth tests')
    pytest.importorskip('cryptography')
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid='test-key', alg='RS256', use='sig')
    return key, jwk


@pytest.mark.parametrize('fault', [None, 'bad_nonce', 'wrong_audience', 'wrong_issuer', 'wrong_signature', 'no_plan'])
def test_real_pkce_loopback_and_signed_oidc(tmp_path, monkeypatch, oidc, fault):
    import httpx
    import jwt
    key, jwk = oidc
    auth = ChatGPTAuth(tmp_path/'auth')
    browser = {}
    threads, callback_codes = [], []

    def open_url(url):
        query = parse_qs(urlsplit(url).query)
        browser.update(query)
        assert query['client_id'] == ['dynamic_agent_client']
        assert auth._read()['ext_agent_host_id'] == query['ext_agent_host_id'][0]
        assert query['agent_name_hint'] == ['repo-code']

        def redirect():
            # Invalid state must neither consume the listener nor exchange code.
            base = query['redirect_uri'][0]
            with httpx.Client(trust_env=False) as local:
                bad = local.get(base+'?'+urlencode({'state': 'invalid'}))
                callback_codes.append(bad.status_code)
                valid = local.get(base+'?'+urlencode({'state': query['state'][0], 'code': 'test-code', 'client_id': 'oaiapp_test'}))
                callback_codes.append(valid.status_code)
        thread = threading.Thread(target=redirect)
        threads.append(thread); thread.start()
        return True

    def respond(request):
        if request.url.path.endswith('openid-configuration'):
            return httpx.Response(200, json={'issuer': ISSUER, 'jwks_uri': ISSUER+'/.well-known/jwks.json'})
        if request.url.path.endswith('jwks.json'):
            return httpx.Response(200, json={'keys': [jwk]})
        data = parse_qs(request.content.decode())
        verifier = data['code_verifier'][0]
        import base64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        assert challenge == browser['code_challenge'][0]
        assert data['redirect_uri'] == browser['redirect_uri'] and data['client_id'] == ['oaiapp_test']
        claims = {'sub': 'user_1', 'iss': ISSUER, 'aud': 'oaiapp_test', 'iat': int(time.time()),
                  'exp': int(time.time())+3600, 'nonce': browser['nonce'][0]}
        if fault == 'bad_nonce':claims['nonce'] = 'wrong'
        if fault == 'wrong_audience':claims['aud'] = 'another-app'
        if fault == 'wrong_issuer':claims['iss'] = 'https://example.invalid'
        if fault == 'wrong_signature':claims['nonce'] = 'tampered'  # Sign with a different key below.
        signing_key = key
        if fault == 'wrong_signature':
            from cryptography.hazmat.primitives.asymmetric import rsa
            signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode(claims, signing_key, algorithm='RS256', headers={'kid': 'test-key'})
        return httpx.Response(200, json={'access_token': 'test-access', 'refresh_token': None if fault == 'no_plan' else 'test-refresh',
                                         'id_token': token, 'token_type': 'Bearer', 'expires_in': 3600,
                                         'scope': 'openid' if fault == 'no_plan' else PLAN_SCOPE})

    monkeypatch.setattr('repo_code.chatgpt_auth.webbrowser.open', open_url)
    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            if fault in {None, 'no_plan'}:
                status = auth.login(client=client, notify=lambda _: None, timeout=5)
                assert status['logged_in'] and status['plan_usage_enabled'] == (fault is None)
            else:
                with pytest.raises(RepoError, match='ID token'):
                    auth.login(client=client, notify=lambda _: None, timeout=5)
                assert not auth.status()['logged_in']
    finally:
        for thread in threads:thread.join(timeout=5)
    assert callback_codes == [400, 200]


def test_logout_revokes_then_clears_tokens_retains_registration(tmp_path):
    import httpx
    auth = ChatGPTAuth(tmp_path/'auth');auth._write(session())

    def respond(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'revocation_endpoint': ISSUER+'/oauth/revoke'})
        data = parse_qs(request.content.decode())
        assert data['token'] == ['test-refresh']
        return httpx.Response(200)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert auth.logout(client=client)['remote_revocation_confirmed']
    assert auth._read() == {k: session()[k] for k in ('ext_agent_host_id', 'client_id', 'subject', 'email')}


@pytest.mark.parametrize('overlap', ['same', 'ancestor', 'descendant'])
def test_auth_cannot_enter_workspace_or_run_backup(tmp_path, overlap):
    root = tmp_path/'private'
    target = root if overlap == 'same' else tmp_path if overlap == 'ancestor' else root/'task'
    p = ChatGPTPlanProvider('test', root)
    with pytest.raises(RepoError):p.assert_workspace_safe(target)
    with pytest.raises(RepoError):p.assert_state_safe(target)


@pytest.mark.asyncio
@pytest.mark.parametrize('memory', [False, True])
async def test_native_agent_owns_edit_verification_and_memory(app, source, contract, tmp_path, memory):
    from conftest import new_run
    from repo_code.agent import CodingAgent
    from repo_code.memory.service import MemoryService
    p, api = plan(Stream([done([call('read', '{"path":"main.py"}', 'read')])]),
                  Stream([done([call('edit', '{"path":"main.py","old_text":"return 0","new_text":"return 42"}', 'edit')])]),
                  Stream([done([{'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
                                'content': [{'type': 'output_text', 'text': 'Ready for verification', 'annotations': []}]}])]))
    app.provider = app.broker.provider = p
    app.agent = CodingAgent(app.store, app.runtime, app.broker, app.grader, app.releases, p)
    if memory:
        profile = {'mode': 'lexical_only', 'learning_enabled': False, 'auxiliary_calls': 0,
                   'semantic_consolidation': False, 'max_tokens': 64000}
        app.memory = MemoryService(app.store, app.runtime, p, profile)
        app.runtime.memory = app.broker.memory = app.agent.memory = app.memory
    run = new_run(app, source, contract, content='def answer():\n    return 0\n')
    result = await app.execute(run['id'])
    assert result['state'] == 'SUCCEEDED', result
    assert result['delivery']['outcome'] == 'VERIFIED'
    assert result['usage']['input_tokens'] == 300 and result['budget_used']['tool_calls'] == 2
    assert 'return 0' in (source/'main.py').read_text()  # Edits stay in the native task copy.
    assert api.requests[1]['input'][-1]['type'] == 'function_call_output'
    assert all(req['store'] is False for req in api.requests)
