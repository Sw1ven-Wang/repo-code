"""Bounded DEV-only Memory v2.1 trials. Never consumes holdout eligibility."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
import tempfile
from evaluate_memory_v2 import provider,AuditProvider,write
from memory_task_trials import tasks,trial
from repo_code.memory.models import MemoryProfile
from repo_code.memory.encoders import LocalEncoder
from repo_code.memory.budget import ContextBudgetCoordinator
from repo_code.util import RepoError,canonical,digest

ROOT=Path(__file__).resolve().parents[1]


class BudgetAuditProvider(AuditProvider):
    def __init__(self,inner,output,calls,tokens):
        super().__init__(inner,output,calls);self.token_cap=tokens;self.reservations=[];self.guard=asyncio.Lock()
    async def complete(self,system,messages,tools,on_delta=None,*,json_mode=False):
        # UTF-8 serialized bytes + output + protocol slack is conservative for
        # these text-only requests. Actual usage is reconciled even on failure.
        options={'json_mode':True} if json_mode else {}
        request=self.build_request(system,messages,tools,**options)[0]
        budget=ContextBudgetCoordinator(self.inner,16000,4096,500,request_options=options).assert_fits(system,messages,tools)
        charge=len(canonical(request))+4096+1024
        async with self.guard:
            if self.token_cap is not None and sum(r['charged_tokens'] for r in self.reservations)+charge>self.token_cap:raise RepoError('EXPERIMENT_TOKEN_BUDGET')
            row={'purpose':'main' if tools else 'auxiliary','charged_tokens':charge,'usage_status':'RESERVED','budget':budget.__dict__}
            self.reservations.append(row)
        call_index=len(self.calls)
        try:
            reply=await super().complete(system,messages,tools,on_delta,**options)
            actual=reply.usage.get('prompt_tokens',reply.usage.get('input_tokens'))
            if isinstance(actual,int) and actual+4096+500>16000:
                violation={'actual_input':actual,'output_reserve':4096,'safety':500,'window':16000}
                self.calls[call_index]['actual_context_budget_violation']=violation;row['actual_context_budget_violation']=violation
                write(self.output/'calls.json',self.calls)
                return reply.model_copy(update={'stop_reason':'error'})
            return reply
        finally:
            usage=self.calls[call_index].get('usage',{}) if len(self.calls)>call_index else {}
            if len(self.calls)>call_index:row['purpose']=self.calls[call_index]['purpose']
            row['usage']=usage
            if usage:
                row['charged_tokens']=(usage.get('prompt_tokens',usage.get('input_tokens',0)) or 0)+(usage.get('completion_tokens',usage.get('output_tokens',0)) or 0)
                row['usage_status']='REPORTED'
            else:row['usage_status']='USAGE_UNKNOWN'
            write(self.output/'external-budget-ledger.json',{'status':'EXTERNAL_BUDGET_ENFORCED' if self.token_cap is not None else 'ACCOUNTING_ONLY_NO_CUMULATIVE_TOKEN_CAP','token_cap':self.token_cap,
                'call_cap':self.limit,'charged_tokens':sum(r['charged_tokens'] for r in self.reservations),'reservations':self.reservations,
                'monetary_hard_limit':False,'dollar_cost':None})


async def main_async(args):
    args.output.mkdir(parents=True,exist_ok=False)
    selected=[t for t in tasks() if t['id'] in args.tasks]
    if {t['id'] for t in selected}!=set(args.tasks):raise ValueError('Unknown DEV task')
    profile=MemoryProfile.model_validate(json.loads(args.profile.read_text(encoding='utf-8'))).model_copy(update={
        'max_tokens':16000,'reserve_tokens':4096,'safety_tokens':500,'auto_activate_observations':False,'auxiliary_calls':None if args.unlimited_calls else 8})
    image=json.loads((ROOT/'examples/sandbox/validation-config.json').read_text())['image']
    native=provider(args.output).inner;p=BudgetAuditProvider(native,args.output,None if args.unlimited_calls else args.call_cap,args.token_cap)
    source={f.relative_to(ROOT).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in (ROOT/'src').rglob('*.py')}
    write(args.output/'evaluation-lock.json',{'domain':'DEV_REPAIR','tasks':selected,'task_digest':digest(selected),'arms':args.arms,
        'profile':profile.model_dump(),'source':source,'source_digest':digest(source),'call_cap':args.call_cap,'token_cap':args.token_cap,
        'per_run':{'model_calls_including_aux':None if args.unlimited_calls else 40,'tool_calls':100,'wall_seconds':600,'window':16000,'output':4096,'safety':500},
        'call_limit_override':'USER_REQUEST_2026_11_01' if args.unlimited_calls else None,
        'holdout_eligibility_consumed':False,'arm_order':'PREDECLARED','stops_on_budget':True,'cross_task_learning':'FRESH_STATE_PER_RUN'})
    encoder=LocalEncoder(profile.encoder);results=[]
    try:
        for task in selected:
            for arm in args.arms:
                state=Path(tempfile.mkdtemp(prefix='m21-',dir=ROOT/'.venv'))
                result=await trial(args.output,task,arm,p,profile,encoder,image,state_root=state,preflight=True,model_call_limit=None if args.unlimited_calls else 40);results.append(result)
                write(args.output/'summary.json',[{k:v for k,v in r.items() if k not in {'report','memory_budget'}} for r in results])
                print(json.dumps({k:v for k,v in result.items() if k in {'task','arm','state','reason','verified','compactions'}},indent=2),flush=True)
                if result['reason'] in {'BUDGET_EXHAUSTED','EXPERIMENT_TOKEN_BUDGET','EXPERIMENT_TOTAL_CALL_LIMIT'}:return
    finally:encoder.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tasks',nargs='+',default=['cross-module-csv'])
    p.add_argument('--arms',nargs='+',choices=['A','D'],default=['D'])
    p.add_argument('--profile',type=Path,default=ROOT/'examples/memory/deepseek-experimental.json')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--call-cap',type=int,default=40);p.add_argument('--token-cap',type=int,default=500000)
    p.add_argument('--unlimited-calls',action='store_true',help='Explicit operator override; retain wall/tool/token/context limits')
    asyncio.run(main_async(p.parse_args()))


if __name__=='__main__':main()
