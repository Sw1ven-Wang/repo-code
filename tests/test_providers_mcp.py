import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from repo_code.providers import OpenAIProvider,AnthropicProvider,openai_messages
from repo_code.mcp import MCPClient,validate_schema
from repo_code.models import Assistant
from repo_code.util import RepoError
from conftest import new_run,tool

class FakeStream:
    def __init__(self,chunks):self.chunks=chunks;self.closed=False
    def __aiter__(self):return self._items()
    async def _items(self):
        for chunk in self.chunks:yield chunk
    async def close(self):self.closed=True
class Completion:
    def __init__(self,stream):self.stream=stream;self.request=None
    async def create(self,**kwargs):self.request=kwargs;return self.stream

def chunk(text=None,calls=None,finish=None,usage=None):return NS(choices=[NS(delta=NS(content=text,tool_calls=calls),finish_reason=finish)],usage=usage)

@pytest.mark.asyncio
async def test_openai_native_stream_tool_assembly_and_usage():
    stream=FakeStream([chunk('hello'),chunk(calls=[NS(index=0,id='id1',function=NS(name='read',arguments='{"pa'))]),chunk(calls=[NS(index=0,id=None,function=NS(name=None,arguments='th":"a.py"}'))],finish='tool_calls'),NS(choices=[],usage=NS(prompt_tokens=100,completion_tokens=10))])
    complete=Completion(stream);p=OpenAIProvider('test',client=NS(chat=NS(completions=complete)))
    deltas=[];r=await p.complete('system',[{'role':'user','content':'read'}],[],deltas.append)
    assert r.tool_calls[0].arguments=={'path':'a.py'} and r.usage['prompt_tokens']==100
    assert stream.closed and deltas==['hello']

@pytest.mark.asyncio
@pytest.mark.parametrize('finish,args',[('length','{}'),('tool_calls','[]'),('tool_calls','{')])
async def test_openai_partial_or_invalid_json_is_not_executable(finish,args):
    stream=FakeStream([chunk(calls=[NS(index=0,id='x',function=NS(name='read',arguments=args))],finish=finish)])
    p=OpenAIProvider('test',client=NS(chat=NS(completions=Completion(stream))))
    reply=await p.complete('s',[],[])
    if finish=='length':assert reply.stop_reason=='length' and not reply.tool_calls
    else:assert reply.tool_calls[0].argument_error and reply.tool_calls[0].raw_arguments==args
    assert stream.closed

class Block:
    def __init__(self,**data):self.data=data
    def model_dump(self):return self.data
class AnthroStream:
    async def __aenter__(self):return self
    async def __aexit__(self,*a):pass
    @property
    def text_stream(self):
        async def chunks():yield 'working'
        return chunks()
    async def get_final_message(self):return NS(stop_reason='tool_use',content=[Block(type='thinking',thinking='opaque',signature='signed'),Block(type='tool_use',id='t',name='read',input={'path':'a'})],usage=Block(input_tokens=30,output_tokens=4))
class AnthroMessages:
    def stream(self,**kw):self.request=kw;return AnthroStream()

@pytest.mark.asyncio
async def test_anthropic_preserves_protocol_blocks_and_groups_results():
    api=AnthroMessages();p=AnthropicProvider('test',client=NS(messages=api))
    r=await p.complete('s',[{'role':'user','content':'go'}],[])
    assert r.native_blocks[0]['signature']=='signed'
    await p.complete('s',[{'role':'assistant','content':'','native_blocks':r.native_blocks,'tool_calls':[x.model_dump() for x in r.tool_calls]},
        {'role':'tool','tool_call_id':'t','content':'result'},{'role':'tool','tool_call_id':'t2','content':'second'}],[])
    assert len(api.request['messages'][-1]['content'])==2
    assert api.request['messages'][0]['content'][0]['signature']=='signed'

SERVER=r'''import sys,json
for line in sys.stdin:
    q=json.loads(line);method=q.get('method');rid=q.get('id')
    if rid is None:continue
    if method=='initialize':result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'test','version':'1'}}
    elif method=='tools/list':result={'tools':[{'name':'sum','description':'sum two integers','inputSchema':{'type':'object','properties':{'a':{'type':'integer'},'b':{'type':'integer'}},'required':['a','b'],'additionalProperties':False}}]}
    elif method=='tools/call':
        a=q['params']['arguments'];result={'content':[{'type':'text','text':str(a['a']+a['b'])}]}
    else:result={}
    print(json.dumps({'jsonrpc':'2.0','id':rid,'result':result}),flush=True)
'''

@pytest.mark.asyncio
async def test_real_isolated_mcp_handshake_and_call(app,source,contract,tmp_path):
    server_root=tmp_path/'mcp';server_root.mkdir();(server_root/'server.py').write_text(SERVER)
    run=new_run(app,source,contract);client=MCPClient(app.sandbox,server_root,['python','server.py'],run['id'])
    try:
        await client.start();app.broker.mcp['calc']=client
        _,listing=await tool(app,run,'mcp.discover',{})
        assert listing['result']['servers']['calc']['tools'][0]['name']=='sum'
        req,r=await tool(app,run,'mcp.call',{'server':'calc','tool':'sum','arguments':{'a':2,'b':3}})
        assert r['status']=='WAITING_APPROVAL';app.broker.approve(req.key)
        r=await app.broker.submit(req);assert r['result']['content'][0]['text']=='5'
        with pytest.raises(RepoError):await client.call('sum',{'a':'x','b':3})
    finally:await client.close()
    assert app.sandbox.resources_stopped(run['id'])

@pytest.mark.asyncio
async def test_mcp_emergency_stop(app,source,contract,tmp_path):
    root=tmp_path/'mcp';root.mkdir();(root/'server.py').write_text(SERVER)
    run=new_run(app,source,contract);client=await MCPClient(app.sandbox,root,['python','server.py'],run['id']).start()
    app.runtime.admit(run['id'],'cancel-mcp','CANCEL')
    assert app.sandbox.resources_stopped(run['id'])
    await client.close()

@pytest.mark.parametrize('value,schema',[(True,{'type':'integer'}),({'x':1},{'type':'object','additionalProperties':False}),([] ,{'type':'object'}),({}, {'required':['x']})])
def test_mcp_schema_rejects_invalid(value,schema):
    with pytest.raises(RepoError):validate_schema(value,schema)
