# Live Compact validation review

Status: **EXPERIMENTAL_ONLY**. Baseline: `7358a792838759cac894206993eecfdf3bc0c609`.

This change adds an opt-in deterministic history projection, not a new default
or a qualified Release. See [behavior, configuration and rollback](LIVE_COMPACT.md).
It borrows design principles from `Tranquillitatis-Lab/dsh-live-compact`
at `b33fc52730045473e13ef4f6619686af27a0548b`; no DSH implementation or runtime
is imported.

## Coverage

The feature tests exercise complete tool groups, recent-group retention,
user/checkpoint floors, failed/unknown/background-operation refusal, native
request size reduction, scoped/hash-verified artifacts and revocations, Run
profile pinning, control fences, five crash points, concurrent commit
idempotency, Memory v2 integration and original L1 Episode sources. Benchmark
trials now inherit their parent service's compaction mode and profile.

All new tests use local fixtures or scripted providers. No paid model calls or
official SWE-bench gold patches/hidden grading material are required.

## Initial offline publication checks (2026-11-15, `85939f8`)

| Check | Result |
| --- | --- |
| Full Linux native suite | 946 passed, 0 failures/errors/skips; 769 seconds |
| Docker backend suite | 11 passed, 0 failures/errors/skips |
| Independent design reference | 13 passed; reported separately from implementation tests |
| Final-source Linux feature suite | 56 passed, 0 failures/errors/skips; included in the full-suite case set |
| Windows feature suite | 56 passed; portability check only |
| Source distribution check | 269 hashes, syntax and documentation links passed |
| Wheel builds | Windows and Linux passed; both new modules present |
| Staged source review | 24 selected files; no environment credential or token-pattern scan hits |

Native test host: `repo-code-native-validation` at
`sha256:eb25bbe4ad55ebfb3840a62d3f182ba864386f912ffd0826360c14cf7d5eaf88`,
with OpenAI SDK 3.42.0. Docker task image: `repo-code-sandbox` at
`sha256:994f52230f4076c130f60882056d3f82adb3359dda1d1bfbe41a9ffa41811607`.
The native test container had no network or model credentials.

The full suite used a frozen snapshot. Before publication, `agent.py` line
endings were restored to the baseline CRLF bytes without changing its text.
The final feature suite and successful Docker run use the final package Python
bytes. An earlier Docker attempt was rejected by `WORKER_ENVIRONMENT_CHANGED`
while this source normalization was in progress; it is not counted as a pass.

The stock native image lacks packaging tools. The separate offline Linux build
added setuptools 72.1.0 and wheel 0.45.1 to a disposable container, then verified
`scripts/build.sh` as an unprivileged user with root-owned source caches left
by prior compilation. Existing declared distribution paths were retained when
refreshing the hash manifest.

## Review corrections

Checkpoint protection now applies to the committed prefix only. Memory v1 can
fold complete tail groups into an index after that prefix. Memory v2.1 maintains
independent semantic heads over disjoint raw-source tails, and new local cuts
start after existing semantic coverage. Semantic CAS also fences the live head
and authorization; request admission includes all segment node IDs.

The planner can join adjacent small groups and pointerize oversized successful
results. Under request pressure, it can advance through at most `max_cuts`
eligible spans (default four) before fallback. Failed/uncertain observations
remain ineligible. Model explanations are retained as bounded, quoted,
unverified proposals separate from Host receipts.

Per-attempt request records retain dispatch outcome, usage (including known
usage on failure), serialized prefix measurements and projection work counts.
The [protocol capability matrix](LIVE_COMPACT.md#protocol-capability-matrix)
states the current safe subset; opaque reasoning/native blocks remain skipped.

The review regressions include 300 additional real Broker tool groups after a
checkpoint, joint-span replay, oversized results, bounded multiple cuts,
Live/semantic/Live/semantic continuation, restart and revocation, stale semantic
commits, stable system prefixes and known usage on failed model attempts.
In the Linux review run, the 300-group scenario took 54 seconds; measured
preparation at 20/160/300 groups took 0.17/0.88/1.00 seconds on that test host.
These are synthetic regression measurements, not real-model performance gains.
Full native validation and wheel checks are also run by the repository's
`Linux offline integration` workflow on each PR head. Final review results are
recorded in the PR checks and description; the table above describes the
initial publication commit only.

## Experimental limits

- Real-model A/B/C/D cohorts, repeated trials, quality/transfer gates and the
  planned 15% cost target remain unverified. Offline correctness does not prove
  quality, cache or cost improvement.
- Native token estimates, serialized common-prefix bytes and real provider
  usage are different measurements. A stable prefix is never reported as a
  cache hit.
- Candidate selection considers at most 64 recent group slots by default.
  Snapshot validation, checkpoint reconstruction and fencing still scan history;
  this is not an O(1) or demonstrated low-latency implementation.
- Local cuts skip opaque native reasoning/block protocols. A committed live
  checkpoint cannot be crossed by semantic compaction or legacy refolding.
  Its right-hand tail remains eligible; a mandatory prefix, current request or
  retained decisions that exceed the request window still fail closed.
- The package version remains 1.5.1. The default remains `legacy`; no automatic
  Release activation or version tag is part of this change. Merging this opt-in
  implementation does not qualify it as a completed real-model experiment.

Windows tests are a portability check. Linux native and Docker isolation are
validated separately; skipped isolation checks cannot count as acceptance.
