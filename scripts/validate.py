#!/usr/bin/env python3
"""Bounded offline validation; skipped isolation is not full acceptance."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--repeat',type=int,default=1)
    parser.add_argument('--timeout-seconds',type=int,default=600,help='Offline suite deadline; does not change Agent budgets or acceptance criteria')
    parser.add_argument('--allow-skips',action='store_true',help='Partial developer run only, not release acceptance')
    parser.add_argument('--suite',choices=['all','native','docker'],default='all',help='Explicit backend scope; native excludes the separately opt-in Docker test file')
    args=parser.parse_args()
    if not 1<=args.repeat<=10:parser.error('--repeat must be 1..10')
    if not 60<=args.timeout_seconds<=3600:parser.error('--timeout-seconds must be 60..3600')
    root=Path(__file__).resolve().parents[1]
    reports=root/'reports';reports.mkdir(exist_ok=True)
    env=os.environ.copy();env['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1';env['PYTHONPATH']=str(root/'src')
    summary=[]
    for i in range(1,args.repeat+1):
        xml=reports/f'validation-{i}.xml';log=reports/f'validation-{i}.log';start=time.monotonic()
        selection=['tests']
        if args.suite=='docker':selection+=['-m','docker_backend']
        if args.suite=='native':selection+=['-m','not docker_backend']
        command=[sys.executable,'-m','pytest','-p','pytest_asyncio.plugin',*selection,'-q','--tb=short','--junitxml='+str(xml)]
        with log.open('w',encoding='utf-8') as out:
            try:result=subprocess.run(command,cwd=root,env=env,stdout=out,stderr=subprocess.STDOUT,timeout=args.timeout_seconds);code=result.returncode
            except subprocess.TimeoutExpired:code=124
        counts={'cases':0,'skipped':0,'failures':0,'errors':0}
        if xml.exists():
            tree=ET.parse(xml)
            counts={name:len(tree.findall('.//'+tag)) for name,tag in [('cases','testcase'),('skipped','skipped'),('failures','failure'),('errors','error')]}
        accepted=code==0 and counts['cases']>0 and (args.allow_skips or counts['skipped']==0)
        summary.append({'round':i,'suite':args.suite,'returncode':code,'seconds':round(time.monotonic()-start,3),'accepted':accepted,**counts,'backend_selection':'docker_backend' if args.suite=='docker' else 'not docker_backend' if args.suite=='native' else 'all','command':command})
        print(log.read_text(encoding='utf-8'),end='')
        (reports/'validation-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        if not accepted:return 1
    # The original reference file is unittest-based and is not part of the
    # implementation pytest testpath. Execute it separately, without mixing counts.
    ref_log=reports/'design-reference.log'
    with ref_log.open('w',encoding='utf-8') as out:
        reference=subprocess.run([sys.executable,'tests/design_reference.py'],cwd=root,env=env,stdout=out,stderr=subprocess.STDOUT,timeout=60)
    (reports/'design-reference-status.json').write_text(json.dumps({'returncode':reference.returncode,'kind':'design reference, not implementation tests'},indent=2))
    print(ref_log.read_text(encoding='utf-8'),end='')
    return 0 if reference.returncode==0 else 1

if __name__=='__main__':raise SystemExit(main())
