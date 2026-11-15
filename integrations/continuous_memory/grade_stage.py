"""Trusted Docker stage grader. It never starts Solver or accesses credentials."""
import argparse
import json
from pathlib import Path
import re
import uuid
import xml.etree.ElementTree as ET
from repo_code.util import RepoError


def grade(task,received_turn,candidate,grader_directory,output,image):
    if task not in {'ledgerflow','queuecraft'} or not 1<=received_turn<=37:raise RepoError('INVALID_GRADING_STAGE')
    if not re.fullmatch(r'(?:\S+@)?sha256:[a-f0-9]{64}',image):raise RepoError('PINNED_GRADER_IMAGE_REQUIRED')
    candidate=Path(candidate).resolve();grader_directory=Path(grader_directory).resolve();output=Path(output).resolve()
    if grader_directory.is_relative_to(candidate) or output.is_relative_to(candidate):raise RepoError('GRADER_ISOLATION_REQUIRED')
    output.mkdir(parents=True,exist_ok=False)
    files=['/grader/stage_checks.py']
    if received_turn==37:files.extend(['/grader/test_'+task+'_contract.py','/grader/hardening_checks.py'])
    import docker
    client=docker.from_env(timeout=180)
    normal=lambda p:str(p)[4:] if str(p).startswith('\\\\?\\') else str(p)
    container=client.containers.create(image,command=['sleep','infinity'],network_mode='none',read_only=True,
        user='65534:65534',mem_limit='2g',nano_cpus=2_000_000_000,pids_limit=128,cap_drop=['ALL'],
        security_opt=['no-new-privileges:true'],tmpfs={'/tmp':'rw,nosuid,nodev,size=256m,mode=1777'},
        volumes={normal(candidate):{'bind':'/candidate','mode':'ro'},normal(grader_directory):{'bind':'/grader','mode':'ro'}},
        environment={'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':'/candidate','RC_CONTINUOUS_TASK':task,
                     'RC_CONTINUOUS_DELIVERED_TURN':str(received_turn)},name='rc-stage-'+uuid.uuid4().hex[:10])
    try:
        container.start()
        result=container.exec_run(['python','-m','pytest',*files,'-q','-p','no:cacheprovider','--basetemp','/tmp/checks','--junitxml=/tmp/report.xml'],workdir='/tmp')
        (output/'output.log').write_bytes(result.output)
        xml=container.exec_run(['cat','/tmp/report.xml'])
        if xml.exit_code:raise RepoError('GRADER_REPORT_MISSING')
        (output/'report.xml').write_bytes(xml.output)
        cases=ET.fromstring(xml.output).findall('.//testcase')
        failed=sum(c.find('failure') is not None for c in cases);errors=sum(c.find('error') is not None for c in cases)
        skipped=sum(c.find('skipped') is not None for c in cases);passed=len(cases)-failed-errors-skipped
        report={'task':task,'received_turn':received_turn,'protocol_version':2,'exit_code':result.exit_code,
                'passed':passed,'failed':failed,'errors':errors,'skipped':skipped,'total':len(cases),
                'all_passed':result.exit_code==0 and passed>0 if passed+failed+errors else None,
                'status':'ASSESSED' if passed+failed+errors else 'NOT_ASSESSED',
                'scope':'TERMINAL' if received_turn==37 else 'RECEIVED_STAGE_ONLY',
                'image':image,'read_only_candidate':True,'read_only_grader':True,'network':'none','paid_model_calls':0}
        (output/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        return report
    finally:
        container.remove(force=True,v=True);client.close()


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for key in ['task','candidate','grader-directory','output','image']:p.add_argument('--'+key,required=True)
    p.add_argument('--received-turn',type=int,required=True);a=p.parse_args()
    print(json.dumps(grade(a.task,a.received_turn,a.candidate,a.grader_directory,a.output,a.image),ensure_ascii=False))
