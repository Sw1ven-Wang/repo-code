# Cross-task Draft promotion integration

A finished local review or registered check saves a signed stage candidate in
the same Host transaction as the verdict. Candidates preserve the checked
snapshot, event boundary and evidence references. The Host batches the
candidates later, for example between tasks. The Curator reads DEV operation
evidence from each stage. Registered grader reports stay on the grading side.
Verified stage curation uses the collection's own execution traces and skips
historical seed lessons configured for legacy replay.

```python
flow = app.learning_pipeline(isolated_trial_runner)
batch = await flow.curate_stages(collection_id, curator_factory, max_stages=4)
result = await flow.advance(
    collection_id,
    planner_factory,       # draft_id -> real compiler provider
    reviewer_factory,      # draft_id -> distinct semantic review provider
    validation_for,        # Draft -> {support_tasks, probes}, trusted Host only
    base=frozen_operator_experiment_release,
)
```

The Curator factory receives the stage candidate ID. The batch records its
results and resumes without redispatching completed Curator calls. By default
promotion selects Drafts created from this collection's processed stages.
Explicit Draft IDs still support review of saved legacy Drafts.

The compiler and reviewer factories receive only the Draft identifier. The
native promotion implementation constructs their evidence views. Validation
oracles belong to the trusted callback and never become student task inputs.
The embedding must audit provider usage and stop on an unknown request outcome.

`advance` checks the open collection, Draft scope, source lineage and frozen
validation plan before model dispatch. It delegates to the existing
ProcedurePromotion implementation: portable binding compilation, independent
semantic review, executable probes, S1, isolated real-model paired S2, and
signed session admission. Its output records the native status; a rejection
is not converted into success. `SESSION_READY` means conditional advisory
reuse within that collection and Host environment, not a permanent Release.

Later tasks use `new_task(..., session_id=collection_id, task_family=family)`.
The native router still checks scope, applicability, exclusions, source state,
revocation and environment. Portable guidance requires `skill.bind`; successful
matching operations are recorded in application receipts. A receipt proves
observed execution only. Final correctness requires the independent task
grader, and performance gains require a matched outcome/cost analysis.

Full guidance is offered once. Until evidence binding succeeds, later requests
receive at most two compact pending handles, restricted to the current native
route's applicable contracts. Handles preserve the original offer identifier
and slot purposes; they do not manufacture file observations or execute tools.
Revocation, a changed control epoch and failed applicability suppress them.
After `skill.bind`, the ordinary tool result carries the bound steps once so
the stateless model can continue from its history. Pending handles disappear
at binding; actual application still requires the ordered successful operations
and retains the existing independent probe/Release checks.

The binding tool describes path slots as current relative file paths and argv
slots as JSON argument arrays. Rejected bindings name the offending slot and
the required representation. An invalid argv is a known input rejection, not
an uncertain external effect or a reason to enter reconciliation. Validation
does not split shell strings, manufacture observations or relax slot coverage.
Compiler context includes only public path permissions, and explicitly requires
one action rule per grouped procedure step. Private grader definitions and
expected answers remain excluded.

Runtime discovery is separate from the old knowledge snapshot. `skill.search`
now includes still-applicable, already offered native binding handles even when
the legacy skill snapshot is empty. `memory.search` keeps its knowledge results
and points to `skill.search` when a native handle is available. Unoffered Drafts,
foreign Runs and revoked evidence are not made discoverable by this bridge.

The Host states the capability-use protocol in both the initial offer and the
pending continuation: evaluate it against the public task, bind and execute a
matching capability, or explain a concrete conflict. This is not permission to
override the task or protected paths. Pre-binding reads justify slot values;
post-binding operations supply application evidence. The full procedure is
not repeatedly injected, and the pending protocol stops after binding.

Compilation distinguishes binding preparation from application. Current-file
discovery and reads needed only to establish slot evidence belong in
`necessary_checks`; they are not repeated as learned post-binding steps merely
to produce a receipt. The compiler and independent reviewer receive this
execution contract. Genuine later inspections remain valid when causally
needed. The ordered operation matcher is unchanged and still gives no
retroactive application credit to preparatory observations.

Semantic verification requirements are independent of test-authoring strategy.
The compiler receives existing Host planning obligations so it does not present
baseline reading, test tooling or final diff inspection as newly learned rules.
Focused cases remain necessary checks. Their current validation command can be
bound and observed without requiring an incidental edit of the historical test
file; a specific test-file transformation is required only when it is itself
the learned capability. Existing tests, new files and inline assertions still
pass through their normal permissions and original independent grading.

Three append-only records track each Draft/base/collection attempt:

| Record | Meaning |
|---|---|
| `learning_promotion_input` | Frozen validation digest and identity |
| `learning_promotion_dispatch` | Durable external-stage dispatch marker |
| `learning_promotion_result` | Native result, always `release_authorized=false` |

A completed call is idempotent. A changed validation plan is rejected. An
unfinished dispatch raises `LEARNING_PROMOTION_RECOVERY_REQUIRED`; operators
must reconcile the existing native stage and provider ledger before any new
attempt. The integration does not silently retry a possibly charged call.

The repository ships no benchmark dataset. Embeddings supply their own local
support tasks and probes. Memory v1 remains the default. Permanent publication
continues through the existing independent transfer checks and Release gates;
this API neither grants Release approval nor changes the active Release.
# Nominating a draft by a shared mechanism

The Host may use `repo_code.qualification_catalog.QualificationCatalog` when the
task titles or dataset family labels do not describe the repair. Construct the
catalog **before execution**, with a trusted set of public starting files per
task, fixed source/support/target roles, and operator-defined mechanism rules.
Use `catalog.family(task_id, initial_files)` for the existing session binding;
do not retag old Episodes or signed grants.

For example, “each item can be selected once” is a policy, **not** a diagnosis.
A faulty implementation might count an item repeatedly or skip a free item at
zero capacity. To select the first mechanism, combine the public policy with a
Host-owned behavioral observation. Run a frozen public probe against the
pristine task in the isolated sandbox. Bind its receipt to the rule's
`observation` key (a digest of the probe specification), the complete initial
file digest, and an explicit `MATCH` or `NO_MATCH`. An execution failure is
never a match. Do not accept receipts supplied by a worker or infer them from
the candidate answer. The controller owns probe execution and provenance; the
catalog is not an oracle or an authenticator.

After independent grading, call `nominate(draft, source_id, source_files=...,
passed_sources=..., passed_supports=...)` for drafts traced to that source run.
Only a nomination with independent, distinct, preassigned support may be
passed to the native `learning_pipeline.advance` with those complete frozen
support contracts and registered positive/negative/boundary probes. No grader
materials belong in inputs to the worker. Unknown or ambiguous mechanisms get
a task-local family; the same source or a copy of a held-out target is not
eligible support. No matching rule, no draft, a rejected review or failed S2
is a recorded outcome, not permission to inject a skill.

Test the result on a separate forward task: record the signed session grant,
the target's own application receipt and independent score, and check a
negative task for unwanted offers. An application receipt is **not** evidence
of a performance gain. This path never changes the permanent Release.
