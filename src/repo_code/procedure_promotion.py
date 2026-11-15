"""Host-only Draft -> reviewed candidate -> existing Skill/Release gates.

No worker IPC, automatic evidence synthesis, or alternative publication authority.
Each external stage has a durable dispatch marker. An interrupted/unknown stage
cannot be silently reissued; completed stages can be resumed without recharging.
"""
from __future__ import annotations

import copy
import json
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from .util import RepoError, digest, execution_lock
from .providers import model_configuration, is_test_double
from .capabilities import CapabilityRouter
from .skill_validation import SkillValidator
from .memory.consolidation import Consolidator
from .memory.repository import MemoryRepository
from .memory.models import MemoryProfile, ClaimRecord
from .memory.curation import ProcedureContract
from .memory.experience import ExperienceConsolidation
from .capability_binding import BindingSlot, validate_template


class StepRule(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    tool: Literal['read', 'search', 'edit', 'patch', 'create', 'process.run', 'workspace.diff']
    arguments_match: dict = Field(min_length=1)

    @model_validator(mode='after')
    def executable_argument_subset(self):
        from .tool_models import SCHEMAS
        fields = SCHEMAS[self.tool][0].model_fields
        if not set(self.arguments_match) <= set(fields):
            raise RepoError('PROMOTION_STEP_ARGUMENT_UNKNOWN')
        if 'expected_version' in self.arguments_match:
            raise RepoError('PROMOTION_VOLATILE_STEP_BINDING')
        for name, value in self.arguments_match.items():
            if isinstance(value,dict) and set(value)=={'slot'} and isinstance(value['slot'],str):
                continue # Portable templates are type/coverage checked as a whole below.
            try:
                field = fields[name]
                annotation = Annotated[field.annotation, *field.metadata] if field.metadata else field.annotation
                TypeAdapter(annotation).validate_python(value, strict=True)
            except ValueError:
                raise RepoError('PROMOTION_STEP_ARGUMENT_TYPE') from None
        if self.tool == 'patch' and any(op.get('kind') != 'create' for op in self.arguments_match.get('operations', [])):
            # Current native receipt matching compares whole argument values;
            # Host-added per-file versions make literal edit-operation arrays unstable.
            raise RepoError('PROMOTION_VOLATILE_STEP_BINDING')
        if self.tool == 'patch':
            try:
                SCHEMAS[self.tool][0].model_validate(self.arguments_match)
            except ValueError:
                raise RepoError('PROMOTION_STEP_ARGUMENT_TYPE') from None
        return self


class PromotionPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(min_length=1, max_length=4000)
    procedure: ProcedureContract
    step_rules: list[StepRule] = Field(min_length=1, max_length=8)
    binding_slots: dict[str,BindingSlot] = Field(default_factory=dict,max_length=8)

    @model_validator(mode='after')
    def portable_contract(self):
        rules=[r.model_dump() for r in self.step_rules]
        if self.binding_slots:
            validate_template({k:v.model_dump() for k,v in self.binding_slots.items()},rules,self.procedure.steps)
        elif any(isinstance(v,dict) and 'slot' in v for r in rules for v in r['arguments_match'].values()):
            raise RepoError('CAPABILITY_BINDING_SLOTS_REQUIRED')
        return self


class SemanticReview(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    decision: Literal['APPROVED', 'REJECTED', 'INSUFFICIENT_EVIDENCE']
    rationale: str = Field(min_length=1, max_length=4000)
    quote_ids: list[str] = Field(min_length=1, max_length=12)
    limitations: list[str] = Field(min_length=1, max_length=12)


class ProcedurePromotion:
    """Embedding API; trusted suites/providers are injected by the Host.

    Approval is never manufactured here. Even successful quality gates leave a
    candidate awaiting the existing release_approval and activate() checks.
    """
    REVISION = 'procedure-promotion-v1.3-binding-review-context'

    def __init__(self, service, runner, release_evaluator=None):
        self.service, self.store = service, service.store
        self.owner = Consolidator(MemoryRepository(self.store), service.runtime,
                                  MemoryProfile(mode='lexical_only'))
        self.validator = SkillValidator(service, runner)
        self.evaluator = release_evaluator

    def _source(self, draft_id):
        draft = self.store.get('procedure_draft', draft_id)
        if not draft:
            raise RepoError('PROCEDURE_DRAFT_NOT_FOUND')
        with self.store.tx() as c:
            lesson, episode, claim = ExperienceConsolidation(self.owner)._source(draft['lesson_ref'], c)
        self.owner.repo.ensure_dev([draft_id, *lesson['source_ids']])
        if draft['status'] != 'LOCAL_PROCEDURE_DRAFT' or draft['runtime_eligible']:
            raise RepoError('PROMOTION_DRAFT_STATE')
        if lesson['category'] != 'PROCEDURE_DRAFT' or lesson['procedure_draft'] != draft['draft']:
            raise RepoError('PROMOTION_DRAFT_BINDING')
        if claim['validity'] in {'STALE', 'CONTRADICTED'} or claim.get('contradiction_refs') or lesson.get('conflicts'):
            raise RepoError('PROMOTION_CONFLICT_REVIEW_REQUIRED')
        if episode['failure_class'] in {'HOST', 'ENVIRONMENT', 'EVALUATION', 'CONTRACT'}:
            raise RepoError('PROMOTION_HOST_NOT_SKILL')
        return {'draft': draft, 'lesson': lesson, 'claim': claim}

    async def _stage(self, job, name, operation):
        key = digest([job, name])
        row = self.store.get('procedure_promotion_step', key)
        if row:
            if row['status'] == 'COMPLETE':
                return row['result']
            raise RepoError('PROMOTION_STAGE_RECONCILIATION_REQUIRED', name)
        self.store.put('procedure_promotion_step', key, {'job': job, 'stage': name, 'status': 'DISPATCHING'})
        try:
            result = await operation()
        except BaseException as exc:
            from .util import ProviderNotDispatched
            status = 'NOT_DISPATCHED' if isinstance(exc, ProviderNotDispatched) else 'UNKNOWN_OR_FAILED'
            self.store.put('procedure_promotion_step', key, {'job': job, 'stage': name, 'status': status,
                           'error': getattr(exc, 'code', type(exc).__name__)})
            raise
        self.store.put('procedure_promotion_step', key, {'job': job, 'stage': name, 'status': 'COMPLETE', 'result': result})
        return result

    def retry_undispatched(self, job, stage):
        """Only a proven pre-dispatch failure may be reset by the trusted Host."""
        key = digest([job, stage])
        with execution_lock(self.store.root / 'executor_locks' / (job[7:] + '.lock')):
            row = self.store.get('procedure_promotion_step', key)
            if not row or row['status'] != 'NOT_DISPATCHED':
                raise RepoError('PROMOTION_RETRY_DENIED')
            with self.store.tx() as c:
                self.store.put('procedure_promotion_recovery', digest(row), row, c, immutable=True)
                c.execute("DELETE FROM objects WHERE kind='procedure_promotion_step' AND id=?", (key,))

    def _finish(self, job, status, reason=None, **fields):
        result = {'job': job, 'status': status, 'reason': reason, 'published': status == 'PUBLISHED', **fields}
        self.store.put('procedure_promotion', job, result)
        return result

    async def _model(self, provider, instruction, payload, schema):
        response = await provider.complete(instruction, [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}], [])
        if response.tool_calls:
            raise RepoError('PROMOTION_REVIEW_TOOLS_DENIED')
        parsed = schema.model_validate_json(response.text)
        return {'value': parsed.model_dump(), 'usage': response.usage,
                'provider': model_configuration(provider), 'integration_only': is_test_double(provider)}

    async def _prepared_model(self, prepared, source, context):
        """Pinned Host input, explicitly NOT a fresh model-generation result.

        Reusing a saved procedure must not silently regenerate different action
        bindings. Independent review and all execution gates still run normally.
        """
        if (set(prepared)!={'plan','source_digest','public_support_digest','origin'}
                or not isinstance(prepared['origin'],str) or not prepared['origin'].strip()
                or prepared['source_digest']!=digest(source)
                or prepared['public_support_digest']!=digest(context['public_support_tasks'])):
            raise RepoError('PREPARED_PLAN_SOURCE_BINDING')
        plan=PromotionPlan.model_validate(prepared['plan']).model_dump()
        if not plan.get('binding_slots'):plan.pop('binding_slots',None)
        return {'value':plan,'usage':{'input_tokens':0,'output_tokens':0},
                'provider':{'adapter':'HostPreparedPlan','origin':prepared['origin']},
                'integration_only':False,'evidence_class':'HOST_INPUT_REQUIRES_INDEPENDENT_REVIEW','model_calls':0}

    def _materialize(self, job, source, plan, review):
        """Atomic new claim/lesson, never edits or activates the source Draft."""
        lid = 'promoted-' + job[7:31]
        original, original_claim = source['lesson'], source['claim']
        with self.store.tx() as c:
            old = self.store.get('promotion_materialization', job, c)
            if old:
                return old
            if c.execute('SELECT count(*) FROM memory_versions').fetchone()[0] >= self.owner.profile.max_items:
                raise RepoError('MEMORY_GROWTH_BUDGET')
            claim = ClaimRecord.model_validate({**original_claim, 'claim_id': 'promotion-claim-' + lid,
                'version': 1, 'kind': 'PROCEDURE', 'text': plan['title'], 'lifecycle': 'CANDIDATE',
                'epistemic_status': 'INFERRED', 'derived_from': sorted(set(original_claim['derived_from'] + [source['draft']['id'], job]))})
            if plan.get('binding_slots'):
                # Historical source versions remain in immutable provenance.
                # Current-task dependencies are supplied by signed runtime binding.
                # Semantic/environment preconditions are deliberately retained.
                claim=claim.model_copy(update={'file_dependencies':{}})
            cref = claim.claim_id + ':1'
            lesson = {**original, 'id': lid, 'category': 'PROCEDURE', 'text': plan['title'],
                'procedure': plan['procedure'], 'claim_ref': cref, 'status': 'CANDIDATE',
                'semantic_review': {'status': 'INDEPENDENT_MODEL_REVIEW_NOT_RUNTIME_QUALIFICATION'},
                'promotion_job': job, 'source_draft_ref': source['draft']['id'],
                'derived_from': claim.derived_from}
            if plan.get('binding_slots'):
                lesson['portability']={'kind':'RUN_BOUND_TEMPLATE','source_file_dependencies':original_claim['file_dependencies'],
                    'binding_slots':plan['binding_slots'],'step_rules':plan['step_rules'],'semantic_preconditions_preserved':True}
            for field in ('procedure_draft', 'runtime_eligible', 'semantic_id', 'evidence_revision', 'previous_lesson_ref'):
                lesson.pop(field, None)
            self.owner.repo.put('memory_versions', cref, claim.model_dump(), c)
            self.owner.repo.put('memory_control', 'claim:' + claim.claim_id, {'version': 1, 'lifecycle': 'CANDIDATE'}, c)
            self.store.put('lesson', lid, lesson, c, immutable=True)
            self.owner.repo.derive(lid, claim.derived_from, c)
            self.owner.repo.derive('memory-version:' + cref, [lid, *claim.derived_from], c)
            payload = {'lesson_id': lid, 'lesson_digest': digest(lesson), 'decision': 'APPROVED',
                       'promotion_job': job, 'plan_digest': digest(plan), 'review_digest': digest(review),
                       'evidence_class': 'INDEPENDENT_MODEL_SEMANTIC_REVIEW'}
            self.store.put('claim_semantic_review', lid, {'payload': payload, 'signature': self.store.sign(payload)}, c, immutable=True)
            value = {'lesson_id': lid, 'claim_ref': cref}
            self.store.put('promotion_materialization', job, value, c, immutable=True)
            return value

    async def run(self, draft_id, planner, reviewer, *, support_tasks, probes,
                  transfer_tasks=(), cohorts=None, efficiency_metric=None, base=None, session_id=None, prepared_plan=None):
        """One frozen attempt. Rejection is final; new experiments need new inputs.

        support_tasks/transfer_tasks/probes are trusted frozen evaluation inputs,
        never model-generated tests or passed to runtime provider construction.
        """
        if planner is reviewer:
            raise RepoError('PROMOTION_INDEPENDENT_REVIEW_REQUIRED')
        if efficiency_metric not in {None, 'model_attempts', 'tool_calls', 'input_tokens', 'output_tokens'}:
            raise RepoError('SKILL_EFFICIENCY_METRIC')
        support_tasks, probes = copy.deepcopy(support_tasks), copy.deepcopy(probes)
        transfer_tasks = copy.deepcopy(list(transfer_tasks))
        source = self._source(draft_id)
        if session_id and self.service.session_experiences().get(session_id)['scope']!=source['draft']['scope']:
            raise RepoError('SESSION_SCOPE_DENIED')
        base = base or self.service.releases.active()['digest']
        frozen = {'revision': self.REVISION, 'source': digest(source), 'base': base,
                  'support_tasks': support_tasks, 'probes': probes, 'transfer_tasks': list(transfer_tasks),
                  'cohorts': cohorts, 'efficiency_metric': efficiency_metric,
                  'planner': model_configuration(planner), 'reviewer': model_configuration(reviewer),
                  'environment': self.service.sandbox.environment_digest}
        if session_id:frozen['session_id']=session_id
        if prepared_plan is not None:
            prepared_plan=copy.deepcopy(prepared_plan)
            frozen['prepared_plan']=prepared_plan
            frozen['planner']={'adapter':'HostPreparedPlan'}
        job = digest(frozen)
        with execution_lock(self.store.root / 'executor_locks' / (job[7:] + '.lock')):
            self.store.put('procedure_promotion_input', job, frozen, immutable=True)
            with self.store.tx() as c:
                self.owner.repo.derive(job, [draft_id, *source['lesson']['source_ids']], c)
            previous = self.store.get('procedure_promotion', job)
            if previous and previous['status'] in {'REJECTED', 'PUBLISHED', 'SESSION_READY'}:
                return previous
            self._finish(job, 'RUNNING')
            context = {'draft': source['draft'], 'evidence': source['lesson']['quote_bindings'],
                       'historical_applicability':{'preconditions':source['claim']['preconditions'],'file_dependencies':source['claim']['file_dependencies']},
                       'allowed_conditions': ['task_family:' + source['draft']['task_family'], 'run_cancelled', 'source_changed'],
                       'plan_schema': PromotionPlan.model_json_schema(),
                       'public_support_tasks': [{k: t[k] for k in ('id', 'goal', 'files')} for t in support_tasks.values()]}
            from .tool_models import definitions
            # Describe the downstream binding protocol to both planning and review.
            # This is reference context only: _model still grants no executable tools.
            context['live_tool_definitions'] = definitions(['read', 'search', 'edit', 'patch', 'create', 'process.run', 'workspace.diff', 'skill.bind'])
            context['binding_execution_contract']={
                'preparation':'Current-file discovery, prerequisite reads and slot binding precede application.',
                'application':'Only the declared successful operation sequence after binding is application evidence.',
                'placement':'Put reads needed only to prepare bindings in necessary_checks; compile learned decisions into post-binding steps.',
                'verification':'Preserve semantic required checks, not incidental test-authoring tools or test-file structure from the historical trace. Bind current verification argv.',
                'slot_coverage':'Declare only slots actually used by action rules; cite prerequisite documents as binding evidence rather than unused action slots.',
                'matching':'Exact argument and order checks remain required; no retroactive application credit.'}
            context['existing_host_obligations']={
                'planning_policy':self.service.releases.components(base)['planning_policy'].get('instructions','See the existing public tool contracts and task permissions.'),
                'tool_contracts':'Current tool declarations already define reading, editing and execution syntax; these are not newly learned capabilities.'}
            # Public execution boundaries are needed to compile a reachable
            # workflow. Private checks, expected answers and probes stay outside.
            context['public_support_path_policy']={key:{name:task['contract'].get(name,[])
                for name in ('allowed_paths','forbidden_paths','protected_paths')}
                for key,task in support_tasks.items()}
            try:
                plan_report = await self._stage(job, 'PLAN', lambda: self._prepared_model(prepared_plan,source,context) if prepared_plan is not None else self._model(planner,
                    'Prepare a scoped, advisory Procedure candidate from the untrusted evidence. No tools. Return only JSON matching plan_schema. '
                    'Do not relearn tool syntax or infer validation success. Use only allowed_conditions as triggers/exclusions. '
                    'Keep scope/family implicit and unchanged. Extract an adaptable decision procedure; do not transplant the old patch, '
                    'source names, imports or regression literals into a different implementation. Preserve the observed causal mechanism and unknowns. '
                    'Produce a PORTABLE capability: binding_slots is required. Each slot declares kind path or argv and a semantic purpose. '
                    'Steps describe causal decisions, invariants, exclusions and checks; never embed support-task filenames, commands or implementation-specific structure. '
                    'There must be exactly one step_rule for each procedure step, in the same order; use the smallest sufficient workflow and group related invariants within its steps. '
                    'Binding preparation is a prerequisite, not a learned action step: put discovery and reads needed only to fill slots in necessary_checks, not in procedure.steps or step_rules. '
                    'Compile the reusable post-binding decision into its actual effect/check sequence. Do not require unchanged rereads solely to obtain a receipt; retain new inspections only when causally needed after adoption. '
                    'Existing Host workflow obligations are baseline, not new learning. Do not transcribe an incidental historical tool choice into a mandatory capability step. '
                    'Keep focused counterexamples and boundary coverage in necessary_checks; normally bind their current verification argv rather than require editing a particular existing test file. '
                    'Test authoring may use existing tests, new files or inline assertions. Require a specific test-file transformation only when that transformation itself is the learned causal capability. '
                    'Every declared slot must be used by an action rule. Prerequisite documents can be cited as binding evidence without separate unused path slots. '
                    'Respect public_support_path_policy: do not require editing protected files merely to produce an application receipt. '
                    'Each step_rule uses read/edit/workspace.diff with arguments_match={path:{slot:name}}, or process.run with {argv:{slot:name}}. '
                    'A new task resolves slots from its own observed implementation and test configuration using skill.bind; never resolve them during extraction. '
                    'Citation selectors such as function_definition/new_hunk/created_content are NOT tool arguments. '
                    'Do not bind transient expected_version values or literal patch edit-operation arrays; Host versions are added later. '
                    'Action bindings are only observability, not proof of semantic execution. Preserve unknown boundaries and value/equality exclusions. '
                    'No gold tests or answers are available; do not invent them.', context, PromotionPlan))
                plan = plan_report['value']
                if not plan.get('binding_slots') and prepared_plan is None and not is_test_double(planner):
                    raise RepoError('PROMOTION_PORTABLE_PLAN_REQUIRED')
                if plan.get('binding_slots'):
                    # Deterministic guard supplements, never replaces, independent
                    # semantic review. Reject known task paths in published prose.
                    prose=json.dumps({'title':plan['title'],'procedure':plan['procedure'],'slots':plan['binding_slots']})
                    from pathlib import PurePosixPath
                    names={n for t in context['public_support_tasks'] for p in t['files'] for n in (p,PurePosixPath(p).name) if len(n)>3}
                    if any(n in prose for n in names):raise RepoError('PROMOTION_TASK_LITERAL_IN_CAPABILITY')
                if len(plan['step_rules']) != len(plan['procedure']['steps']):
                    raise RepoError('CAPABILITY_STEP_BINDING')
                keys = set(plan['procedure']['triggers'] + plan['procedure']['exclusions'])
                if not keys <= set(context['allowed_conditions']) or not set(plan['procedure']['triggers']):
                    raise RepoError('PROMOTION_CONDITION_UNOBSERVABLE')
                if any(not x.strip() for values in plan['procedure'].values() if isinstance(values, list) for x in values):
                    raise RepoError('PROMOTION_EMPTY_STEP')
                review_report = await self._stage(job, 'SEMANTIC_REVIEW', lambda: self._model(reviewer,
                    'Independently review the untrusted Draft, exact evidence quotes and proposed Procedure. No tools. '
                    'Return JSON {decision:"APPROVED|REJECTED|INSUFFICIENT_EVIDENCE",rationale:str,quote_ids:[exact IDs],limitations:[str]}. '
                    'APPROVED means only suitable for independent experiments, never proven effective or publishable. '
                    'Reject generic existing tool rules, unjustified causal certainty, unsafe scope and misleading action bindings. '
                    'Check binding_execution_contract: reject executable workflows inflated with prerequisite slot-discovery reads instead of learned post-binding decisions. '
                    'Reject incidental test-authoring constraints or baseline workflow obligations presented as a new capability; preserve the semantic verification requirements independently of file-authoring strategy. '
                    'Check source diagnosis AND implementation evidence, keep uncertainties; do not approve merely because a plan exists. '
                    'For portable plans reject task-specific filenames, literal test commands, fixed operator counts, support-answer leakage or unverified implementation assumptions in prose. '
                    'Review the generalization explicitly: historical file versions become provenance rather than runtime file requirements; current files must be read and rebound. Reject if correctness depends on those exact historical files. Semantic/environment preconditions remain required. '
                    'Slot purposes must explain how a new task discovers the relevant implementation and verification, not disguise a fixed script.',
                    {'source': context, 'plan': plan}, SemanticReview))
                review = review_report['value']
                bindings = {b['quote_id']: b for b in context['evidence']}
                if len(set(review['quote_ids'])) != len(review['quote_ids']) or not set(review['quote_ids']) <= set(bindings):
                    raise RepoError('PROMOTION_REVIEW_CITATION')
                if review['decision'] != 'APPROVED':
                    return self._finish(job, 'REJECTED', 'SEMANTIC_' + review['decision'], review=review)
                if not {'supports', 'implements'} <= {bindings[q]['relation'] for q in review['quote_ids']}:
                    raise RepoError('PROMOTION_REVIEW_CAUSAL_COVERAGE')
                if digest(self._source(draft_id)) != frozen['source']:
                    raise RepoError('PROMOTION_SOURCE_CHANGED')
                materialized = self._materialize(job, source, plan, review_report)
                experience = ExperienceConsolidation(self.owner).consolidate(materialized['lesson_id'])
                # Register actual frozen probes; strings in a Draft never count as executable evidence.
                if (len({p['probe_id'] for p in probes}) != len(probes)
                        or len({(p['task_id'], p['check_id']) for p in probes}) != len(probes)):
                    raise RepoError('PROMOTION_DUPLICATE_PROBE')
                for spec in probes:
                    if spec['task_id'] not in support_tasks:
                        raise RepoError('PROBE_TASK_BINDING')
                    self.validator.register(spec, support_tasks[spec['task_id']])
                ref, contract = CapabilityRouter(self.owner).contract(experience['entity_id'], experience['revision'],
                    step_rules=plan['step_rules'], probe_ids=[p['probe_id'] for p in probes],binding_slots=plan.get('binding_slots'))
                candidate_binding = self.store.get('promotion_candidate', job)
                if not candidate_binding:
                    cfg = self.service.releases.components(base)['skills_index']
                    entry = {'experience_id': experience['entity_id'], 'revision': experience['revision'],
                             'revision_digest': experience['revision_digest'], 'scope': contract['scope'],
                             'task_family': source['draft']['task_family'], 'status': 'ACTIVE',
                             'phases': ['PLAN', 'EDIT', 'DEBUG', 'VERIFY', 'RESUME'],
                             'step_rules': plan['step_rules'], 'required_probe_ids': contract['required_probe_ids']}
                    if plan.get('binding_slots'):entry['binding_slots']=plan['binding_slots']
                    # Recover a build completed immediately before a process crash.
                    from .models import Candidate
                    built = [v for v in self.store.values('candidate') if v['pattern_id'] == job]
                    if len(built) > 1:
                        raise RepoError('PROMOTION_AMBIGUOUS_BUILD')
                    if built:
                        candidate = Candidate.model_validate(built[0])
                        if candidate.base_release_digest != base or ref not in candidate.source_evidence:
                            raise RepoError('PROMOTION_BUILD_BINDING')
                    else:
                        candidate = self.service.releases.candidate(job, {'skills_index': {**cfg, 'skills': cfg.get('skills', []) + [entry]}},
                            plan['title'], [ref, job], operation='ADD', base=base,
                            justification='One evidence-bound advisory Skill candidate for independent validation: ' + review['rationale'])
                    candidate_binding = {'candidate': candidate.id, 'contract_ref': ref}
                    self.store.put('promotion_candidate', job, candidate_binding, immutable=True)
                cid = candidate_binding['candidate']
                self._finish(job, 'CANDIDATE', **candidate_binding)
                s1 = self.validator.s1(ref, cid, support_tasks)
                if not s1['passed']:
                    return self._finish(job, 'REJECTED', 'S1_REJECTED', s1=s1, **candidate_binding)
                s2 = await self._stage(job, 'S2', lambda: self.validator.s2(ref, cid, support_tasks, efficiency_metric=efficiency_metric))
                if any(t['state'] == 'INFRASTRUCTURE_ERROR' for t in s2.get('trials', [])):
                    return self._finish(job, 'BLOCKED', 'S2_INFRASTRUCTURE_ERROR', s2=s2, **candidate_binding)
                if not s2['passed']:
                    return self._finish(job, 'REJECTED', 'S2_REJECTED', s2=s2, **candidate_binding)
                if session_id:
                    if plan_report['integration_only'] or review_report['integration_only']:
                        return self._finish(job,'REJECTED','SESSION_REAL_MODEL_EVIDENCE_REQUIRED',**candidate_binding)
                    grant=self.service.session_experiences().admit(session_id,ref,cid)
                    return self._finish(job,'SESSION_READY','LOCAL_REUSE_NOT_PUBLICATION',session_id=session_id,grant_id=grant,**candidate_binding)
                if not transfer_tasks:
                    return self._finish(job, 'BLOCKED', 'INDEPENDENT_TRANSFER_SUITE_REQUIRED', **candidate_binding)
                s3 = await self._stage(job, 'S3', lambda: self.validator.s3(ref, cid, list(transfer_tasks)))
                if not s3.get('execution_valid', True):
                    return self._finish(job, 'BLOCKED', 'S3_INVALID_EXECUTION', s3=s3, **candidate_binding)
                if not s3.get('cost_comparison', {}).get('cost_settled', True):
                    return self._finish(job, 'BLOCKED', 'S3_COST_UNSETTLED', s3=s3, **candidate_binding)
                if (not s3['passed'] or s3['evidence_class'] != 'MODEL_TASK_EVALUATION'
                        or plan_report['integration_only'] or review_report['integration_only']):
                    return self._finish(job, 'REJECTED', 'S3_REJECTED_OR_INTEGRATION_ONLY', s3=s3, **candidate_binding)
                if not self.evaluator or not cohorts:
                    return self._finish(job, 'BLOCKED', 'RELEASE_EVALUATOR_AND_COHORTS_REQUIRED', **candidate_binding)
                gates = await self._stage(job, 'RELEASE_GATES', lambda: self.evaluator.qualify(cid, *cohorts))
                if gates['status'] != 'PASS':
                    return self._finish(job, 'REJECTED', 'RELEASE_GATES_REJECTED', gates=gates, **candidate_binding)
                # Do not create release_approval. activate() remains the final authority.
                if not self.store.get('release_approval', cid):
                    return self._finish(job, 'AWAITING_APPROVAL', 'RELEASE_APPROVAL_REQUIRED', **candidate_binding)
                active = self.service.releases.activate(cid)
                return self._finish(job, 'PUBLISHED', active=active, **candidate_binding)
            except Exception as exc:
                return self._finish(job, 'BLOCKED', getattr(exc, 'code', type(exc).__name__), error_type=type(exc).__name__)
