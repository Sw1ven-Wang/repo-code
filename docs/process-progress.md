# Repeated process and inspection observations

The Host protects each Run against repeated synchronous `process.run` inspections,
with Memory either enabled or disabled. This is independent of the release's
optional failed-process retry hints.

* Three identical completed observations produce a warning in the next request's
  Host runtime status. That status includes remaining tool/model calls and time.
* Five identical observations with unchanged source, writer/control epochs and
  sandbox identity exhaust the normal allowance. A subsequent identical request
  returns `PROCESS_NO_PROGRESS`, `executed=false`, and the existing output artifact.
  It is committed as a nonexecuted `CANCELLED` operation without consuming a tool
  slot. Replaying that same operation remains idempotent.
* The model can read the artifact, narrow an inspection, edit, or test a new
  hypothesis. A new source state or changed observed outcome starts a new streak.
  Counts are per command, so alternating two unchanged commands does not evade it.
* For transient or external-state changes, `process.run.retry_reason` allows up
  to two additional identical executions. Each reason must be nonempty and new
  for this streak. This does not grant permissions or extend any resource budget.
* Two consecutive model turns with withheld repetitions and no new evidence end
  `PARTIAL / NO_PROGRESS`. Completed work remains exportable. A turn that performs
  a genuinely informative action can recover normally; unrelated `ls` does not
  erase the streak. No patch is manufactured by the guard.

Identity covers normalized command arguments (excluding the reason), source,
sandbox environment, exit/error outcome, complete stdout/stderr hashes and output
snapshot. Lease IDs, elapsed time, and API tool-call IDs are deliberately excluded.
Timeouts, cancellation, unfinished processes and unknown outcomes do not establish
an identical-observation streak. Arbitrary commands are not assumed to be pure:
external services can change without a workspace change, hence justified retries.

Context projection replaces older identical process outputs with references to
the newest occurrence and an authorized artifact. Raw messages, evidence and
assistant/tool pairing are preserved. Memory packing reclaims older expansions
before reducing the latest real process observation, even if a withheld request
follows it. Very large outputs still obey the original context limit and may need
bounded `artifact.read` calls. This does not change task acceptance or permissions.

This guard addresses wasted execution and missing progress feedback. It does not
prove a model will produce a correct patch, nor guarantee detection of every loop
whose arguments or output differ on each iteration. Existing frozen experiments
remain historical evidence; use a new release/Memory implementation pin for a new
experiment instead of modifying their locks or scores.

The same Host boundary also covers deterministic inspections: `read`, `search`,
`glob`, `artifact.read`, `history.search/expand`, `memory.search/read/explain`, and
`skill.search`. Three identical successful observations warn; five exhaust the
allowance for that inspection under the same source, writer/control epochs and
environment. Further repetitions return `INSPECTION_NO_PROGRESS` without consuming
a tool slot. Alternating tool names does not erase each inspection's history.
Two withheld batches without new evidence stop with the same `PARTIAL / NO_PROGRESS`
outcome as process loops; an intervening edit or genuinely different inspection
can recover. A new user turn, changed source or a new page permits inspection.
Large-output bounding preserves the inspection identity, and context projection
replaces older duplicate bodies with archive references while preserving raw
assistant/tool pairs. Failed inspections are not treated as successful evidence.

The 2026-11-05 Host layer additionally tracks version/turn/environment-fenced
coverage across tools: overlapping read ranges, ordinary cat/sed reads, original
artifact pages, unchanged successful validation summaries, and environment
listings. First reads of new pages and changed failures remain useful. Six
batches without new evidence warn; ten end PARTIAL, with `STAGNATION_AFTER_WRITE`
when a candidate exists. Plan rewordings and timeout variations cannot keep
resetting this counter; two distinct explicit flaky/external retries remain.
An edit, new user turn or environment change resets the fence. This is bounded
workflow protection, not a semantic proof of completeness or every possible loop.

`runtime_feedback.work_ledger` keeps changed paths, the last write, proposed plan
steps and the last same-version local check separate from unrelated commands.
Its local evidence does not satisfy acceptance. `verify_before_finish` now emits
explicit check/handoff guidance. A no-tool final without an internal contract
ends `PARTIAL / CANDIDATE_READY_EXTERNAL_EVALUATION`; stagnation never verifies
success or advances a continuous task to WAITING_USER.

The inspection guard identifies exact repeated requests and observations, rather
than assuming that a long sequence of distinct reads is useless. It does not
prove implementation correctness or detect every loop that changes arguments or
produces genuinely different evidence.
