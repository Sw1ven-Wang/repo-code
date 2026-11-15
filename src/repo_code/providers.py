"""Native SDK adapters. Credentials never cross the trusted model broker boundary."""
from __future__ import annotations
import inspect
import json
import os
import re
from typing import Protocol
from .models import Assistant, ToolCall
from .util import RepoError, ProviderNotDispatched, redact

class Provider(Protocol):
    async def complete(self,system: str,messages:list,tools:list,on_delta=None)->Assistant: ...

class ScriptedProvider:
    """Deterministic testing/demo provider. Not a model-quality benchmark."""
    def __init__(self,responses=None,handler=None):self.responses=list(responses or []);self.handler=handler;self.calls=0
    async def complete(self,system,messages,tools,on_delta=None):
        i=self.calls;self.calls+=1
        if self.handler:
            response=self.handler(system,messages,tools,i)
            if inspect.isawaitable(response):response=await response
        elif self.responses:response=self.responses.pop(0)
        else:raise RepoError("SCRIPT_EXHAUSTED")
        if isinstance(response,Exception):raise response
        result=response if isinstance(response,Assistant) else Assistant.model_validate(response)
        if on_delta and result.text:on_delta(result.text)
        return result

def openai_tool_aliases(tools,messages):
    names=[t['name'] for t in tools]
    names += [t['name'] for m in messages if m.get('role')=='assistant' for t in m.get('tool_calls',[])]
    encode={};decode={}
    for name in names:
        alias=re.sub(r'[^a-zA-Z0-9_-]','_',name)
        if not alias or len(alias)>64:raise RepoError('INVALID_API_TOOL_NAME',name)
        if alias in decode and decode[alias]!=name:raise RepoError('API_TOOL_NAME_COLLISION',alias)
        encode[name]=alias;decode[alias]=name
    return encode,decode


def openai_messages(messages,name_map=None):
    result=[]
    for m in messages:
        if m["role"]=="assistant":
            d={"role":"assistant","content":m.get("content") or None}
            if m.get('reasoning_content') is not None:d['reasoning_content']=m['reasoning_content']
            if m.get("tool_calls"):d["tool_calls"]=[{"id":t["id"],"type":"function","function":{"name":(name_map or {}).get(t["name"],t["name"]),"arguments":t['raw_arguments'] if t.get('argument_error') and t.get('raw_arguments') is not None else json.dumps(t["arguments"],ensure_ascii=False)}} for t in m["tool_calls"]]
            result.append(d)
        elif m["role"]=="tool":result.append({"role":"tool","tool_call_id":m["tool_call_id"],"content":m["content"]})
        else:result.append({"role":m["role"],"content":m["content"]})
    return result

class OpenAIProvider:
    supports_json_mode=True
    def __init__(self,model,base_url=None,api_key_env="OPENAI_API_KEY",client=None,timeout=60,reasoning_effort=None,thinking=None,max_tokens=None,temperature=None):
        if reasoning_effort not in {None,"none","minimal","low","medium","high","xhigh"}:
            raise RepoError("INVALID_REASONING_EFFORT")
        self.model=model;self.base_url=base_url or os.getenv("OPENAI_BASE_URL")
        self.reasoning_effort=reasoning_effort
        if thinking not in {None,'enabled','disabled'}:raise RepoError('INVALID_THINKING_MODE')
        if max_tokens is not None and (isinstance(max_tokens,bool) or not isinstance(max_tokens,int) or max_tokens<=0):raise RepoError('INVALID_MAX_TOKENS')
        if temperature is not None and (isinstance(temperature,bool) or not isinstance(temperature,(int,float)) or not 0<=temperature<=2):raise RepoError('INVALID_TEMPERATURE')
        normalized_temperature=int(temperature) if temperature is not None and float(temperature).is_integer() else temperature
        self.thinking,self.max_tokens,self.temperature=thinking,max_tokens,normalized_temperature
        if client is None:
            try:from openai import AsyncOpenAI
            except ImportError:raise RepoError("SDK_MISSING","Install repo-code[models]") from None
            key=os.getenv(api_key_env)
            if not key:raise RepoError("API_KEY_MISSING",api_key_env)
            # Avoid optional system Brotli implementations incompatible with the
            # SDK's streaming decoder. Identity keeps SSE transport portable.
            client=AsyncOpenAI(api_key=key,base_url=self.base_url,timeout=timeout,max_retries=0,
                               default_headers={"Accept-Encoding":"identity"})
        self.client=client
    def build_request(self,system,messages,tools,*,json_mode=False):
        """The exact native request is shared with Memory v2 budget admission."""
        encode,decode=openai_tool_aliases(tools,messages)
        aliases={name:alias for name,alias in encode.items() if name!=alias}
        if aliases:system+='\nAPI tool aliases: '+json.dumps(aliases)+'. Use the advertised alias names when calling these same Host tools.'
        request={"model":self.model,"messages":[{"role":"system","content":system},*openai_messages(messages,encode)],"stream":True,"stream_options":{"include_usage":True}}
        if self.reasoning_effort is not None:request["reasoning_effort"]=self.reasoning_effort
        if self.thinking is not None:request['extra_body']={'thinking':{'type':self.thinking}}
        if self.max_tokens is not None:request['max_tokens']=self.max_tokens
        if self.temperature is not None:request['temperature']=self.temperature
        if json_mode:request['response_format']={'type':'json_object'}
        if tools:request["tools"]=[{"type":"function","function":{**t,"name":encode[t["name"]]}} for t in tools]
        return request,decode
    async def complete(self,system,messages,tools,on_delta=None,*,json_mode=False):
        self.last_usage={}
        self.last_response_info={}
        request,decode=self.build_request(system,messages,tools,json_mode=json_mode)
        text=[];reasoning=[];calls={};usage={};reason=None;stream=None
        try:
            stream=await self.client.chat.completions.create(**request)
            async for chunk in stream:
                u=getattr(chunk,"usage",None)
                if u:
                    usage=u.model_dump() if hasattr(u,"model_dump") else vars(u)
                    self.last_usage=usage
                if not chunk.choices:continue
                ch=chunk.choices[0];d=ch.delta
                if getattr(d,'reasoning_content',None):reasoning.append(d.reasoning_content)
                if ch.finish_reason:reason=ch.finish_reason
                if d.content:
                    text.append(d.content)
                    if on_delta:on_delta(d.content)
                for tc in d.tool_calls or []:
                    item=calls.setdefault(tc.index,{"id":"","name":"","arguments":""})
                    if tc.id:item["id"]=tc.id
                    if tc.function:
                        if tc.function.name:item["name"]+=tc.function.name
                        if tc.function.arguments:item["arguments"]+=tc.function.arguments
            self.last_response_info={'finish_reason':reason,'text_chars':sum(map(len,text)),
                                     'tool_argument_chars':sum(len(c['arguments']) for c in calls.values()),'tool_count':len(calls)}
            if reason=='length':
                # A truncated tool call is never parsed, admitted or executed.
                # Return usage so the Host can reject it and request smaller work.
                return Assistant(usage=usage,stop_reason='length')
            if reason not in {"stop","tool_calls"}:raise RepoError("INCOMPLETE_MODEL_RESPONSE",str(reason))
            parsed=[]
            for item in calls.values():
                if not item['id'] or not item['name']:raise RepoError('INVALID_TOOL_JSON','Missing tool call identity')
                error=None
                try:
                    args=json.loads(item['arguments'])
                    if not isinstance(args,dict):raise ValueError('Tool arguments must be a JSON object')
                except ValueError as exc:args={};error=str(exc)[:220]
                parsed.append(ToolCall(id=item["id"],name=decode.get(item["name"],item["name"]),arguments=args,
                                       argument_error=error,raw_arguments=item['arguments'] if error else None))
            return Assistant(text="".join(text),reasoning_content=''.join(reasoning) if reasoning or self.thinking=='enabled' else None,tool_calls=parsed,usage=usage,stop_reason=reason)
        except RepoError as exc:
            exc.usage=usage;exc.provider_details=self.last_response_info;raise
        except Exception as exc:
            error=RepoError('MODEL_ERROR',redact(str(exc))[:500]);error.usage=usage
            error.provider_details={**self.last_response_info,'error_type':type(exc).__name__,
                                    'status_code':getattr(exc,'status_code',None),'request_id':getattr(exc,'request_id',None)}
            raise error from None
        finally:
            if stream and hasattr(stream,"close"):
                r=stream.close()
                if inspect.isawaitable(r):await r

class AnthropicProvider:
    def __init__(self,model,base_url=None,api_key_env="ANTHROPIC_API_KEY",client=None,timeout=60,max_tokens=4096):
        self.model,self.max_tokens=model,max_tokens;self.base_url=base_url
        if client is None:
            try:from anthropic import AsyncAnthropic
            except ImportError:raise RepoError("SDK_MISSING","Install repo-code[models]") from None
            key=os.getenv(api_key_env)
            if not key:raise RepoError("API_KEY_MISSING",api_key_env)
            client=AsyncAnthropic(api_key=key,base_url=base_url,timeout=timeout,max_retries=0)
        self.client=client
    async def complete(self,system,messages,tools,on_delta=None):
        history=[]
        for m in messages:
            if m["role"]=="tool":
                entry={"type":"tool_result","tool_use_id":m["tool_call_id"],"content":m["content"],"is_error":m.get("is_error",False)}
                if history and history[-1]["role"]=="user" and isinstance(history[-1]["content"],list):history[-1]["content"].append(entry)
                else:history.append({"role":"user","content":[entry]})
            elif m["role"]=="assistant":
                blocks=m.get("native_blocks") or ([{"type":"text","text":m["content"]}] if m.get("content") else [])
                if not m.get("native_blocks"):blocks += [{"type":"tool_use","id":t["id"],"name":t["name"],"input":t["arguments"]} for t in m.get("tool_calls",[])]
                history.append({"role":"assistant","content":blocks})
            else:history.append({"role":"user","content":m["content"]})
        args={"model":self.model,"max_tokens":self.max_tokens,"system":system,"messages":history}
        if tools:args["tools"]=[{"name":t["name"],"description":t["description"],"input_schema":t["parameters"]} for t in tools]
        try:
            async with self.client.messages.stream(**args) as stream:
                async for text in stream.text_stream:
                    if on_delta:on_delta(text)
                result=await stream.get_final_message()
            if result.stop_reason not in {"end_turn","tool_use","stop_sequence"}:raise RepoError("INCOMPLETE_MODEL_RESPONSE",result.stop_reason)
            blocks=[b.model_dump() if hasattr(b,"model_dump") else vars(b) for b in result.content]
            calls=[ToolCall(id=b["id"],name=b["name"],arguments=b["input"]) for b in blocks if b["type"]=="tool_use"]
            return Assistant(text="".join(b["text"] for b in blocks if b["type"]=="text"),tool_calls=calls,usage=result.usage.model_dump(),stop_reason=result.stop_reason,native_blocks=blocks)
        except RepoError:raise
        except Exception as exc:raise RepoError("MODEL_ERROR",redact(str(exc))[:500]) from None

def is_test_double(value):
    seen=set()
    while hasattr(value,'inner') and id(value) not in seen:
        seen.add(id(value));value=value.inner
    return isinstance(value,ScriptedProvider)


def json_output_options(provider):
    """Explicit Host request format; wrappers cannot silently lose capability."""
    seen=set()
    while id(provider) not in seen:
        seen.add(id(provider))
        if getattr(provider,'supports_json_mode',False):return {'json_mode':True}
        if not hasattr(provider,'inner'):break
        provider=provider.inner
    return {}


def model_configuration(provider):
    """Pin explicit inference settings; keep old default manifests compatible."""
    result={"adapter":type(provider).__name__,"model":getattr(provider,"model","scripted"),
            "endpoint":getattr(provider,"base_url",None)}
    effort=getattr(provider,"reasoning_effort",None)
    if effort is not None:result["reasoning_effort"]=effort
    for field in ('thinking','max_tokens','temperature'):
        value=getattr(provider,field,None)
        if value is not None:result[field]=value
    for field in ('billing','wire_protocol'):
        value=getattr(provider,field,None)
        if value is not None:result[field]=value
    return result


class MeteredProvider:
    """Shared Host budget counts every attempted call, including failed trials."""
    def __init__(self,inner,ledger):
        self.inner,self.ledger=inner,ledger
        self.model=getattr(inner,"model","unknown")
        self.base_url=getattr(inner,"base_url",None)
        self.reasoning_effort=getattr(inner,"reasoning_effort",None)
        for field in ('thinking','max_tokens','temperature'):setattr(self,field,getattr(inner,field,None))
        for field in ('billing','wire_protocol'):setattr(self,field,getattr(inner,field,None))
    async def complete(self,*args,**kwargs):
        if self.ledger["calls"]>=self.ledger["limit"]:raise ProviderNotDispatched("EVOLUTION_TOTAL_BUDGET")
        self.ledger["calls"]+=1
        result=await self.inner.complete(*args,**kwargs)
        self.ledger["usage"].append(result.usage)
        return result
    def build_request(self,system,messages,tools,**options):
        if hasattr(self.inner,'build_request'):return self.inner.build_request(system,messages,tools,**options)
        return {'system':system,'messages':messages,'tools':tools},{}
    def assert_workspace_safe(self,workspace):
        if hasattr(self.inner,'assert_workspace_safe'):self.inner.assert_workspace_safe(workspace)
    def assert_state_safe(self,state_root):
        if hasattr(self.inner,'assert_state_safe'):self.inner.assert_state_safe(state_root)
