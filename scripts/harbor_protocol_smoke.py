"""Explicit paid DeepSeek thinking/tool round-trip, separate from task scores."""
import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path

from repo_code.providers import OpenAIProvider


async def run(report):
    from openai import AsyncOpenAI
    client=AsyncOpenAI(api_key=os.environ['DEEPSEEK_API_KEY_REPO'],base_url='https://api.deepseek.com',max_retries=0,timeout=120)
    provider=OpenAIProvider('deepseek-flash',base_url='https://api.deepseek.com',client=client,
        thinking='enabled',reasoning_effort='high',max_tokens=4096)
    tools=[{'name':'read_nonce','description':'Obtain the nonce for the current test.',
        'parameters':{'type':'object','properties':{},'additionalProperties':False}}]
    messages=[{'role':'user','content':'Call read_nonce exactly once, then repeat its returned nonce exactly as your final answer.'}]
    records=[]
    try:
        first=await provider.complete('Use the advertised tool. Do not invent the nonce.',messages,tools)
        records.append(first.usage)
        assert len(first.tool_calls)==1 and first.tool_calls[0].name=='read_nonce'
        nonce=secrets.token_hex(12)
        messages += [{'role':'assistant','content':first.text,'reasoning_content':first.reasoning_content,
            'tool_calls':[x.model_dump() for x in first.tool_calls]},
            {'role':'tool','tool_call_id':first.tool_calls[0].id,'content':nonce}]
        second=await provider.complete('Use the advertised tool. Do not invent the nonce.',messages,tools)
        records.append(second.usage)
        assert not second.tool_calls and nonce in second.text
        data={'status':'PASSED','kind':'LIVE_PROVIDER_PROTOCOL_NOT_BENCHMARK','model':'deepseek-flash',
            'thinking':'enabled','reasoning_effort':'high','calls':len(records),'usage':records,
            'reasoning_content_present':first.reasoning_content is not None,'nonce_round_trip':True}
    except BaseException as exc:
        data={'status':'FAILED','kind':'LIVE_PROVIDER_PROTOCOL_NOT_BENCHMARK','error_type':type(exc).__name__,
            'error_code':getattr(exc,'code',None),'calls':len(records),'usage':records}
        raise
    finally:
        report.parent.mkdir(parents=True,exist_ok=True)
        report.write_text(json.dumps(data,indent=2),encoding='utf-8')
        await client.close()
    print(json.dumps(data,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--report',type=Path,required=True)
    p.add_argument('--i-understand-api-costs',action='store_true',required=True)
    args=p.parse_args();asyncio.run(run(args.report))
