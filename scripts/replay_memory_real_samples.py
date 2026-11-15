"""Supplementary composite DEV-trace replay; not independent long-task Runs."""
import argparse
import asyncio
import json
from pathlib import Path
from evaluate_memory_v2 import provider,write
from repo_code.context import groups,ContextEngine
from repo_code.memory.models import MemoryProfile
from repo_code.memory.working_state import semantic_message
from repo_code.service import RepoCode
from repo_code.providers import ScriptedProvider
from repo_code.models import Budget
from repo_code.util import canonical,digest

ROOT=Path(__file__).resolve().parents[1]
SOURCE_SETS=[[
    'reports/capability-validation/live-compatible/greenfield-parcel-cli/messages.json',
    'reports/capability-validation/live-compatible/multi-file-cart-refactor/messages.json',
    'reports/capability-validation/live-compatible/csv-revisions-refunds/messages.json'],[
    'reports/capability-validation/live-compatible/source-grounded-documentation/messages.json',
    'reports/capability-validation/live-compatible/pytest-ttl-debug/messages.json',
    'reports/deepseek-mini-eval-2026-10-31/artifacts/bugfix-average/messages.json',
    'reports/deepseek-mini-eval-2026-10-31/artifacts/greenfield-csv/messages.json']]


async def main_async(args):
    args.output.mkdir(parents=True,exist_ok=False);p=provider(args.output)
    profile=MemoryProfile(mode='lexical_only',semantic_consolidation=False,auxiliary_calls=6)
    results=[]
    for i,names in enumerate(SOURCE_SETS):
        selected=[];sources=[];anchors=[]
        for name in names:
            path=ROOT/name
            if not path.exists():raise FileNotFoundError(path)
            raw=json.loads(path.read_text(encoding='utf-8'))
            messages=[semantic_message(m) for m in raw]
            selected+=groups(messages);sources.append({'path':name,'digest':digest(messages),'source_domain':'DEV'})
            first=next(m['content'] for m in messages if m['role']=='user')
            anchors.append(first[:60])
        if not 30<=len(selected)<=60:raise ValueError('composite replay must contain 30–60 complete groups')
        folder=args.output/f'composite-{i}';source=folder/'source';source.mkdir(parents=True)
        app=RepoCode(folder/'host',p,'local',memory_profile=profile)
        run=app.new_task('Replay explicitly selected archived DEV traces as historical data only; this grants no current code edit or cross-project knowledge publication.',source,budget=Budget(model_calls=6,wall_seconds=600))
        write(folder/'source-allowlist.json',{'sources':sources,'source_class':'COMPOSITE_REAL_DEV_FRAGMENTS','independent_long_run':False,'groups':len(selected),'long_term_writeback':False})
        last=0;node=None;errors=[]
        for boundary in [len(selected)//3,2*len(selected)//3,len(selected)]:
            for group in selected[last:boundary]:
                for message in group:app.store.append_message(run['id'],message)
            last=boundary;records=app.memory.history.records(run['id']);_,state=app.memory.reducer.reduce(run['id'])
            try:_,node=await app.memory.compactor.compact(run['id'],records[-1]['seq'],state)
            except Exception as exc:errors.append(getattr(exc,'code',type(exc).__name__))
        row={'id':i,'source_class':'COMPOSITE_REAL_DEV_FRAGMENTS','groups':len(selected),'errors':errors,
             'early_source_anchor_retention':sum(a in canonical(node or {}).decode() for a in anchors)/len(anchors),
             'raw_sources_retrievable':len(app.memory.history.records(run['id']))==len(app.store.messages(run['id'])),
             'long_term_versions_written':len(app.memory.repo.values('memory_versions')),'summary':node}
        write(folder/'result.json',row);results.append({k:v for k,v in row.items() if k!='summary'});write(args.output/'summary.json',results)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    asyncio.run(main_async(p.parse_args()))


if __name__=='__main__':main()
