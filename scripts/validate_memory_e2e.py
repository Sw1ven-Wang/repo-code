"""Frozen real-provider A/D trials with one-shot private cohorts and fresh state.

Curated task files/oracles are supplied separately, never embedded here. No
publication, approval or default activation is performed by this runner.
"""
from __future__ import annotations
import argparse,asyncio,contextlib,hashlib,json,shutil,sqlite3,tempfile
from pathlib import Path
from repo_code.service import RepoCode
from repo_code.providers import ScriptedProvider,model_configuration
from repo_code.memory.models import MemoryProfile
from repo_code.memory.working_state import semantic_message
from repo_code.governance import HoldoutVault
from repo_code.regression import CandidateRegression,validate_g4_report
from repo_code.evaluation import TrialRunner
from repo_code.maintenance import Maintenance
from repo_code.models import Candidate
from repo_code.util import RepoError,digest
from evaluate_memory_v2 import provider,write
from memory_repair_dev import BudgetAuditProvider
from memory_task_trials import tasks as dev_tasks
from migrate_memory_v2 import ReadOnlySource

ROOT=Path(__file__).resolve().parents[1]


def source_files():
    files={}
    for name in ['src','tests','scripts','examples','docs','.github','README.md','CHANGELOG.md','pyproject.toml','LICENSE','SECURITY.md']:
        base=ROOT/name
        for path in sorted(base.rglob('*')) if base.is_dir() else [base]:
            if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':files[path.relative_to(ROOT).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def short_dir(prefix):return Path(tempfile.mkdtemp(prefix=prefix,dir=ROOT/'.venv'))


def clone(source,destination):
    destination.mkdir()
    source=ReadOnlySource(source)
    with contextlib.closing(source.connect()) as origin,contextlib.closing(sqlite3.connect(destination/'runtime.db')) as target:origin.backup(target)
    shutil.copy2(source.root/'host.key',destination/'host.key');shutil.copytree(source.artifact_root,destination/'artifacts')


async def prepare(args):
    args.output.mkdir(parents=True,exist_ok=False)
    bank=json.loads(args.bank.read_text(encoding='utf-8'))
    if sorted(t['role'] for t in bank)!=['final','final','promotion','promotion','promotion','promotion']:raise RepoError('COHORT_ROLE_MISMATCH')
    root=short_dir('m2e-');image=json.loads((ROOT/'examples/sandbox/validation-config.json').read_text())['image']
    profile=MemoryProfile.model_validate(json.loads(args.profile.read_text(encoding='utf-8'))).model_copy(update={
        'learning_enabled':False,'auxiliary_calls':None,'max_tokens':16000,'reserve_tokens':4096,'safety_tokens':500,'total_token_budget':500000})
    app=RepoCode(root/'controller',ScriptedProvider(),'docker',image,memory_profile=profile)
    candidate=app.releases.candidate('memory-v21-response-recovery',{'context_policy':{'kind':'config','max_tokens':16000,'reserve_tokens':4096}},
        'Frozen Memory 2.1 Host response recovery, no cross-task learning, same 16k request budgets',['e2e-source-freeze'])
    vault=HoldoutVault(app.store);cohorts={}
    budget={'model_calls':None,'tool_calls':100,'wall_seconds':600}
    for role in ['dev','promotion','final']:
        selected=[t for t in dev_tasks() if t['id']=='cross-module-csv'] if role=='dev' else [t for t in bank if t['role']==role]
        selected=[{**{k:v for k,v in t.items() if not k.startswith('_') and k!='role'},'budget':budget} for t in selected]
        cohorts[role]=vault.register('memory-v21-response-'+role,role,selected,family='memory-v21-response-'+role)
    complexity=app.releases.governor.check(app.releases.components(candidate.candidate_release_digest))
    app.releases.record_gate(candidate,'G0',{'passed':True,'complexity':complexity,'scope':'schema and complexity'})
    native=provider(args.output).inner
    lock={'source_files':source_files(),'candidate':candidate.model_dump(mode='json'),'release':app.releases.get(candidate.candidate_release_digest).model_dump(mode='json'),
        'state_root':str(root/'controller'),'template':str(root/'template'),'trial_root':str(root/'trials'),'cohorts':cohorts,
        'memory_profile':app.memory.profile.model_dump(mode='json'),'model':model_configuration(native),'profile_digest':app.memory.profile_digest,
        'image':image,'environment_digest':app.sandbox.environment_digest,'per_run_budget':budget,'per_run_token_cap':500000,
        'total_new_cohort_token_cap':6000000,'model_call_cap':None,'auxiliary_call_cap':None,'context_window':16000,'output_reserve':4096,'safety':500,
        'active_input':11404,'shared_learning':False,'provider_factory_fields':['id','goal','files'],'oracle_to_model':False,
        'authorization':'User explicitly authorized independent PROMOTION/FINAL and no call-count limit in current turn',
        'final_even_if_promotion_fails':'User requested complete FINAL measurement; G4b gate remains false if prerequisite G3 fails. No approval/activation.',
        'quality_rule':'No Pass-to-Fail, positive candidate passes, model+tool cost<=2x. PROMOTION requires F2P or equal passes with lower cost; FINAL requires prior G3.',
        'source_class':'AUTHORED_PROGRAMMING_REPOSITORIES_WITH_REAL_DEEPSEEK_PROVIDER','tool_difference':'v1 original tools; v2 readonly memory/history tools. Same permission grants; recorded as treatment difference.'}
    write(args.output/'evaluation-lock.json',lock)
    Maintenance(app.store).backup(root/'template')
    write(args.output/'template-files.json',{p.relative_to(root/'template').as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'template').rglob('*') if p.is_file()})
    app.close();await native.client.close();print('Frozen one candidate and 1 DEV + 4 PROMOTION + 2 FINAL tasks; no model call yet',flush=True)


async def phase(args):
    lock=json.loads((args.output/'evaluation-lock.json').read_text(encoding='utf-8'))
    if lock['source_files']!=source_files():raise RepoError('FROZEN_SOURCE_CHANGED')
    profile=MemoryProfile.model_validate(lock['memory_profile'])
    app=RepoCode(lock['state_root'],ScriptedProvider(),'docker',lock['image'],memory_profile=profile)
    candidate=Candidate.model_validate(lock['candidate']);vault=HoldoutVault(app.store)
    out=args.output/args.phase;out.mkdir(exist_ok=False)
    if args.phase=='g4':
        report=await CandidateRegression(app).run(candidate)
        assert validate_g4_report(report,candidate.candidate_release_digest,app.releases.get(candidate.candidate_release_digest).components,app.sandbox.environment_digest)
        app.releases.record_gate(candidate,'G4',report);write(out/'report.json',report);app.close();print('G4 passed all 12 cases',flush=True);return
    key=lock['cohorts'][args.phase]
    if args.phase!='dev':
        dev=json.loads((args.output/'dev/report.json').read_text())
        if not dev['passed']:raise RepoError('DEV_GATE_FAILED')
        vault.claim(candidate.id,key,'G3' if args.phase=='promotion' else 'G4b')
    cohort=vault.open(key,vault.evaluator_capability() if args.phase!='dev' else None)
    rows=[];costs=[]
    try:
        for task in cohort['tasks']:
            for arm in ['A','D']:
                case=out/(task['id']+'-'+arm);case.mkdir()
                paid=BudgetAuditProvider(provider(case).inner,case,None,500000)
                shell=short_dir('m2t-')/'host';clone(lock['template'],shell)
                service=RepoCode(shell,ScriptedProvider(),'docker',lock['image'],memory_profile=profile,memory_encoder=app.memory.encoder)
                captured=[]
                def factory(public):
                    assert set(public)<=set(lock['provider_factory_fields'])
                    captured.append(digest(public));return paid
                try:
                    result=await TrialRunner(service,factory,True,lock['trial_root']).run(task,candidate.candidate_release_digest,key,args.phase!='dev',memory_profile=None if arm=='A' else profile)
                    state=ReadOnlySource(result['state_root'])
                    if result.get('run_id'):
                        write(case/'events.json',state.events(result['run_id'],trusted=True,limit=10000))
                        write(case/'messages-visible.json',[semantic_message(m) for m in state.messages(result['run_id'])])
                        with contextlib.closing(state.connect()) as c:
                            summaries=c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='summary_nodes'").fetchone()
                            result['compactions']=len(c.execute('SELECT id FROM summary_nodes').fetchall()) if summaries else 0
                    result['arm']=arm;result['provider_public_input_digests']=captured
                    result['external_budget']=json.loads((case/'external-budget-ledger.json').read_text()) if (case/'external-budget-ledger.json').exists() else {'reservations':[]}
                    rows.append(result);write(case/'result.json',result);write(out/'progress.json',rows)
                finally:service.close();await paid.inner.client.close()
        pairs=[(rows[i],rows[i+1]) for i in range(0,len(rows),2)]
        old=sum(a['passed'] for a,b in pairs);new=sum(b['passed'] for a,b in pairs)
        f2p=[a['task_id'] for a,b in pairs if not a['passed'] and b['passed']];p2f=[a['task_id'] for a,b in pairs if a['passed'] and not b['passed']]
        cost=lambda r:r['cost'].get('model_calls',0)+r['cost'].get('tool_calls',0)
        ca=sum(cost(a) for a,b in pairs);cd=sum(cost(b) for a,b in pairs)
        unknown=sum(r['usage_status']=='USAGE_UNKNOWN' for trial in rows for r in trial['external_budget']['reservations'])
        overflow=sum(bool(r.get('actual_context_budget_violation')) for trial in rows for r in trial['external_budget']['reservations'])
        infra=any(r['state']=='INFRASTRUCTURE_ERROR' for r in rows)
        measured=not infra and not overflow and not unknown and new>0 and not p2f and new>=old and cd<=max(ca,1)*2
        quality=measured and (bool(f2p) or new==old and cd<ca) if args.phase=='promotion' else measured
        prerequisite=True
        if args.phase=='final':prerequisite=json.loads((args.output/'promotion/report.json').read_text())['passed']
        report={'passed':bool(quality and prerequisite),'quality_passed':bool(quality),'prerequisites_passed':prerequisite,'evidence_class':'MODEL_TASK_EVALUATION',
            'cohort':key,'tasks':len(pairs),'baseline_passed':old,'candidate_passed':new,'fail_to_pass':f2p,'pass_to_fail':p2f,'cost':{'baseline':ca,'candidate':cd},
            'unknown_usage_calls':unknown,'actual_context_overflows':overflow,'infrastructure_errors':sum(r['state']=='INFRASTRUCTURE_ERROR' for r in rows),'trials':[r['id'] for r in rows],
            'cross_task_learning':False,'source_frozen':lock['source_files']==source_files(),'completed_pairs':len(pairs)}
        write(out/'report.json',report)
        for gate in ['G1','G2'] if args.phase=='dev' else ['G3'] if args.phase=='promotion' else ['G4b']:app.releases.record_gate(candidate,gate,report)
        if args.phase=='dev':vault.mark_exposure(candidate.id,key)
        else:vault.notice(candidate.id,key,report['passed'],'PASSED' if report['passed'] else 'QUALITY')
        print(json.dumps({'phase':args.phase,'completed_pairs':len(pairs),'passed':report['passed'],'quality_passed':report['quality_passed']},indent=2),flush=True)
    finally:app.close()
    original=json.loads((args.output/'template-files.json').read_text())
    assert all(hashlib.sha256((Path(lock['template'])/p).read_bytes()).hexdigest()==v for p,v in original.items())


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--phase',choices=['prepare','dev','g4','promotion','final'],required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--bank',type=Path)
    parser.add_argument('--profile',type=Path,default=ROOT/'examples/memory/deepseek-experimental.json')
    args=parser.parse_args()
    if args.phase=='prepare' and args.bank is None:parser.error('--bank is required for prepare')
    asyncio.run(prepare(args) if args.phase=='prepare' else phase(args))

if __name__=='__main__':main()
