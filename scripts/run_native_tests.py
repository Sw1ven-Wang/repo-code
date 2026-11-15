#!/usr/bin/env python3
"""Run actual native-backend tests in a disposable, offline Linux container.

The Windows host never becomes root. No API credentials, Docker socket, home
directory, or writable project mount are passed to the Linux test host.
"""
from __future__ import annotations
import argparse
import asyncio
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
CAPABILITIES={'CHOWN','DAC_OVERRIDE','FOWNER','SETUID','SETGID','SYS_CHROOT','KILL'}


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def checked(command,**kwargs):
    result=subprocess.run(command,text=True,capture_output=True,**kwargs)
    if result.returncode:raise RuntimeError(f'{command[0]} failed ({result.returncode}): {result.stderr[-1500:]}')
    return result.stdout.strip()


def archive_source(output):
    archive=output/'source-input.tar';manifest=[]
    key=os.environ.get('OPENAI_API_KEY','').encode()
    with tarfile.open(archive,'w') as target:
        for name in ['src','tests','scripts','examples','docs','integrations','.github','pyproject.toml','README.md','LICENSE','SECURITY.md','CHANGELOG.md']:
            base=ROOT/name
            for path in sorted(base.rglob('*')) if base.is_dir() else [base]:
                if '__pycache__' in path.parts or path.suffix=='.pyc' or not path.is_file():continue
                if path.is_symlink():raise ValueError('Symlinks are not allowed in the test source snapshot')
                raw=path.read_bytes()
                if key and key in raw:raise ValueError('Credential detected in a source file; refusing to copy it')
                relative=path.relative_to(ROOT).as_posix()
                target.add(path,arcname=relative,recursive=False)
                manifest.append({'path':relative,'sha256':hashlib.sha256(raw).hexdigest()})
    write(output/'source-input-manifest.json',{'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'files':manifest})
    return archive


async def probe_native():
    from repo_code.storage import Store
    from repo_code.sandbox import Sandbox
    if sys.platform!='linux' or os.geteuid()!=0:raise RuntimeError('Linux root test host required')
    if any(k in os.environ for k in ['OPENAI_API_KEY','ANTHROPIC_API_KEY']):raise RuntimeError('Credentials must not enter the test host')
    # Root must be able to stop a process after dropping its UID.
    process=subprocess.Popen(['/usr/bin/python3','-c','import time;time.sleep(0.2)'],preexec_fn=lambda:os.setuid(22222))
    try:os.kill(process.pid,0)
    finally:process.wait(timeout=3)
    sandbox=Sandbox(Store('/work/native-cache/state'),'native');sandbox.prepare()
    work=Path('/work/probe');work.mkdir(exist_ok=True)
    code="import os,socket,json; denied=False\ntry:socket.socket()\nexcept OSError:denied=True\nprint(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'socket_denied':denied}))"
    result=await sandbox.run('native-probe',work,['python','-c',code],readonly=True,timeout=10)
    if result['exit_code']!=0:raise RuntimeError(result['stderr'])
    observation=json.loads(result['stdout'])
    if observation['uid']==0 or not observation['socket_denied']:raise RuntimeError('Native isolation probe failed')
    return sandbox,{'python':sys.version,'system_python':os.path.realpath('/usr/bin/python3'),
        'platform':platform.platform(),'uid':os.getuid(),'libseccomp':ctypes.util.find_library('seccomp'),
        'credentials_absent':True,'can_signal_dropped_uid':True,'probe':result,'environment':sandbox.environment()}


def inside(selection,timeout_seconds=600):
    destination=Path('/work/results');destination.mkdir(parents=True,exist_ok=True)
    sandbox,preflight=asyncio.run(probe_native());write(destination/'preflight.json',preflight)
    env=os.environ.copy();env.update(PYTHONPATH=str(ROOT/'src'),REPO_CODE_TEST_KIT=str(sandbox.kit),PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    if selection:
        nodes=json.loads(Path('/input/selection.json').read_text(encoding='utf-8'))
        if not nodes or len(nodes)!=len(set(nodes)):raise ValueError('Selection must contain unique node IDs')
        for node in nodes:
            if not isinstance(node,str) or not node.startswith('tests/') or '::' not in node:raise ValueError('Invalid test selector')
            if not (ROOT/node.split('::')[0]).resolve().is_relative_to(ROOT):raise ValueError('Test selector escapes the project')
        xml=destination/'native-selected.xml'
        command=[sys.executable,'-m','pytest','-p','pytest_asyncio.plugin','-o','asyncio_default_fixture_loop_scope=function',
                 '-q','--tb=short','--junitxml='+str(xml),*nodes]
    else:
        nodes=None
        command=[sys.executable,str(ROOT/'scripts/validate.py'),'--suite','native','--timeout-seconds',str(timeout_seconds)]
        xml=ROOT/'reports/validation-1.xml'
    started=time.monotonic();code=124
    with (destination/'tests.log').open('w',encoding='utf-8') as log:
        try:code=subprocess.run(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=timeout_seconds+100).returncode
        except subprocess.TimeoutExpired:log.write('\nValidation command exceeded its total timeout.\n')
    if not selection:
        for pattern in ['validation-*','design-reference*']:
            for path in (ROOT/'reports').glob(pattern):shutil.copy2(path,destination/path.name)
    counts={name:0 for name in ['cases','failures','errors','skipped']}
    if xml.exists():
        parsed=ET.parse(xml).getroot()
        counts={name:len(list(parsed.iter(tag))) for name,tag in [('cases','testcase'),('failures','failure'),('errors','error'),('skipped','skipped')]}
    accepted=code==0 and counts['cases']>0 and not any(counts[k] for k in ['failures','errors','skipped'])
    if nodes is not None:accepted &= counts['cases']==len(nodes)
    report={'suite':'selected-native' if nodes else 'native-core','accepted':bool(accepted),'returncode':code,
            'seconds':round(time.monotonic()-started,3),'counts':counts,'command':command,
            'excluded_test_files':[], 'excluded_backend_marker':None if nodes else 'docker_backend',
            'note':'Docker-backend tests are a separate backend suite; no Docker socket is available here.'}
    write(destination/'summary.json',report);print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if accepted else 1


def host(args):
    output=args.output.resolve()
    if output.exists() and any(output.iterdir()):raise ValueError('Use a new/empty output directory')
    output.mkdir(parents=True,exist_ok=True)
    profile_path=ROOT/'examples/sandbox/native-validation-config.json'
    profile=json.loads(profile_path.read_text(encoding='utf-8'))
    if set(profile['capabilities'])!=CAPABILITIES:raise ValueError('Unexpected native test host capability set')
    if checked(['docker','info','--format','{{.OSType}}'],timeout=20)!='linux':raise RuntimeError('Docker Linux engine required')
    if args.build:
        base=json.loads((ROOT/'examples/sandbox/validation-config.json').read_text(encoding='utf-8'))
        digests=json.loads(checked(['docker','image','inspect',profile['base_tag'],'--format','{{json .RepoDigests}}'],timeout=20))
        if base['image'] not in digests:raise RuntimeError('Local base tag differs from the reviewed pinned sandbox image')
        build=['docker','buildx','build','--load','--pull=false','--tag','repo-code-native-validation:1.5.1',
               '--build-arg','BASE_IMAGE='+profile['base_tag'],'--file',str(ROOT/'examples/sandbox/Dockerfile.native-validation'),
               '--metadata-file',str(output/'image-build-metadata.json'),str(ROOT/'examples/sandbox')]
        with (output/'image-build.log').open('w',encoding='utf-8') as log:
            built=subprocess.run(build,stdout=log,stderr=subprocess.STDOUT,timeout=600)
        if built.returncode:raise RuntimeError('Native image build failed; see image-build.log')
        refs=json.loads(checked(['docker','image','inspect','repo-code-native-validation:1.5.1','--format','{{json .RepoDigests}}'],timeout=20))
        profile['image']=next(r for r in refs if r.startswith('repo-code-native-validation@sha256:'))
        write(profile_path,profile)
    if not re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}',profile['image']):raise ValueError('Repository digest required')
    checked(['docker','image','inspect',profile['image']],timeout=20)
    source=archive_source(output);write(output/'profile.json',profile)
    command=['docker','run','--detach','--init','--user','0:0','--network','none','--cap-drop','ALL',
             '--security-opt','no-new-privileges','--memory','4g','--cpus','2','--pids-limit','512',
             '--mount',f'type=bind,source={source},target=/input/source.tar,readonly']
    for capability in sorted(CAPABILITIES):command+=['--cap-add',capability]
    if args.selection:
        command+=['--mount',f'type=bind,source={args.selection.resolve()},target=/input/selection.json,readonly']
    command += [profile['image'],'sleep',str(args.timeout_seconds+600)]
    container=checked(command,timeout=30)
    if not re.fullmatch(r'[a-f0-9]{64}',container):raise RuntimeError('Unexpected container ID')
    write(output/'container.json',{'id':container,'image':profile['image'],'command':command})
    try:
        checked(['docker','exec',container,'python','-c',"import pathlib,tarfile;p=pathlib.Path('/work/project');p.mkdir();tarfile.open('/input/source.tar').extractall(p,filter='data')"],timeout=30)
        print('Linux native test host started; source is copied from a read-only archive. No model calls.',flush=True)
        execute=['docker','exec','--env','PYTHONPATH=/work/project/src',container,'python','/work/project/scripts/run_native_tests.py','--inside','--timeout-seconds',str(args.timeout_seconds)]
        if args.selection:execute+=['--selection','/input/selection.json']
        with (output/'driver.log').open('w',encoding='utf-8') as log:
            result=subprocess.run(execute,stdout=log,stderr=subprocess.STDOUT,timeout=args.timeout_seconds+150)
        checked(['docker','cp',container+':/work/results/.',str(output)],timeout=30)
        print((output/'summary.json').read_text(encoding='utf-8'),flush=True)
        return result.returncode
    finally:
        checked(['docker','rm','--force',container],timeout=30)
        write(output/'cleanup.json',{'container':container,'removed':True})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--selection',type=Path,help='Optional exact native test node IDs in a JSON list')
    parser.add_argument('--timeout-seconds',type=int,default=600,help='Offline suite deadline; independent of model/Agent budgets')
    parser.add_argument('--build',action='store_true',help='Build system Python/libseccomp test-host image; records its actual digest')
    parser.add_argument('--inside',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if not 60<=args.timeout_seconds<=3600:parser.error('--timeout-seconds must be 60..3600')
    if args.inside:return inside(args.selection,args.timeout_seconds)
    if args.output is None:parser.error('--output is required')
    return host(args)


if __name__=='__main__':raise SystemExit(main())
