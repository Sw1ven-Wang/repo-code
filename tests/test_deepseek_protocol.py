from types import SimpleNamespace as NS

import pytest

from repo_code.providers import OpenAIProvider,model_configuration,openai_messages


class Stream:
    async def __aiter__(self):
        yield NS(usage=None,choices=[NS(finish_reason=None,delta=NS(content=None,tool_calls=None,reasoning_content='context-'))])
        yield NS(usage=None,choices=[NS(finish_reason='tool_calls',delta=NS(content=None,reasoning_content='signature',
            tool_calls=[NS(index=0,id='c1',function=NS(name='read',arguments='{"path":"a"}'))]))])
    async def close(self):pass


@pytest.mark.asyncio
async def test_thinking_tool_round_trip_preserves_provider_context():
    requests=[]
    async def create(**kwargs):requests.append(kwargs);return Stream()
    provider=OpenAIProvider('deepseek-flash',thinking='enabled',reasoning_effort='high',max_tokens=8192,
        client=NS(chat=NS(completions=NS(create=create))))
    reply=await provider.complete('system',[{'role':'user','content':'read'}],[])
    assert reply.reasoning_content=='context-signature'
    messages=[{'role':'assistant','content':reply.text,'reasoning_content':reply.reasoning_content,
        'tool_calls':[t.model_dump() for t in reply.tool_calls]},
        {'role':'tool','tool_call_id':'c1','content':'a contents'}]
    await provider.complete('system',messages,[])
    assert requests[1]['messages'][1]['reasoning_content']=='context-signature'
    assert requests[0]['extra_body']=={'thinking':{'type':'enabled'}}
    assert requests[0]['max_tokens']==8192
    assert model_configuration(provider)['thinking']=='enabled'
    assert model_configuration(provider)['max_tokens']==8192


def test_default_openai_history_remains_compatible():
    assert openai_messages([{'role':'assistant','content':'done'}])==[{'role':'assistant','content':'done'}]
