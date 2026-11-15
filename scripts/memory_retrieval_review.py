"""R3 frozen old-query regression and new read-only queries. Zero API calls."""
import argparse
import asyncio
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time
from evaluate_memory_v2 import retrieval,QUERIES,write
from repo_code.memory.models import MemoryProfile
from repo_code.memory.encoders import LocalEncoder
from repo_code.context import estimate_tokens

ROOT=Path(__file__).resolve().parents[1]


async def main_async(args):
    args.output.mkdir(parents=True,exist_ok=False)
    locked=json.loads(args.query_lock.read_text(encoding='utf-8'))
    new=[(x['query'],x['expected'].removeprefix('fixture-') if x['expected'] else None) for x in locked['queries']]
    profile=MemoryProfile.model_validate(json.loads(args.profile.read_text(encoding='utf-8')));encoder=LocalEncoder(profile.encoder)
    summaries=[];started=time.monotonic()
    try:
        for name,queries in [('old',QUERIES),('new',QUERIES[:8]+new)]:
            folder=args.output/name;folder.mkdir()
            await retrieval(folder,encoder,profile,queries)
            rows=json.loads((folder/'results.json').read_text(encoding='utf-8'))[8:]
            positive=[r for r in rows if r['expected']];negative=[r for r in rows if not r['expected']]
            times=sorted(r['seconds'] for r in rows);hits=[h for r in rows for h in r['hits']]
            with sqlite3.connect(folder/'host/runtime.db') as c:
                builds=[json.loads(x[0]) for x in c.execute('SELECT data FROM memory_control') if json.loads(x[0]).get('index_build')]
            summary={'cohort':name,'queries':len(rows),'answerable':len(positive),'recall_at_6':sum(r['recall_at_6'] for r in positive)/len(positive),
                'ndcg_at_6_answerable':statistics.mean(r['ndcg_at_6'] for r in positive),
                'no_match_correct':sum(r['no_match_correct'] for r in negative),'no_match_count':len(negative),
                'relevant_applicable_precision':sum(h['id']==r['expected'] and h['applicability']=='TRUE' for r in rows for h in r['hits'])/max(1,len(hits)),
                'false_applicability_injections':sum(h['applicability']=='FALSE' for h in hits),'unknown_returned':sum(h['applicability']=='UNKNOWN' for h in hits),
                'p50_seconds':statistics.median(times),'p95_seconds':times[math.ceil(.95*len(times))-1],
                'visible_evidence_estimated_tokens':sum(estimate_tokens(h) for h in hits),'index_build_seconds':sum(r['seconds'] for r in builds),
                'index_encoder_seconds':sum(r.get('encoder_seconds',0) for r in builds),'encoder':encoder.identity,'paid_calls':0,
                'used_to_tune':False,'calibration':'FIRST_EIGHT_ORIGINAL_DEV_ONLY','new_queries_lock':str(args.query_lock)}
            summaries.append(summary);write(args.output/'retrieval-benchmark.json',summaries)
    finally:encoder.close()
    write(args.output/'runtime.json',{'seconds':time.monotonic()-started,'encoder_seconds':encoder.seconds,'paid_calls':0})
    print(json.dumps(summaries,indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--query-lock',type=Path,required=True)
    p.add_argument('--profile',type=Path,default=ROOT/'examples/memory/profile.json')
    asyncio.run(main_async(p.parse_args()))


if __name__=='__main__':main()
