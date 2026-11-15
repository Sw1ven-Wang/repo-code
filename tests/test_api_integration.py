"""Offline guards for the explicitly invoked live API harness and inference pinning."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from repo_code.cli import parser,provider
from repo_code.models import Budget
from repo_code.providers import OpenAIProvider,model_configuration,openai_tool_aliases
from repo_code.service import RepoCode
from repo_code.util import RepoError
from test_providers_mcp import FakeStream,Completion,chunk

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('api_integration',ROOT/'scripts/api_integration.py')
harness=importlib.util.module_from_spec(spec);spec.loader.exec_module(harness)


@pytest.mark.asyncio
async def test_medium_is_sent_and_unspecified_effort_omitted():
    for effort in (None,'medium'):
        api=Completion(FakeStream([chunk('ok',finish='stop')]))
        p=OpenAIProvider('gpt-6.1-sol',client=NS(chat=NS(completions=api)),reasoning_effort=effort)
        await p.complete('s',[{'role':'user','content':'u'}],[])
        if effort is None:assert 'reasoning_effort' not in api.request
        else:assert api.request['reasoning_effort']=='medium'


def test_invalid_effort_is_rejected_before_sdk_or_network():
    with pytest.raises(RepoError,match='INVALID_REASONING_EFFORT'):
        OpenAIProvider('m',reasoning_effort='typo')


@pytest.mark.asyncio
async def test_dotted_host_tool_uses_legal_wire_name_and_round_trips_history():
    stream=FakeStream([chunk(calls=[NS(index=0,id='process-1',function=NS(name='process_run',arguments='{"argv":["python","-V"]}'))],finish='tool_calls')])
    api=Completion(stream)
    p=OpenAIProvider('m',client=NS(chat=NS(completions=api)))
    tools=[{'name':'process.run','description':'Run a command','parameters':{'type':'object'}}]
    result=await p.complete('s',[],tools)
    assert api.request['tools'][0]['function']['name']=='process_run'
    assert tools[0]['name']=='process.run' and result.tool_calls[0].name=='process.run'
    api.stream=FakeStream([chunk('done',finish='stop')])
    history=[{'role':'assistant','content':'','tool_calls':[t.model_dump() for t in result.tool_calls]},
             {'role':'tool','tool_call_id':'process-1','content':'Python 3.13'}]
    await p.complete('s',history,[])
    assert api.request['messages'][1]['tool_calls'][0]['function']['name']=='process_run'
    assert api.request['messages'][2]['tool_call_id']=='process-1'


def test_tool_alias_collision_is_rejected_before_api_call():
    with pytest.raises(RepoError) as exc:
        openai_tool_aliases([{'name':'process.run'},{'name':'process_run'}],[])
    assert exc.value.code=='API_TOOL_NAME_COLLISION'


def test_cli_passes_explicit_medium(monkeypatch):
    monkeypatch.setattr('repo_code.cli.OpenAIProvider',lambda **kw:kw)
    args=parser().parse_args(['--provider','openai','--model','gpt-6.1-sol',
        '--base-url','https://relay.invalid/v1','--reasoning-effort','medium','doctor'])
    assert provider(args)=={'model':'gpt-6.1-sol','base_url':'https://relay.invalid/v1','reasoning_effort':'medium'}
    args.provider='anthropic'
    with pytest.raises(RepoError,match='REASONING_EFFORT_REQUIRES_OPENAI'):provider(args)


def test_env_endpoint_is_explicitly_pinned(monkeypatch):
    monkeypatch.setenv('OPENAI_BASE_URL','https://relay.invalid/v1')
    p=OpenAIProvider('m',client=object(),reasoning_effort='medium')
    assert model_configuration(p)['endpoint']=='https://relay.invalid/v1'
    assert model_configuration(p)['reasoning_effort']=='medium'
    assert 'reasoning_effort' not in model_configuration(NS(model='m',base_url=None))


@pytest.mark.asyncio
async def test_resume_cannot_change_medium_to_high(tmp_path):
    p=OpenAIProvider('m',client=object(),reasoning_effort='medium')
    app=RepoCode(tmp_path/'state',p,sandbox='local')
    source=tmp_path/'source';source.mkdir()
    run=app.new_task('test',source,budget=Budget(model_calls=1))
    assert run['data']['model_config']['reasoning_effort']=='medium'
    p.reasoning_effort='high'
    with pytest.raises(RepoError,match='Resume with the original'):
        await app.execute(run['id'])


@pytest.mark.parametrize('endpoint',[
    'https://user:secret@relay.invalid/v1','https://relay.invalid/v1?api_key=secret',
    'https://relay.invalid/v1#secret','file:///tmp/x','https://relay.invalid:bad/v1'])
def test_harness_blocks_unsafe_endpoint_without_recording_it(monkeypatch,tmp_path,endpoint):
    monkeypatch.setenv('TEST_RELAY_KEY','private-value')
    args=NS(base_url=endpoint,api_key_env='TEST_RELAY_KEY',model='m',reasoning_effort='medium')
    report=harness.preflight(args)
    assert report['status']=='BLOCKED' and report['base_url'] is None
    assert 'secret' not in json.dumps(report) and 'private-value' not in json.dumps(report)


def test_harness_default_is_offline_even_with_credentials(monkeypatch,tmp_path):
    monkeypatch.setenv('TEST_RELAY_KEY','private-value')
    monkeypatch.setattr(harness.importlib.util,'find_spec',lambda name:object())
    def forbidden(*args,**kwargs):raise AssertionError('must not instantiate a paid client')
    monkeypatch.setattr(harness,'OpenAIProvider',forbidden)
    report=tmp_path/'report.json'
    code=harness.main(['--model','gpt-6.1-sol','--base-url','https://relay.invalid/v1',
        '--api-key-env','TEST_RELAY_KEY','--reasoning-effort','medium','--report',str(report)])
    assert code==0
    data=json.loads(report.read_text(encoding='utf-8'))
    assert not data['provider_contacted'] and data['adapter_calls']==0 and data['tests']==[]
    assert 'private-value' not in report.read_text(encoding='utf-8')
