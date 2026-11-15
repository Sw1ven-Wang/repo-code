"""Host-owned final native request accounting, shared by every model purpose."""
from dataclasses import dataclass,asdict
import json
import math
from ..context import estimate_tokens,input_token_scale
from ..util import RepoError,digest,canonical


@dataclass(frozen=True)
class ContextBudgetBreakdown:
    provider_profile_digest:str
    system_and_contract:int
    tool_schemas:int
    native_protocol_fields:int
    working_state:int
    recent_complete_groups:int
    compaction_projection:int
    retrieved_memory:int
    expanded_artifacts:int
    other_guidance:int
    estimated_input_total:int
    reserved_output:int
    safety_margin:int
    max_effective_window:int
    estimate_scale:float=1.0
    raw_estimated_input_total:int=0

    def within_limit(self):
        return self.estimated_input_total+self.reserved_output+self.safety_margin<=self.max_effective_window


class ContextBudgetCoordinator:
    def __init__(self,provider,window,reserve,safety,profile_digest=None,request_options=None):
        self.provider,self.window,self.reserve,self.safety=provider,window,reserve,safety
        self.scale=input_token_scale(provider)
        self.request_options=request_options or {}
        self.profile_digest=profile_digest or digest({'adapter':type(provider).__name__,'model':getattr(provider,'model',None)})

    def serialized(self,system,messages,tools):
        if hasattr(self.provider,'build_request'):return self.provider.build_request(system,messages,tools,**self.request_options)[0]
        return {'system':system,'messages':messages,'tools':tools}

    def estimate(self,system,messages,tools):return math.ceil(estimate_tokens(self.serialized(system,messages,tools))*self.scale)

    def measure(self,system,messages,tools,guidance=''):
        """Marginal costs sum exactly to the same native serialized total.

        These are estimates, not a provider tokenizer or a monetary hard limit.
        Protocol overhead stays in native_protocol_fields; no opaque state is
        removed from the actual request to obtain a smaller budget figure.
        """
        context=None
        if messages:
            try:context=json.loads(messages[0].get('content',''))
            except (ValueError,TypeError):pass
        if not isinstance(context,dict) or context.get('type')!='memory_context_data':context=None
        fields={};base={k:v for k,v in (context or {}).items() if k not in {'working_state','summary','retrieved_evidence'}}
        def prefix():return [{'role':'user','content':canonical(base).decode()}] if context else []
        count=self.estimate
        total=count('',prefix(),[]);fields['native_protocol_fields']=total
        clean_system=system[:-len(guidance)] if guidance and system.endswith(guidance) else system
        next_total=count(clean_system,prefix(),[]);fields['system_and_contract']=next_total-total;total=next_total
        next_total=count(system,prefix(),[]);fields['other_guidance']=next_total-total;total=next_total
        next_total=count(system,prefix(),tools);fields['tool_schemas']=next_total-total;total=next_total
        for key,label in [('working_state','working_state'),('summary','compaction_projection'),('retrieved_evidence','retrieved_memory')]:
            if context:base[key]=context.get(key)
            next_total=count(system,prefix(),tools);fields[label]=next_total-total;total=next_total
        request=self.serialized(system,messages,tools);raw=estimate_tokens(request);final=math.ceil(raw*self.scale)
        fields['recent_complete_groups']=final-total;fields['expanded_artifacts']=0
        return ContextBudgetBreakdown(self.profile_digest,**fields,estimated_input_total=final,
            reserved_output=self.reserve,safety_margin=self.safety,max_effective_window=self.window,estimate_scale=self.scale,raw_estimated_input_total=raw)

    def assert_fits(self,system,messages,tools,guidance=''):
        result=self.measure(system,messages,tools,guidance)
        if not result.within_limit():raise RepoError('CONTEXT_BUDGET_EXCEEDED','required state, summary and last complete groups exceed budget: '+canonical(asdict(result)).decode())
        return result

    def audit_usage(self,usage,breakdown=None):
        """Audit completed usage separately from conservative admission reserves.

        Safety/output reserves guide packing before dispatch. They are not
        generated tokens and must not turn a valid returned reply into failure.
        A real completed-window or output-limit violation remains a hard error.
        Feedback can only tighten later estimates within this provider/Run.
        """
        inputs=usage.get('prompt_tokens',usage.get('input_tokens'))
        outputs=usage.get('completion_tokens',usage.get('output_tokens'))
        if any(isinstance(v,bool) or not isinstance(v,int) or v<0 for v in (inputs,outputs)):
            return {'usage_known':False,'status':'USAGE_UNKNOWN','hard_violation':False}
        status=('HARD_WINDOW_EXCEEDED' if inputs+outputs>self.window else
                'OUTPUT_LIMIT_EXCEEDED' if outputs>self.reserve else
                'RESERVE_SHORTFALL' if inputs+self.reserve>self.window else
                'SAFETY_MARGIN_USED' if inputs+self.reserve+self.safety>self.window else 'WITHIN_BUDGET')
        audit={'usage_known':True,'status':status,'actual_input':inputs,'actual_output':outputs,
               'actual_total':inputs+outputs,'window':self.window,'output_reserve':self.reserve,
               'safety_margin':self.safety,'allocation_excess':max(0,inputs+self.reserve+self.safety-self.window),
               'hard_violation':status in {'HARD_WINDOW_EXCEEDED','OUTPUT_LIMIT_EXCEEDED'}}
        if breakdown and breakdown.raw_estimated_input_total>0:
            audit['estimated_input']=breakdown.estimated_input_total
            if inputs>breakdown.estimated_input_total:
                observed=max(input_token_scale(self.provider),(inputs+64)/breakdown.raw_estimated_input_total)
                self.provider.observed_input_scale=observed
                audit['next_estimate_scale']=observed
        return audit

    @classmethod
    def auxiliary(cls,provider,profile,json_mode=False):
        from ..providers import json_output_options
        reserve=max(profile.auxiliary_output_tokens,getattr(provider,'max_tokens',None) or 4096)
        return cls(provider,profile.max_tokens,reserve,profile.safety_tokens,request_options=json_output_options(provider) if json_mode else None)
