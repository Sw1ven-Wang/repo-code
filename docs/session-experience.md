# Session experience and external evaluation

Session reuse is advisory, expiring and revocable. It does not activate a
candidate Release, grant tool privileges or change delivery acceptance.
Memory v1 remains the default. Episode and Evidence Selector are unchanged.

## Host API

```python
session = app.session_experiences().create(project_scope, ttl_seconds=86400)
outcome = await app.procedure_promotion(runner).run(
    draft_id, planner, independent_reviewer,
    support_tasks=local_tasks, probes=local_probes, session_id=session,
)
# SESSION_READY means real semantic review + S1/S2 local validation, NOT publication.
run = app.new_task(goal, workspace, contract, budget,
                   authorize_write=True, authorize_process=True,
                   session_id=session, task_family=applicability_family)
report = await app.execute(run['id'])
app.session_experiences().revoke(outcome['grant_id'], 'Observed counterexample')
app.session_experiences().close(session)
```

Only the trusted Host assigns project scope and task family. Model tools cannot
create/bind/admit a session. A new run must explicitly join the session before
model execution; sessions never implicitly attach across chats or projects.
One state-root hosts a session's successive runs; each run retains its own copied
workspace, budget and delivery evidence. A new independent state-root does not
inherit session grants. A closed session stays archived, not globally active.

For an already compiled procedure, `prepared_plan` can pin the exact saved plan,
its source digest, public support-task digest and origin. This is labelled Host
input with zero new planning calls, not invented model output. Independent review
and local execution checks are still required; historical qualification is not
transferred to a changed Host/environment. Never silently regenerate action
bindings when the experiment is intended to reuse an existing procedure.

CLI: `session create --scope ...`, `session admit SESSION --contract-ref ...
--candidate ...`, `session revoke GRANT --reason ...`, `session close SESSION`,
`session status SESSION`. A later `run --session-id SESSION --task-family ...`
uses the normal model/provider and permission options.

## Persistent contract and recovery

* `experience_session`: OPEN/CLOSED, scope, expiry (maximum seven days), generation.
* `session_run`: signed binding to run, session, scope, family, release and environment.
* `session_grant`: immutable signed exact experience revision and capability contract,
  S1/S2 digests, environment, expiration. At most 16 grants per session.
* `session_grant_state`: ACTIVE/REVOKED. Re-admitting the same grant never revives it.
* `session_feedback`: immutable run/receipt/outcome/Usage; causal benefit UNKNOWN.

Every retrieval checks signature, expiry, scope, source revocation/validity,
conflict status and frozen local validation. It also retains normal trigger,
exclusion, phase and file-version checks. Routing keys include the current grants;
the Host rechecks authorization before offering guidance. Existing receipts retain
historical evidence after revocation. Guidance is offered once, with explicit
withdrawal notice if authorization later becomes inactive. Already read guidance
cannot be erased from model history; it never grants authority in the first place.

An applied procedure followed by unsuccessful delivery is conservatively revoked
pending review; this is not a claim that the procedure caused the failure.
Feedback errors are audited and cannot rewrite task delivery. Durable admission
is idempotent; interrupted model stages retain the coordinator's existing
reconciliation requirements. Permanent publication still requires S3 and all
Release G0/G1/G2/G3/G4/G4b checks, approval, and CAS.

## Applicability versus contamination

New trusted cohort registrations supply both `applicability_family` and explicit
`contamination_groups`. The first describes where a procedure is relevant; the
second records shared source/template/near-duplicate lineage. Independent instances
of the same mechanism may be validated. Shared contamination groups, repeated
task fingerprints, identical implementations, exposed ancestors and repeated
reservations are still rejected. A trusted evaluator must assign provenance;
renaming a group is not evidence of independence. Exact implementation checks do
not automatically detect all semantic near-duplicates.

Legacy cohorts keep conservative family contamination rules. Declaring a v2
cohort cannot erase a candidate's old family exposure/reservation. This change
does not retroactively qualify old blocked candidates.

## Distribution contains no evaluation datasets

The installed project ships code and normal runtime dependencies, not SWE-bench,
SymPy task packs, local traces, hidden tests, or this machine's E: evaluation paths.
No dataset is automatically downloaded or required at ordinary startup.
The trusted evaluator supplies local/user task evidence or an external benchmark
adapter when qualifying a new permanent capability. If evidence is unavailable,
the candidate remains unqualified; ordinary task execution continues normally.

Do not publish a raw state-root: it contains local workspaces, task inputs, private
evidence and Host keys. Python wheels contain only the software; experiment archives
are separate local audit artifacts. Existing built-in security regression probes
remain part of the trusted execution implementation, not a bundled benchmark.
