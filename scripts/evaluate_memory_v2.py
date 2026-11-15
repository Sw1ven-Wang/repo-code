"""Frozen lightweight Memory v2 experiments; all auxiliary calls are counted.

Raw provider reasoning is never exported. Paid mode requires a configured local
DeepSeek key. Deterministic contracts live in test_memory_v2_contracts.py.
"""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import time

from repo_code.context import ContextEngine, groups, estimate_tokens
from repo_code.models import Assistant, Budget, Check, Contract, Requirement
from repo_code.providers import OpenAIProvider, ScriptedProvider, model_configuration
from repo_code.service import RepoCode
from repo_code.memory.models import MemoryProfile, ClaimRecord, EvidenceRef
from repo_code.memory.working_state import semantic_message
from repo_code.memory.encoders import LocalEncoder
from repo_code.acceptance import freeze_contract
from repo_code.util import RepoError, canonical, digest, atomic_write,redact

ROOT=Path(__file__).resolve().parents[1]


def write(path,value):atomic_write(path,json.dumps(value,ensure_ascii=False,indent=2).encode())


class AuditProvider:
    def __init__(self,inner,output,limit=260):
        self.inner,self.output,self.limit=inner,output,limit;self.calls=[]
        for k,v in model_configuration(inner).items():
            if k not in {'adapter','endpoint'}:setattr(self,k,v)
        self.base_url=inner.base_url
    def build_request(self,*args,**kwargs):return self.inner.build_request(*args,**kwargs)
    async def complete(self,*args,**kwargs):
        if self.limit is not None and len(self.calls)>=self.limit:raise RepoError('EXPERIMENT_TOTAL_CALL_LIMIT')
        purpose=('compaction' if args[0].startswith('Extract compact task-memory') else 'consolidation' if args[0].startswith('Extract up to 8 evidence-bound') else 'delegate' if args[0].startswith('Read-only explorer.') else 'auxiliary' if not args[2] else 'main')
        row={'call':len(self.calls)+1,'started':time.time(),'purpose':purpose,
             'request_digest':digest(self.build_request(*args[:3],**({'json_mode':kwargs['json_mode']} if 'json_mode' in kwargs else {}))[0]),
             'json_mode':bool(kwargs.get('json_mode'))}
        self.calls.append(row);write(self.output/'calls.json',self.calls)
        started=time.monotonic()
        try:
            reply=await asyncio.wait_for(self.inner.complete(*args,**kwargs),90)
            row.update(usage=reply.usage,tool_names=[c.name for c in reply.tool_calls],stop_reason=reply.stop_reason,
                       invalid_tool_calls=sum(bool(c.argument_error) for c in reply.tool_calls),
                       visible_response_digest=digest({'text':reply.text,'tools':[c.model_dump() for c in reply.tool_calls]}))
            return reply
        except Exception as exc:
            row['error']=getattr(exc,'code',type(exc).__name__);row['error_detail']=redact(str(exc))[:500]
            row['usage']=getattr(exc,'usage',{});row['provider_details']=getattr(exc,'provider_details',{});raise
        finally:row['seconds']=time.monotonic()-started;write(self.output/'calls.json',self.calls)


def provider(output):
    p=OpenAIProvider('deepseek-flash',base_url='https://api.deepseek.com',api_key_env='DEEPSEEK_API_KEY_REPO',thinking='disabled',max_tokens=4096,temperature=0)
    p.client.max_retries=0
    return AuditProvider(p,output)


def replay_samples():
    samples=[]
    subjects=['CSV column order','transaction boundary','cancellation cleanup','public error codes','cache key format','UTC timestamps',
              'atomic file replacement','pagination cursor','dependency lock','return type compatibility','unicode normalization','retry idempotency']
    for i,subject in enumerate(subjects):
        messages=[{'role':'user','content':f'Decision D{i}: preserve {subject}. Open question Q{i}: inspect rollback after partial failure.'}]
        for step in range(32):
            text=(f'Phase {step//8}, observation {step}: examining {subject}; attempt {step%3}; '
                  f'file component_{step%5}.py version {step//5}; failing scenario input case-{step}; '
                  'hypothesis remains unconfirmed until the registered external check runs.')
            messages.extend([{'role':'assistant','content':'','tool_calls':[{'id':f'c{step}','name':'read','arguments':{'path':f'component_{step%5}.py'}}]},
                             {'role':'tool','tool_call_id':f'c{step}','content':text}])
            if step==9:messages.append({'role':'user','content':f'Correction C{i}: retain {subject}, but treat previous test results as historical after edits.'})
        samples.append({'id':f'replay-{i:02}','source_class':'SCRIPT_CONSTRUCTED','split':'DEV' if i<4 else 'TEST',
                        'subject':subject,'expected_anchors':[f'Decision D{i}',f'Open question Q{i}',f'Correction C{i}'],'messages':messages})
    return samples


async def replay(output,p,encoder,profile):
    samples=replay_samples();write(output/'replay-fixtures.json',samples)
    results=[]
    for sample in samples:
        folder=output/sample['id'];source=folder/'input';source.mkdir(parents=True)
        app=RepoCode(folder/'host',p,'local',memory_profile=profile,memory_encoder=encoder)
        run=app.new_task('Investigate '+sample['subject']+' preserving registered decisions',source,budget=Budget(model_calls=8,wall_seconds=600))
        # Three successive closed prefixes, carrying the same earlier decision.
        all_groups=groups(sample['messages']);last=0;node=None;errors=[]
        for boundary in [12,24,len(all_groups)]:
            for group in all_groups[last:boundary]:
                for message in group:app.store.append_message(run['id'],message)
            last=boundary;records=app.memory.history.records(run['id']);_,state=app.memory.reducer.reduce(run['id'])
            try:_,node=await app.memory.compactor.compact(run['id'],records[-1]['seq'],state)
            except Exception as exc:errors.append(getattr(exc,'code',type(exc).__name__))
        text=canonical(node or {}).decode()
        original=app.store.messages(run['id'])
        # Actual legacy folding, using its unchanged source/pin codec in another state.
        baseline=RepoCode(folder/'legacy',ScriptedProvider(),'local')
        old=baseline.new_task('Investigate '+sample['subject']+' preserving registered decisions',source)
        for message in original[1:]:baseline.store.append_message(old['id'],message)
        try:
            _,folded=ContextEngine(baseline.store,baseline.runtime).build(old['id'],[],{'max_tokens':3200,'reserve_tokens':1000})
            baseline_text=canonical(folded).decode()
        except RepoError:baseline_text=''
        anchors=sample['expected_anchors']
        record={'id':sample['id'],'split':sample['split'],'source_class':sample['source_class'],
                'annotated_anchor_count':len(anchors),'v2_anchor_retention':sum(x in text for x in anchors)/len(anchors),
                'legacy_anchor_retention':sum(x in baseline_text for x in anchors)/len(anchors),
                'raw_source_retrievable':all(app.memory.history.resolve(run['id'],r['ref']['source_id']) for r in app.memory.history.records(run['id'])),
                'semantic_errors':errors,'summary':node,'budget':app.memory.repo.values('memory_budget')}
        write(folder/'result.json',record);results.append({k:v for k,v in record.items() if k not in {'summary','budget'}})
        write(output/'summary.json',results)
    return results


MEMORIES=[('network','Connection reset by peer means the remote endpoint closed the TCP connection.'),
          ('transaction','数据库事务回滚会撤销本次事务中尚未提交的写入。'),('cancel','Async cancellation cleanup releases acquired resources in a finally block.'),
          ('bom','UTF-8-SIG decoding handles CSV files that contain a byte order mark.'),
          ('idempotent','An idempotency key prevents duplicate imports from creating duplicate records.'),
          ('lock','Dependency lock hashes bind a historical test observation to its original environment.'),
          ('snapshot','A task keeps its original knowledge snapshot; new memories are visible to subsequent tasks.'),
          ('empty','CSV_EMPTY_HEADER identifies a CSV input whose header row is empty.'),
          ('atomic','Atomic replacement writes a temporary file and replaces the destination after the write succeeds.'),
          ('utc','Timezone-aware UTC timestamps record absolute instants independent of display timezones.'),
          ('django4','Django 4 compatibility rule'),('django5','Django 5 compatibility rule')]

QUERIES=[('连接被远端强制关闭','network'),('重复导入不能生成两份订单','idempotent'),('读带字节顺序标记的表格','bom'),('撤销未提交的数据库更新','transaction'),
         ('取消异步任务后释放资源','cancel'),('别把旧环境的绿测当作现在通过','lock'),('先写临时文件再替换目标','atomic'),('天文星座亮度计算',None),
         ('Connection reset by peer','network'),('TCP连接被对端重置','network'),('连接重置错误','network'),('remote endpoint closed connection','network'),
         ('transaction rollback','transaction'),('事务失败后恢复原状','transaction'),('rollback uncommitted writes','transaction'),
         ('async cancellation cleanup','cancel'),('协程被取消时清理已获取的资源','cancel'),('finally resource cleanup','cancel'),
         ('UTF-8-SIG CSV','bom'),('CSV文件开头有BOM','bom'),('byte order mark decoding','bom'),
         ('idempotency key','idempotent'),('重复请求去重导入','idempotent'),('duplicate imports','idempotent'),
         ('dependency lock hash','lock'),('依赖版本变化后旧测试结果','lock'),('knowledge snapshot','snapshot'),('记忆激活下一轮生效','snapshot'),
         ('CSV_EMPTY_HEADER','empty'),('empty CSV header','empty'),('atomic replacement','atomic'),('原子替换文件','atomic'),
         ('timezone aware UTC','utc'),('统一保存世界协调时','utc'),('Django compatibility rule','django5'),
         ('calculate planetary gravity',None),('烹饪西红柿炒鸡蛋',None),('stock portfolio returns',None),('quantum spin measurement',None),('海洋潮汐预测',None)]


async def retrieval(output,encoder,profile,queries=None):
    queries=QUERIES if queries is None else queries
    source=output/'source';source.mkdir(parents=True)
    app=RepoCode(output/'host',ScriptedProvider(),'local',memory_profile=profile,memory_encoder=encoder)
    seed=app.new_task('Register scoped public project reference facts',source)
    ids={}
    for name,text in MEMORIES:
        app.store.append_message(seed['id'],{'role':'user','content':text})
        reference=app.memory.history.records(seed['id'])[-1]['ref']
        value=ClaimRecord(claim_id='fixture-'+name,kind='FACT',text=text,scope_id=seed['data']['manifest']['scope'],
                          support_refs=[EvidenceRef.model_validate(reference)],derived_from=[seed['id']],epistemic_status='USER_STATED',
                          preconditions={'django':'4' if name=='django4' else '5'} if name.startswith('django') else {})
        app.memory.consolidator.commit([value]);app.memory.consolidator.activate(value.claim_id,1,'fixture-reviewer',app.memory.repo.get('snapshot_heads',value.scope_id));ids[name]=value.claim_id
    write(output/'queries.json',[{'id':i,'query':q,'expected':ids.get(target),'split':'DEV' if i<8 else 'TEST'} for i,(q,target) in enumerate(queries)])
    # Threshold is calibrated ONLY from the first eight declared development queries.
    positives=[];negatives=[]
    texts=[text for _,text in MEMORIES]
    for q,target in queries[:8]:
        scores=encoder.rerank(q,texts)
        if target:positives.append(scores[[n for n,_ in MEMORIES].index(target)])
        else:negatives.append(max(scores))
    threshold=(min(positives)+max(negatives))/2
    calibration={'development_ids':list(range(8)),'positive_min':min(positives),'negative_max':max(negatives),'threshold':threshold,
                 'encoder':encoder.identity,'source':'DEV_ONLY','separable':min(positives)>max(negatives)}
    write(output/'calibration.json',calibration)
    tuned=app.memory.profile.model_copy(update={'rerank_min_score':threshold,'calibration_digest':digest(calibration)})
    app.memory.profile=tuned;app.memory.retriever.profile=tuned
    app.memory.compactor.profile=app.memory.consolidator.profile=tuned
    app.memory.profile_digest=app.store.json_blob(tuned)
    run=app.new_task('Debug current task step',source)
    results=[]
    for i,(query,target) in enumerate(queries):
        key,state=app.memory.reducer.reduce(run['id']);intent=app.memory.reducer.intent(run['id'],key,state,query)
        intent=intent.model_copy(update={'environment':{'django':'5'}})
        started=time.monotonic();hits,trace=await asyncio.to_thread(app.memory.retriever.retrieve,intent,{},6)
        expected=ids.get(target);ranks=[j+1 for j,h in enumerate(hits) if h['id']==expected and h['applicability']=='TRUE']
        results.append({'id':i,'split':'DEV' if i<8 else 'TEST','query':query,'expected':expected,'hits':hits,'trace':trace,
                        'recall_at_6':int(bool(ranks)) if target else None,'ndcg_at_6':1/__import__('math').log2(ranks[0]+1) if ranks else 0,
                        'no_match_correct':not hits if target is None else None,'seconds':time.monotonic()-started})
    write(output/'results.json',results)
    write(output/'summary.json',{'queries':len(queries),'test_queries':len(queries)-8,'test_recall_at_6':sum(r['recall_at_6'] for r in results[8:] if r['expected'])/sum(bool(r['expected']) for r in results[8:]),
        'test_no_match_correct':sum(r['no_match_correct'] for r in results[8:] if r['expected'] is None),
        'test_no_match_count':sum(r['expected'] is None for r in results[8:]),'mode':profile.mode,'encoder':encoder.identity})
    return tuned


async def protocol(output,p,runtime_profile=False,readonly=False):
    native=OpenAIProvider('deepseek-flash',base_url='https://api.deepseek.com',api_key_env='DEEPSEEK_API_KEY_REPO',thinking='disabled' if runtime_profile else 'enabled',max_tokens=4096 if runtime_profile else 2048,temperature=0 if runtime_profile else None)
    native.client.max_retries=0
    audited=AuditProvider(native,output,limit=5)
    tools=[{'name':'lookup','description':'Read a supplied public number','parameters':{'type':'object','properties':{},'required':[]}}]
    if runtime_profile:
        from repo_code.tool_models import definitions
        tools=definitions()
        if readonly:tools=[tool for tool in tools if tool['name']!='memory.propose']
    messages=[{'role':'user','content':'Call lookup first, then add 7 to the returned number. Do not guess the lookup result.'}]
    if runtime_profile:messages=[{'role':'user','content':'Call read on number.txt first, then add 7 to the returned number. Do not guess the file contents.'}]
    first=await audited.complete('Use the lookup tool to obtain the number.',messages,tools)
    if not first.tool_calls:raise RepoError('PROTOCOL_TOOL_CALL_NOT_OBSERVED')
    messages.append({'role':'assistant','content':first.text,'reasoning_content':first.reasoning_content,'tool_calls':[c.model_dump() for c in first.tool_calls]})
    for call in first.tool_calls:messages.append({'role':'tool','tool_call_id':call.id,'content':'35'})
    second=await audited.complete('Use the lookup tool to obtain the number.',messages,tools)
    # Rebase at a closed tool boundary, same model/thinking/tools profile.
    third=await audited.complete('Use the lookup tool to obtain the number.',[{'role':'user','content':'Authorized prior work summary: lookup returned 35. Add 7 and state the answer, without another lookup.'}],tools)
    result={'native_field_present':first.reasoning_content is not None,'round_trip_answer_42':'42' in second.text,
            'closed_boundary_rebase_answer_42':'42' in third.text,'native_reasoning_exported':False,
            'profile':model_configuration(native),'calls':len(audited.calls),'tool_schema_digest':digest(tools),
            'verified_profile_digests':[digest(model_configuration(native)),digest(model_configuration(audited))]}
    write(output/'result.json',result);return result


async def main_async(args):
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    profile=MemoryProfile.model_validate(json.loads(args.profile.read_text(encoding='utf-8')))
    encoder=LocalEncoder(profile.encoder)
    try:
        p=provider(output) if args.phase in {'replay','protocol','protocol-runtime','protocol-readonly'} else None
        if args.phase=='retrieval':
            tuned=await retrieval(output,encoder,profile);write(output/'calibrated-profile.json',tuned.model_dump())
        elif args.phase=='replay':await replay(output,p,encoder,profile)
        elif args.phase=='protocol':await protocol(output,p)
        elif args.phase=='protocol-runtime':await protocol(output,p,True)
        elif args.phase=='protocol-readonly':await protocol(output,p,True,True)
    finally:encoder.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=['retrieval','replay','protocol','protocol-runtime','protocol-readonly'],required=True)
    parser.add_argument('--profile',type=Path,default=ROOT/'examples/memory/profile.json')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();asyncio.run(main_async(args))


if __name__=='__main__':main()
