# Native Procedure Draft promotion

`RepoCode.procedure_promotion(runner, release_evaluator=None)` creates a trusted
Host coordinator. It is not a model tool and does not enable Memory v2, learning,
or automatic activation by default. Episode capture and Evidence Selector are
unchanged.

## Input and authority

Call `await coordinator.run(draft_id, planner, reviewer, support_tasks=...,
probes=..., transfer_tasks=..., cohorts=..., efficiency_metric=None, base=None)`.

- `planner` and `reviewer` must be separate provider instances/conversations.
  The planner proposes a typed `PromotionPlan`; the reviewer independently
  accepts, rejects or requests more evidence, citing exact saved quote IDs.
- The Host supplies frozen executable tasks and ProbeSpecs; private contracts
  and transfer/holdout inputs are not included in model review requests. Only
  public support input and previously admitted DEV evidence are supplied.
- Natural-language applicability is not automatically compiled into arbitrary
  facts. Conditions must be existing Host facts: the source task family,
  cancellation and source-change state. Unexpressible finer applicability stays
  in procedure text and unknown boundaries, subject to independent review.
- Step bindings use stable subsets of actual Broker argument schemas, including
  field types and constraints. Evidence selector names are not tool arguments.
  Expected file versions and literal patch edit arrays are rejected because the
  existing receipt matcher compares complete argument values after Host version
  insertion. These bindings cannot certify the natural-language reasoning step.
- Source scope and family remain unchanged. A new CANDIDATE claim/lesson is
  derived from the immutable Draft and evidence; the Draft is never overwritten.
- Review is permission to experiment, not proof of efficacy or Runtime approval.
  Generic existing tool rules and unjustified causal claims must be rejected by
  semantic review; its limitations are retained with the review response.

## Stages

1. Validate DEV lineage, current source, conflicts and source failure class.
2. Persist PLAN dispatch and result; independently persist SEMANTIC_REVIEW.
3. Verify quote identity and diagnosis/implementation coverage. Atomically create
   a new inferred Procedure claim and lesson plus signed review binding.
4. Use existing ExperienceConsolidation and CapabilityRouter to build a canonical
   candidate contract, with typed tool/argument bindings and registered probes.
5. Build one ordinary skills_index Release candidate through Releases.candidate.
6. Call existing SkillValidator.s1/s2/s3. S2 executes positive/negative/boundary
   probes in isolated paired tasks and requires observed application. S3 requires
   the existing independent task coverage, transfer, no regressions/misfires,
   settled costs and measured real-model benefit. Scripted results are integration
   evidence and cannot qualify publication.
7. Call the supplied SuiteEvaluator.qualify for G0/G1/G2/G3/G4/G4b. No gate report
   or success evidence is manufactured by this coordinator.
8. If native quality passes, await an existing Release approval. The coordinator
   never calls Releases.approve. Actual activation still calls Releases.activate,
   preserving signature, gate, approval, environment and active-base CAS checks.

Candidate releases are executable only under the existing trusted isolated-trial
binding before publication. This API does not widen an experience's SINGLE_TASK
production applicability or create an alternative production routing authority.
Broader scope needs separately reviewed evidence/versioning; a successful local
experiment alone does not grant global applicability.

## Persistence and recovery

| Storage kind | Purpose |
|---|---|
| procedure_promotion_input | Immutable source/config/model/environment/task input snapshot |
| procedure_promotion | Current decision: RUNNING/CANDIDATE/BLOCKED/REJECTED/AWAITING_APPROVAL/PUBLISHED |
| procedure_promotion_step | Durable dispatch marker, result, usage and failure state |
| procedure_promotion_recovery | Audit of proven pre-dispatch reset |
| promotion_materialization | Atomic new claim/lesson reference |
| promotion_candidate | Stable existing Release candidate/contract binding |
| claim_semantic_review | Signed immutable binding to new lesson and independent review |

Identity is content-derived from input, source, providers, base Release, task suite
and environment. A changed frozen input starts a distinct attempt; do not use this
to retry outcomes or claim repeated tasks are independent evidence.

Completed external stages are reused after restart. A DISPATCHING/unknown/failed
stage is not automatically replayed. Only `ProviderNotDispatched`, a trusted
pre-dispatch proof, permits `retry_undispatched(job, stage)`; that operation keeps
an audit record. Model timeouts, process crashes and partial suite execution need
external reconciliation or a separately declared new experiment, never silent
recharging/reuse of hidden sets. Interrupted Release builds are recovered by
their exact promotion identity instead of generating duplicate candidates.

Missing evaluation suites or operator approval remain BLOCKED/AWAITING_APPROVAL;
they are not passing gates. Rejections retain candidate, source and all actual
costs. Nothing here alters an Agent task's terminal verdict.

## Offline verification

`tests/test_procedure_promotion.py` covers actual S1/S2 integration using explicitly
scripted fixtures, source/condition/citation/probe rejection, idempotence, source
revocation, unknown-dispatch blocking, proven-undispatched recovery, orphan-build
recovery, S3 integration-only refusal and late-stage dispatch. Late-stage tests
use labeled control-flow mocks; they do not assert real model quality or generate
valid publication gates. Existing SkillValidator, G4 and Release tests remain
the authorities for actual gate behavior.
# Application evidence: complete diff coverage

The receipt retains exact argument matching. A narrowly scoped alternative proof
exists only for `workspace.diff(path=None)` covering a required file-specific
`workspace.diff(path=...)`. Every other declared argument still matches exactly.
The Host recomputes the visible diff using the run's public baseline and current
workspace. It requires the inspection source digest, control/writer epochs and
environment to agree; offset zero, explicit non-truncation and no next page; an
exact match of returned text and file metadata; and a complete nonempty textual
diff for the requested file. Binary/metadata-only, missing-file, stale, altered,
partial, and hidden artifact-tail evidence cannot satisfy this proof.

The immutable application receipt records source/baseline digests, file versions,
coverage paths and visible/target diff digests next to the operation evidence.
Later writes invalidate this equivalence until a fresh complete inspection.
This proves observed coverage, not semantic understanding, test success or Skill
benefit. Required probes, S1/S2/S3, Release gates, approval and activation remain
independent and unchanged.

