"""Three independent real DEV traces, three compressions each; not composites.

Source annotations are fixed before generation. This runner never executes
imported tool calls; it only replays messages and proposed plan metadata.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from memory_repair_dev import BudgetAuditProvider
from evaluate_memory_v2 import provider,write
from repo_code.context import groups
from repo_code.memory.models import MemoryProfile
from repo_code.service import RepoCode
from repo_code.models import Budget
from repo_code.util import digest,canonical

ROOT=Path(__file__).resolve().parents[1]
def _json(value):
    try:return json.loads(value)
    except (TypeError,ValueError):return None

ANNOTATIONS={
 'event-log-debug':['Preserve public APIs.','duplicate event IDs are ignored','failed batches roll back completely','account balances never become negative','Values are integer cents','keep replay(events) -> {account:balance}'],
 'historical-cache-refactor':['Preserve public APIs.','Keys preserve type distinctions (True, 1 and "1" differ)','nested dict order is irrelevant','expiry at now >= deadline','ttl=0 disables storage','values and inputs are defensively copied'],
 'dependency-invalidates-history':['Preserve public APIs.','dependency-policy.json is authoritative for current behavior','history.md describes an obsolete environment and must not override it','ROUND_HALF_UP','reject nonfinite and negative amounts','calculate percentage discounts before rounding once']}


async def main_async(args):
    args.output.mkdir(parents=True,exist_ok=False)
    samples=[]
    for name,anchors in ANNOTATIONS.items():
        if args.samples and name not in args.samples:continue
        source=ROOT/'reports/memory-v2-2026-11-01/e2-window16k'/name/'A/messages-visible.json'
        original=json.loads(source.read_text(encoding='utf-8'));stop=len(original)
        while stop:
            try:closed=groups(original[:stop]);break
            except Exception:stop-=1
        assert 30<=len(closed)<=60
        assert all(anchor in original[0]['content'] for anchor in anchors)
        samples.append({'id':name,'source':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'source_class':'SINGLE_REAL_DEV_TRACE_CLOSED_PREFIX','groups':len(closed),'excluded_open_tail_messages':len(original)-stop,
            'hard_constraints':[{'text':a,'original_message_index':0} for a in anchors],
            'original_messages':original[:stop],'learning_domain':'DEV_REPAIR','composite':False})
        samples[-1]['observations_before_generation']=[{'original_message_index':i,'exit_code':body.get('exit_code'),
            'is_error':body.get('is_error'),'source_snapshot':body.get('relevant_state'),'status_authority':'TOOL_OBSERVATION_NOT_DELIVERY'}
            for i,m in enumerate(original[:stop]) if m['role']=='tool' and isinstance((body:=_json(m.get('content',''))),dict) and ('exit_code' in body or body.get('is_error'))]
        samples[-1]['plan_proposals_before_generation']=[{'original_message_index':i,'steps':call['arguments']['steps'],'status':'PROPOSED_NOT_VERIFIED'}
            for i,m in enumerate(original[:stop]) for call in m.get('tool_calls',[]) if call['name']=='plan.update']
    write(args.output/'source-annotations-before-generation.json',samples)
    write(args.output/'source-lock.json',{f.relative_to(ROOT).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in (ROOT/'src').rglob('*.py')})
    native=provider(args.output).inner;p=BudgetAuditProvider(native,args.output,12,400000)
    profile=MemoryProfile(mode='lexical_only',max_tokens=16000,reserve_tokens=4096,safety_tokens=500,
        semantic_consolidation=False,auxiliary_calls=3,learning_enabled=False)
    summaries=[]
    for sample in samples:
        name=sample['id'];folder={'event-log-debug':'event','historical-cache-refactor':'cache','dependency-invalidates-history':'fee'}[name]
        out=args.output/folder;out.mkdir()
        source=ROOT/'reports/memory-v2-2026-11-01/e2-window16k'/name/'A/input'
        app=RepoCode(out/'host',p,'local',memory_profile=profile)
        run=app.new_task(sample['original_messages'][0]['content'],source,budget=Budget(model_calls=3,wall_seconds=600))
        gs=groups(sample['original_messages']);stages=[];last=0;node=None
        try:
            for boundary in [len(gs)//3,2*len(gs)//3,len(gs)]:
                for index in range(last,boundary):
                    for m in gs[index]:
                        if index==0 and m==sample['original_messages'][0]:continue
                        app.store.append_message(run['id'],m)
                    for m in gs[index]:
                        for call in m.get('tool_calls',[]):
                            if call['name']=='plan.update':app.store.put('plan',run['id'],{'steps':call['arguments']['steps']})
                last=boundary;records=app.memory.history.records(run['id']);_,state=app.memory.reducer.reduce(run['id'])
                error=None
                try:_,node=await app.memory.compactor.compact(run['id'],records[-1]['seq'],state)
                except Exception as exc:error={'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)[:300]}
                stages.append({'boundary_groups':boundary,'error':error,'summary':node,'host_state':state,'sources':records})
            projection_error=None;system='';messages=[];manifest=None
            try:system,messages,manifest=await app.memory.prepare_context(run['id'],[])
            except Exception as exc:projection_error={'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)[:300]}
            write(out/'effective-projection.json',{'system':system,'messages':messages,'manifest':manifest,'error':projection_error})
            row={'id':name,'directory':folder,'source_class':sample['source_class'],'groups':len(gs),'stages':stages,
                'hard_constraint_texts_in_effective_input':sum(a['text'] in system+canonical(messages).decode() for a in sample['hard_constraints']),
                'hard_constraint_count':len(sample['hard_constraints']),'model_calls':len([r for r in app.memory.repo.values('memory_budget') if r['run_id']==run['id']]),
                'original_sources_expandable':all(app.memory.history.resolve(run['id'],r['ref']['source_id']) for r in app.memory.history.records(run['id'])),
                'long_term_writeback':len(app.memory.repo.values('memory_versions')),'projection_error':projection_error}
            write(out/'result.json',row);summaries.append({k:v for k,v in row.items() if k!='stages'})
            write(args.output/'summary.json',summaries)
        finally:app.runtime.end(run['id'],'PARTIAL','Archived semantic-only DEV replay; no code delivery claimed');app.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--samples',nargs='+',choices=list(ANNOTATIONS))
    asyncio.run(main_async(p.parse_args()))


if __name__=='__main__':main()
