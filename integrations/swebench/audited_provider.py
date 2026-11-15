"""Reusable recorded-provider budget for new evaluation runs (no auth copying).

Wrap the explicitly configured provider. Native requests/responses and the
conservative UTF-8 admission rule are preserved; every auxiliary call shares
the same ledger. Never infer missing Usage or silently retry a failed run.
"""
import asyncio
import json
import time
import uuid
from pathlib import Path
from repo_code.memory.budget import ContextBudgetCoordinator
from repo_code.util import ProviderNotDispatched, RepoError, atomic_write, canonical, digest


class AuditedProvider:
    def __init__(self, inner, destination, *, context=16000, safety=500,
                 total_tokens=400000, calls=120, wall_seconds=1800):
        self.inner=inner;self.dest=Path(destination);self.records=[];self.halt=None
        self.context,self.safety=context,safety
        for value in (total_tokens,calls):
            if value is not None and (type(value) is not int or value<=0):raise RepoError('INVALID_EVALUATION_BUDGET')
        self.total_tokens,self.calls,self.wall_seconds=total_tokens,calls,wall_seconds
        self.started=time.monotonic()
        for field in ('model','base_url','reasoning_effort','max_tokens','temperature','thinking','billing','wire_protocol','supports_json_mode'):
            if hasattr(inner,field):setattr(self,field,getattr(inner,field))
        if not getattr(self,'max_tokens',None):raise RepoError('OUTPUT_LIMIT_REQUIRED')
        if (self.dest/'api_usage.json').exists():raise RepoError('FRESH_EVALUATION_DESTINATION_REQUIRED')

    def build_request(self,*args,**kwargs):return self.inner.build_request(*args,**kwargs)
    def assert_state_safe(self,state):
        if hasattr(self.inner,'assert_state_safe'):self.inner.assert_state_safe(state)
    def assert_workspace_safe(self,workspace):
        if hasattr(self.inner,'assert_workspace_safe'):self.inner.assert_workspace_safe(workspace)

    def budget_status(self):
        known=sum(r.get('settled_tokens',0) for r in self.records)
        charged=sum(r.get('settled_tokens',r['reservation']) for r in self.records)
        return {'limit':self.total_tokens,'known_used':known,'charged_including_unknown_reserves':charged,
                'remaining':max(0,self.total_tokens-charged) if self.total_tokens is not None else None,
                'calls_used':len(self.records),'call_limit':self.calls,
                'dispatched_calls':sum(r['status']!='NOT_DISPATCHED' for r in self.records),
                'not_dispatched_attempts':sum(r['status']=='NOT_DISPATCHED' for r in self.records),
                'unknown_usage_calls':sum('settled_tokens' not in r for r in self.records),
                'halted':bool(self.halt) or any(r['status']=='FAILED' for r in self.records),
                'successful_responses':sum(r['status']=='COMPLETED' for r in self.records),
                'cost_settled':all('settled_tokens' in r for r in self.records),
                'actual_cost':None,
                'reservation_method':'UTF8_BYTE_UPPER_BOUND',
                'note':'Every main and auxiliary request shares this limit. Next request needs input upper bound plus output and protocol reserves; it can stop before the displayed remainder is zero.'}

    def save(self,path,value):
        path=self.dest/path;path.parent.mkdir(parents=True,exist_ok=True)
        atomic_write(path,canonical(value))

    async def complete(self,system,messages,tools,on_delta=None,*,json_mode=False):
        # A caller resetting halt cannot erase a dispatched attempt with unknown
        # usage. A later response is not proof that the previous request was free.
        if any(r['status']=='FAILED' and 'settled_tokens' not in r for r in self.records):
            raise ProviderNotDispatched('USAGE_UNSETTLED','Prior dispatched attempt requires reconciliation')
        if any(r['status']=='FAILED' for r in self.records):
            raise ProviderNotDispatched('RETRY_PROOF_REQUIRED','Usage alone does not prove a dispatched request was unprocessed')
        if self.halt:raise ProviderNotDispatched('PROVIDER_HALTED',self.halt)
        options={'json_mode':True} if json_mode else {}
        coordinator=ContextBudgetCoordinator(self,self.context,self.max_tokens,self.safety,request_options=options)
        try:
            estimate=coordinator.assert_fits(system,messages,tools)
            request=self.build_request(system,messages,tools,**options)[0]
        except (RepoError,ValueError,TypeError) as exc:
            raise ProviderNotDispatched(getattr(exc,'code',type(exc).__name__),'Local context admission failed') from exc
        reservation=len(json.dumps(request,ensure_ascii=False).encode('utf-8'))+self.max_tokens+1024
        status=self.budget_status()
        cause=('CALL_LIMIT' if self.calls is not None and len(self.records)>=self.calls else 'TOKEN_RESERVATION_UNAVAILABLE' if status['remaining'] is not None and reservation>status['remaining']
               else 'WALL_LIMIT' if time.monotonic()-self.started>=self.wall_seconds else None)
        if cause:
            exc=ProviderNotDispatched('BUDGET_EXHAUSTED',cause)
            exc.provider_details.update(status,cause=cause,next_reservation=reservation)
            raise exc
        n=len(self.records)+1
        record={'call':n,'purpose':'auxiliary' if json_mode else 'main','status':'DISPATCHED',
                'attempt_id':uuid.uuid4().hex,'dispatched':True,
                'retry_of':self.records[-1].get('attempt_id') if self.records and self.records[-1]['status']=='NOT_DISPATCHED' else None,
                'usage_state':'UNKNOWN','retryable':False,
                'reservation':reservation,'started':time.time(),'request_digest':digest(request),'estimate':estimate.estimated_input_total}
        self.records.append(record);self.save(Path('requests')/f'{n:04}.json',request);self.save(Path('api_usage.json'),self.records)
        deadline = asyncio.timeout(max(.001,self.wall_seconds-(time.monotonic()-self.started)))
        try:
            try:
                async with deadline:
                    reply = await self.inner.complete(system,messages,tools,on_delta,**options)
            except TimeoutError as exc:
                if deadline.expired():
                    error = RepoError('BUDGET_EXHAUSTED','wall_seconds')
                    active = time.monotonic()-self.started
                    error.budget_stop = {'dimension':'wall_seconds',
                                         'stage':'auxiliary_wait' if json_mode else 'model_wait',
                                         'dispatch_status':'DISPATCHED_OR_UNKNOWN','active_seconds':active,
                                         'remaining_seconds':max(0,self.wall_seconds-active)}
                    raise error from exc
                raise
            record.update(status='COMPLETED',usage=reply.usage)
            self.save(Path('responses')/f'{n:04}.json',reply.model_dump(mode='json'))
            if not all(type(reply.usage.get(k)) is int and reply.usage[k]>=0 for k in ('input_tokens','output_tokens')):
                raise RepoError('USAGE_UNKNOWN')
            total=reply.usage['input_tokens']+reply.usage['output_tokens'];record['settled_tokens']=total
            record['usage_state']='KNOWN'
            record['context_audit']=coordinator.audit_usage(reply.usage,estimate)
            if total>reservation or record['context_audit']['hard_violation']:raise RepoError('ACTUAL_BUDGET_VIOLATION')
            return reply
        except BaseException as exc:
            record.update(status='FAILED',error=getattr(exc,'code',type(exc).__name__))
            if hasattr(exc,'budget_stop'):
                record['budget_stop'] = exc.budget_stop
            if isinstance(exc,ProviderNotDispatched):
                record.update(status='NOT_DISPATCHED',settled_tokens=0,usage={},dispatch_proof=exc.provider_details)
                record.update(dispatched=False,usage_state='PROVEN_NOT_DISPATCHED',retryable=True)
            usage=getattr(exc,'usage',None)
            if usage:
                record['usage']=usage
                if all(type(usage.get(k)) is int and usage[k]>=0 for k in ('input_tokens','output_tokens')):
                    record['settled_tokens']=usage['input_tokens']+usage['output_tokens']
                    record['usage_state']='KNOWN'
            self.halt=record['error'];raise
        finally:
            record['finished']=time.time();self.save(Path('api_usage.json'),self.records)
