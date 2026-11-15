import json

import pytest

from repo_code.context import groups
from repo_code.live_compact import LiveCompactProfile, plan_cut
from repo_code.providers import OpenAIProvider
from live_compact_helpers import records


@pytest.mark.parametrize('model,url', [('gpt-6.1-terra','https://example.invalid/v1'),
                                        ('deepseek-flash','https://api.deepseek.com')])
def test_plain_native_function_call_history_remains_paired(model,url):
    provider=OpenAIProvider(model,base_url=url,client=object())
    rows=records();slots=groups([r['data'] for r in rows])
    serialize=lambda s,m,t:provider.build_request(s,m,t)[0]
    result=plan_cut(rows,slots,system='Host',tools=[],serialize=serialize,
                    profile=LiveCompactProfile(),available_history=18000)
    assert result.proposal is not None
    slots[result.proposal.group_start]=[result.proposal.card]
    messages=[m for group in slots for m in group];groups(messages)
    request=serialize('Host',messages,[])
    for i,message in enumerate(request['messages']):
        if message.get('tool_calls'):
            ids=[c['id'] for c in message['tool_calls']]
            assert [m['tool_call_id'] for m in request['messages'][i+1:i+1+len(ids)]]==ids
    assert json.loads(result.proposal.card['content'])['source_roles']==['assistant','tool']


@pytest.mark.parametrize('native',[
    {'native_blocks':[{'type':'thinking','thinking':'opaque','signature':'signed'}]},
    {'native_blocks':[{'type':'function_call','call_id':'call','arguments':'{}'}]},
    {'reasoning_content':''}, {'encrypted_content':'opaque'},
])
def test_opaque_native_envelopes_are_not_rebased(native):
    rows=records();rows[1]['data'].update(native)
    result=plan_cut(rows,groups([r['data'] for r in rows]),system='',tools=[],serialize=lambda s,m,t:[s,m,t],
        profile=LiveCompactProfile(),available_history=18000)
    assert result.reason=='PROTOCOL_REBASE_UNVERIFIED' and result.proposal is None
