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

## Offline publication checks (2026-11-15)

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

## Experimental limits

- Real-model A/B/C/D cohorts, repeated trials, quality/transfer gates and the
  planned 15% cost target remain unverified. Offline correctness does not prove
  quality, cache or cost improvement.
- Native token estimates, serialized common-prefix bytes and real provider
  usage are different measurements. A stable prefix is never reported as a
  cache hit.
- Each settled snapshot can commit at most one card; candidate selection
  considers at most 64 recent group slots by default. This conservative first
  version does not implement multi-cut maintenance.
- Local cuts skip opaque native reasoning/block protocols. A committed live
  checkpoint cannot be crossed by semantic compaction or legacy refolding;
  insufficient remaining context fails closed.
- The package version remains 1.5.1. The default remains `legacy`; no automatic
  Release activation, main-branch merge or version tag is part of this change.

Windows tests are a portability check. Linux native and Docker isolation are
validated separately; skipped isolation checks cannot count as acceptance.
