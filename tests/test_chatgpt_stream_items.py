"""Completed item events are authoritative replay data, after terminal success."""
import pytest
from repo_code.util import RepoError
from test_chatgpt_provider import Stream,plan,done,call,TOOLS

def item(block,index=0):return {'type':'response.output_item.done','output_index':index,'item':block}
def message(text='OK'):return {'type':'message','id':'msg_1','role':'assistant','status':'completed','content':[{'type':'output_text','text':text}]}

@pytest.mark.asyncio
async def test_completed_text_and_json_survive_empty_terminal_array():
    for text in ['OK','{"changes":[]}']:
        p,_=plan(Stream([item(message(text)),done([])]))
        reply=await p.complete('s',[],[])
        assert reply.text==text and reply.native_blocks==[message(text)]
        assert reply.usage['output_tokens']==12

@pytest.mark.asyncio
async def test_reasoning_and_tools_replayed_in_index_order_once():
    reasoning={'type':'reasoning','id':'r','summary':[],'encrypted_content':'opaque-final'}
    p,api=plan(Stream([item(call(),1),item(reasoning,0),item(call(),1),done([])]),Stream([done()]))
    reply=await p.complete('s',[],TOOLS)
    assert len(reply.tool_calls)==1 and reply.native_blocks==[reasoning,call()]
    await p.complete('s',[{'role':'assistant','native_blocks':reply.native_blocks,'tool_calls':[c.model_dump() for c in reply.tool_calls]},
                           {'role':'tool','tool_call_id':'call_1','content':'observed'}],TOOLS)
    assert api.requests[-1]['input'][:2]==[reasoning,call()]

@pytest.mark.asyncio
@pytest.mark.parametrize('events',[
    [done([])],
    [{'type':'response.output_text.delta','delta':'not a complete item'},done([])],
    [item(call())],
    [item(call()),done([],'incomplete')],
    [item(call()),done([],'failed')],
    [item(call(),1),done([])],
    [{'type':'response.output_item.added','output_index':0,'item':message()},item(call(),1),done([])],
    [item(call()),item(call(args='{}')),done([])],
    [item(call()),done([call(args='{}')])],
    [item(call(namespace='other')),done([])],
])
async def test_partial_conflicting_empty_and_foreign_output_never_executes(events):
    p,_=plan(Stream(events))
    with pytest.raises(RepoError) as error:await p.complete('s',[],TOOLS)
    assert not getattr(error.value,'tool_calls',None)

@pytest.mark.asyncio
async def test_terminal_and_finished_items_are_not_double_counted():
    block=call();p,_=plan(Stream([item(block),done([block])]))
    reply=await p.complete('s',[],TOOLS)
    assert len(reply.tool_calls)==1
