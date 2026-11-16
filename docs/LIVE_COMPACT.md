# Live Context Projection (experimental)

`--context-compaction live-experimental` enables a deterministic projection over
completed history. The default is `legacy`; each Run records its mode/profile
and refuses resume with a different value.

Eligible old completed read/search/process groups are represented in provider
requests by a short card. The latest three complete tool groups, user turns and
checkpoint floors remain visible. Cards contain operation/source IDs, receipt
state, an untrusted bounded preview and a content-addressed reference readable
with `artifact.read`. Cards are historical data, not authority, acceptance or
L1 evidence. Raw messages, operations, receipts, artifacts and Memory sources
remain unchanged.

The planner performs no model calls or filesystem writes. It refuses incomplete,
failed, unknown, background or revoked groups, missing artifacts, unsafe native
reasoning/block protocols and any replacement that is not shorter after native
serialization. Commit fencing covers source hashes, Run control/writer epochs,
authorization, manifest, operations and revocations. Crash recovery aborts
uncommitted auxiliary records and retries deterministically; committed cards are
idempotent.

Memory v2.1 uses the projection during request packing, after original-source
Working State and retrieval preparation. Prefix summaries and live checkpoints
are boundaries, not permanent mode switches. Under pressure, a separate semantic
head summarizes only original closed groups after the latest live checkpoint.
New live cuts start after all semantic coverage. Segment heads have independent
CAS, revocation and source-hash checks; summaries never consume live cards.
Memory v1 may fold complete groups after its checkpoint into a diagnostic index
inserted after the frozen prefix. Neither path rewrites the committed prefix.

The default profile triggers when history exceeds 60% of the available history
budget and the forecast exceeds 75%. It preserves three recent tool groups,
considers at most 64 recent group slots. It normally commits one card; under
request pressure it selects earlier eligible groups and can commit up to four
cards per settled snapshot (configurable `max_cuts`, 1–8). Adjacent small groups
can share a card, and oversized successful results can use complete-source
reference cards. Every replacement must be smaller under the native serializer.
Failures and unknown results remain ineligible. Quoted assistant hypotheses
are separately labeled `MODEL_PROPOSED` / `UNVERIFIED`, never Host facts.
Use `--live-compact-profile path.json` or the SDK's `live_compact_profile` for
explicit overrides; the complete profile and its digest are pinned at Run creation.
Resume requires the same mode and profile. Isolated benchmark trials inherit
the selected compaction configuration.

```powershell
python -m repo_code.cli --provider openai --model gpt-6.1-terra `
  --reasoning-effort medium --base-url $env:OPENAI_BASE_URL `
  --context-compaction live-experimental run ...
```

Estimated native tokens, real Provider Usage, common serialized prefix and
cache fields are recorded as separate measurements. Each main request attempt
has its own record, including known usage on failures and dispatch status.
A sequence number and previous-attempt ID preserve ordering. In Live mode,
changing runtime budget feedback is carried as context data rather than in
the system prompt; this is not proof of provider cache reuse.
A common prefix is not a cache hit or a cost claim. Use fresh state roots for A/B groups. Paid benchmark
runs require `--i-understand-api-costs`; preflight is free. Windows is partial;
Linux native and Docker results are reported independently.

## Protocol capability matrix

| History envelope | Local Live Cut | Validation |
| --- | --- | --- |
| Plain chat text and paired function calls | Supported | Scripted and OpenAI-compatible native serialization, including DeepSeek plain chat |
| `reasoning_content`, including an empty value | Skipped for the whole history | Opaque reasoning is never removed or linked by a text heuristic |
| ChatGPT-plan native Responses blocks | Skipped for the whole history | No verified Responses reasoning/call-ID rebase capability |
| Anthropic native content/thinking/tool blocks | Skipped for the whole history | No verified native block/signature rebase capability |
| Unknown fields or non-text content | Skipped | Fail-closed protocol gate |

Serialization alone is not evidence of safe protocol rebasing. Unsupported
envelopes remain intact; a local skip does not certify semantic rebasing.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest -p pytest_asyncio.plugin tests/test_live_compact_*.py -q
python -m pytest -p pytest_asyncio.plugin tests/test_context_request_budget.py tests/test_memory_v21_budget.py -q
python scripts/check_distribution.py
python scripts/live_benchmark.py --provider openai --model gpt-6.1-terra `
  --reasoning-effort medium --base-url $env:OPENAI_BASE_URL `
  --sandbox docker --image YOUR_PINNED_TASK_IMAGE `
  --state reports/live_compact/ab-preflight --report reports/live_compact/ab-preflight.json `
  --preflight-only
```

Return new Runs to `legacy` to roll back. Existing live Runs must resume with
their recorded profile or be stopped and inspected. Disabling the profile does
not rewrite raw evidence; unreconstructible projections fail closed.
