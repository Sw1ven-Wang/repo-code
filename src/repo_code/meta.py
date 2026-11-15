"""Frozen L2 -> optimizer proposals -> L1 search -> L0 trials, under one budget.

Builders never receive private episodes. Frozen instances get only the current
public failure evidence and DEV tasks. Scoring is external and occurs after
all optimizer/candidate selection has finished. Each arm uses a fresh Store.
"""
from __future__ import annotations
import contextlib
import asyncio
import json
import tempfile
import time
from pathlib import Path
from .models import Candidate, Release
from .service import RepoCode
from .evaluation import SuiteEvaluator
from .governance import HoldoutVault, Lineage
from .evolution import EvolutionEngine
from .evolved import EvolvedWorker
from .util import RepoError, digest, uid

class CountingProvider:
    def __init__(self,inner,ledger): self.inner,self.ledger=inner,ledger
    async def complete(self,*args,**kwargs):
        if self.ledger['calls'] >= self.ledger['limit']: raise RepoError('META_TOTAL_BUDGET')
        self.ledger['calls']+=1
        from .providers import is_test_double
        if is_test_double(self.inner):self.ledger['synthetic']=True
        result=await self.inner.complete(*args,**kwargs)
        self.ledger['usage'].append(result.usage)
        return result

class MetaEvaluator:
    """Trusted nested evaluator. provider_factory(public, purpose) never sees oracle."""
    def __init__(self,service,vault,provider_factory):
        self.service,self.store,self.vault,self.provider_factory=service,service.store,vault,provider_factory
        self.cap=vault.evaluator_capability()

    def _public_problem(self,problem):
        # Deliberate allowlist; hidden task inputs and contract expected values omitted.
        return {k:problem[k] for k in ('id','description','evidence','budget') if k in problem}

    async def episode(self,problem,optimizer_release,cohort,hidden,*,synthetic=False):
        start=time.monotonic();public=self._public_problem(problem)
        limit=min(int(problem.get('budget',{}).get('model_calls',80)),1000)
        seconds=min(float(problem.get('budget',{}).get('wall_seconds',300)),3600)
        ledger={'calls':0,'limit':limit,'usage':[]};attempts=[];status='FAILED';score=None
        # Each arm/episode has fresh mutable state, knowledge and candidate archives.
        with tempfile.TemporaryDirectory(prefix='meta-episode-',dir=self.store.root) as td:
            provider=CountingProvider(self.provider_factory(public,'optimizer'),ledger)
            app=RepoCode(Path(td)/'state',provider,sandbox=self.service.sandbox.mode,image=self.service.sandbox.image)
            app.sandbox.kit=self.service.sandbox.kit
            worker=EvolvedWorker(app.store,app.sandbox)
            frozen_optimizer=self.service.releases.get(optimizer_release)
            components=self.service.releases.components(optimizer_release)
            # Import the exact content-addressed release into the fresh episode
            # store, without activating it as the coding harness baseline.
            for ref in [*frozen_optimizer.components.values(), frozen_optimizer.build_attestation_digest]:
                if app.store.blob(self.store.read_blob(ref)) != ref:
                    raise RepoError("OPTIMIZER_IMPORT_MISMATCH")
            app.store.put("release", optimizer_release, frozen_optimizer, immutable=True)
            coding_base=app.releases.active()['digest']
            # The same starting coding harness is installed for BOTH arms.
            starting=problem.get('starting_components')
            if starting:
                if set(starting)&{'l1_optimizer','l2_optimizer'}:raise RepoError('META_STARTING_SCOPE')
                baseline=app.releases.candidate('fixture',starting,'Trusted frozen episode baseline',[],base=coding_base)
                coding_base=baseline.candidate_release_digest
            evidence=public.get('evidence',{})
            if not evidence.get('events') or not evidence.get('pattern'):raise RepoError('META_PUBLIC_EVIDENCE_REQUIRED')
            support=[]
            with app.store.tx() as conn:
                for index,event in enumerate(evidence['events']):
                    rid='episode-source-'+str(index)
                    seq=app.store.emit(conn,rid,event['type'],event['data'],'DEV')
                    pid='episode-problem-'+str(index)
                    app.store.put('problem',pid,{'id':pid,'run_id':rid,'sequence':seq,'symptom':evidence['pattern'].get('symptom','observed_failure')},conn)
                    support.append(pid)
            pattern_id='episode-pattern'
            app.store.put('pattern',pattern_id,{**evidence['pattern'],'id':pattern_id,'support':support})
            vault=HoldoutVault(app.store)
            dev=vault.register('episode-dev','dev',problem['dev_tasks'],synthetic=synthetic)
            secret=vault.register('episode-score','final',problem['score_tasks'],synthetic=synthetic)
            evaluator=SuiteEvaluator(app,vault,lambda task:CountingProvider(self.provider_factory(task,'coding'),ledger))
            engine=EvolutionEngine(app,provider,evaluator,optimizer_release=optimizer_release)
            selected=None
            try:
                async with asyncio.timeout(seconds):
                    l2=await worker.converse(components['l2_optimizer'],{'operation':'meta_search','problem':public},provider,run_id='meta-config')
                    l1=await worker.converse(components['l1_optimizer'],{'operation':'search_config','problem':public},provider,run_id='l1-config')
                    max_optimizers=min(max(int(l2.get('max_candidates',2)),1),3)
                    response=await provider.complete(
                        'You are a FROZEN optimizer instance, not the candidate builder. Given only the public failure evidence, propose up to limit L1 search configurations. Return JSON {optimizers:[{max_candidates:1..5,max_rounds:1..3,order:string}]}. No hidden task access, no grading or permission changes.',
                        [{'role':'user','content':json.dumps({'problem':public,'baseline_l1':l1,'meta_strategy':l2,'limit':max_optimizers},ensure_ascii=False)}],[])
                    options=json.loads(response.text)['optimizers']
                    if not isinstance(options,list) or not options:raise RepoError('EMPTY_META_PROPOSAL')
                    for opt in options[:max_optimizers]:
                        if not isinstance(opt,dict) or set(opt)-{'max_candidates','max_rounds','order'}:raise RepoError('META_CONFIG_SCOPE')
                        result=await engine.search(pattern_id,dev,
                            max_candidates=min(max(int(opt.get('max_candidates',l1.get('max_candidates',3))),1),5),
                            max_rounds=min(max(int(opt.get('max_rounds',l1.get('max_rounds',2))),1),3),
                            base=coding_base,strategy=opt.get('order',l1.get('order','evidence_first')))
                        attempts.append({'optimizer_config':opt,'search':result})
                        if result.get('selected'):
                            match=next(x for x in result['reports'] if x['candidate']==result['selected'])
                            rank=(match['report']['candidate_passed'],-match['report']['cost']['candidate'])
                            if selected is None or rank>selected[0]:selected=(rank,result['selected'])
                    if selected:
                        chosen=Candidate.model_validate(app.store.get('candidate',selected[1]))
                        # No feedback to the instance after this point. Scores remain private.
                        score=await evaluator.paired(chosen,secret,True,'META_SCORE')
                        status='SCORED'
                    else:status='NO_CANDIDATE'
            except (RepoError,ValueError,KeyError,TimeoutError) as exc:
                status=getattr(exc,'code',type(exc).__name__)
            finally:
                with contextlib.closing(app.store.connect()) as conn: runs=[r[0] for r in conn.execute('SELECT id FROM runs')]
                for rid in runs:app.sandbox.stop_run(rid)
            record={'id':uid('meta-episode-'),'problem_id':problem['id'],'optimizer_release':optimizer_release,'cohort':cohort,
                'coding_base_release_digest':coding_base,
                'l1_optimizer_component_digest':frozen_optimizer.components['l1_optimizer'],
                'l2_optimizer_component_digest':frozen_optimizer.components['l2_optimizer'],
                'optimizer_invocations':app.store.values('optimizer_invocation'),
                'status':status,'attempts':attempts,'score':score,'all_model_calls':ledger['calls'],'usage':ledger['usage'],
                'wall_seconds':time.monotonic()-start,'budget':{'model_calls':limit,'wall_seconds':seconds},
                'protocol':'nested-l2-propose-l1-search-l0-trials-v1','fresh_instance':True,'synthetic':ledger.get('synthetic',False)}
            # Preserve evidence before destroying mutable per-episode state.
            # The archive is trusted-private and is never a model-readable Run artifact.
            try:
                import io,zipfile
                from .maintenance import Maintenance
                backup=Path(td)/"audit"
                Maintenance(app.store).backup(backup)
                buffer=io.BytesIO()
                with zipfile.ZipFile(buffer,"w",zipfile.ZIP_DEFLATED) as archive:
                    for path in sorted(backup.rglob("*")):
                        if path.is_file():archive.write(path,path.relative_to(backup).as_posix())
                record["audit_archive_digest"]=self.store.blob(buffer.getvalue())
            except (RepoError,OSError) as exc:
                record["status"]="AUDIT_INCOMPLETE";record["audit_error"]=getattr(exc,"code",type(exc).__name__)
        self.store.put('meta_private' if hidden else 'meta_dev',record['id'],record,immutable=True)
        return record

    async def compare(self,candidate,key,hidden,gate,require_gain=False):
        cohort=self.vault.open(key,self.cap if hidden else None)
        if hidden:self.vault.claim(candidate.id,key,gate)
        pairs=[];synthetic=cohort.get("synthetic_integration",False);old_total=new_total=0;old_cost=new_cost=0;regression=False;valid=True
        for problem in cohort['tasks']:
            old=await self.episode(problem,candidate.base_release_digest,key,hidden,synthetic=cohort.get('synthetic_integration',False))
            new=await self.episode(problem,candidate.candidate_release_digest,key,hidden,synthetic=cohort.get('synthetic_integration',False))
            pairs.append({'old':old['id'],'new':new['id']});synthetic |= old['synthetic'] or new['synthetic']
            a=(old['score'] or {}).get('candidate_passed',0);b=(new['score'] or {}).get('candidate_passed',0)
            old_total+=a;new_total+=b;old_cost+=old['all_model_calls'];new_cost+=new['all_model_calls']
            regression |= b<a
            valid &= old['status'] in {'SCORED','NO_CANDIDATE'} and new['status'] in {'SCORED','NO_CANDIDATE'}
        gain=new_total>old_total or new_total==old_total>0 and new_cost<old_cost
        passed=bool(valid and new_total>0 and not regression and (gain or not require_gain) and new_cost<=max(old_cost,1)*2)
        report={'evidence_class':'INTEGRATION_ONLY' if synthetic else 'MODEL_META_EVALUATION','passed':passed,'protocol':'nested-l2-propose-l1-search-l0-trials-v1','cohort':key,'episodes':pairs,
            'baseline_score':old_total,'candidate_score':new_total,'cost':{'baseline':old_cost,'candidate':new_cost},'regression':regression}
        self.store.put('meta_report_private' if hidden else 'meta_report_dev',digest(report),report,immutable=True)
        if hidden:self.vault.notice(candidate.id,key,passed,'META_QUALITY' if not passed else 'PASSED')
        else:self.vault.mark_exposure(candidate.id,key)
        return report

    async def qualify(self,candidate_id,dev,promotion,final):
        candidate=Candidate.model_validate(self.store.get('candidate',candidate_id))
        if candidate.level!=2:raise RepoError('META_CANDIDATE_REQUIRED')
        if [self.store.get('cohort',x)['role'] for x in (dev,promotion,final)]!=['meta_dev','meta_promotion','meta_final']:raise RepoError('COHORT_ROLE_MISMATCH')
        releases=self.service.releases
        releases.governor.check(releases.components(candidate.candidate_release_digest),candidate.operation,candidate.growth_justification)
        releases.record_gate(candidate,'G0',{'passed':True,'scope':'meta ABI, structural hard limits'})
        dr=await self.compare(candidate,dev,False,'META_DEV')
        releases.record_gate(candidate,'G1',dr);releases.record_gate(candidate,'G2',dr)
        if not dr['passed']:return {'status':'REJECT','reason':'META_DEV'}
        self.store.put('candidate_freeze',candidate.id,{'release':candidate.candidate_release_digest,'cohorts':[dev,promotion,final],'protocol':'nested-v1'},immutable=True)
        pr=await self.compare(candidate,promotion,True,'META_PROMOTION',True);releases.record_gate(candidate,'G3',pr)
        if not pr['passed']:return {'status':'REJECT','reason':'META_PROMOTION'}
        evaluator=SuiteEvaluator(self.service,self.vault,lambda t:self.provider_factory(t,'coding'))
        sr=await evaluator.safety(candidate);releases.record_gate(candidate,'G4',sr)
        if not sr['passed']:return {'status':'REJECT','reason':'META_SAFETY'}
        fr=await self.compare(candidate,final,True,'META_FINAL');releases.record_gate(candidate,'G4b',fr)
        releases.record_gate(candidate,'META',{'passed':fr['passed'],'protocol':'nested-l2-propose-l1-search-l0-trials-v1','all_attempts_accounted':True,'fresh_instances':True})
        self.store.put('candidate_state',candidate.id,{'state':'AWAITING_APPROVAL' if fr['passed'] else 'REJECTED'})
        return {'status':'PASS' if fr['passed'] else 'REJECT','candidate':candidate.id,'meta_activation_requires_operator_enable':True}
