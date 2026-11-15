# repo-code / Harbor integration

This optional integration runs the existing `CodingAgent`, `Runtime`, `Broker`,
`Mutator`, context engine and release configuration on a trusted Host. Only
public source files and the public instruction enter the implementation loop.
`process.run` executes in the current official Harbor task container, with its
installed dependencies. File edits are versioned in the Host mirror and synced
before commands and patch collection. Remote syncs have durable receipts;
unknown process/transport outcomes are not retried.

The adapter never invents an internal acceptance Contract. An internal
`PARTIAL / EXPLORATION_ONLY` completion is expected; the official verifier owns
the score. The task's `tests/`, `solution/` and reference patches stay on the
trusted evaluation side and are not uploaded during an implementation run.

## Runtime

- Python 3.12+, Harbor 0.25.0, OpenAI SDK 3.42.0.
- `Dockerfile.controller` supplies the trusted Linux control Host. Its Docker
  socket is a trusted orchestration capability and is never mounted in task
  containers. Do not install or execute generated task code in the controller.
- `litellm==1.109.0.dev2` is pinned because stable LiteLLM releases at integration
  time reject OpenAI SDK 3.x. Actual inference uses repo-code's OpenAIProvider.
- The exact controller dependency resolution and image ID are saved in the
  evaluation report directory. The image is distinct from the official task
  image; task containers keep the official image and dependencies.

`DockerPhaseEnvironment` supports Linux single-container tasks with public and
no-network phases. It physically detaches the task's Docker network endpoints
and verifies loopback-only networking before an offline phase, restoring the
original endpoints for public verifier phases. It does not emulate allowlists
or accept unsupported multi-container tasks. This is needed on Docker Desktop
kernels without Harbor's nftables `fib inet` support; the official verifier is
unchanged.

## Pro V2 single-task commands

Run inside the trusted Linux controller, with `PYTHONPATH` pointing at this
project's `src`. Use fresh job names and keep official source bytes unchanged
(`git archive` avoids Windows CRLF checkout conversion).

```bash
python scripts/harbor_evaluate.py --task /official/v2/tasks/INSTANCE \
  --jobs-dir /results/jobs --job-name oracle --phase oracle
python scripts/harbor_evaluate.py --task /official/v2/tasks/INSTANCE \
  --jobs-dir /results/jobs --job-name nop --phase nop
# Continue only after oracle=1 and nop=0.
# Set DEEPSEEK_API_KEY_REPO only in the trusted Host process environment.
python scripts/harbor_evaluate.py --task /official/v2/tasks/INSTANCE \
  --jobs-dir /results/jobs --job-name model --phase agent --i-understand-api-costs
python scripts/harbor_evaluate.py --task /official/v2/tasks/INSTANCE \
  --jobs-dir /results/jobs --job-name regrade --phase regrade \
  --source-job /results/jobs/model --official-tooling /official/v2/tooling
```

Report the unchanged official `patch_replay:PatchReplayAgent` result from a
fresh image as the authoritative Pro V2 score. Preserve the original in-place
score too. Check replay's `apply_rc` and captured `agent/model.patch`; a process
exit code or an internal delivery label is insufficient.

The default pilot has 30 model calls, 150 tool calls, 3,000 seconds, 8,192 output
tokens/request, and a 1,200,000 aggregate token limit. Inference uses explicit
thinking=enabled, effort=high, omitted temperature and zero SDK retries. This
is a locally limited budget, not a full leaderboard-comparable configuration.
Provider usage and request attempts are persisted as they occur. Token reserve
checks happen before requests; missing usage or uncertain inference stops the
run. No billed monetary amount is fabricated.

## Boundaries

The initial integration targets L0 evaluation. It does not qualify L1/L2
candidates on the Harbor backend. Generated policy Python, asynchronous
`process.start`, and automatic re-execution of uncertain effects are rejected.
Remote process groups are stopped before collection; cancellation stops the
owned task container. Baseline source symlinks are omitted from Host file tools
and recorded; they remain in the official image for commands and official
patch capture. The standard Host workspace size/path limits still apply.

E2E-SWE project installation/separate verification and Terminal-Bench final
state support must each pass their own single-task integration before a fixed
multi-benchmark pilot. Selection tools alone do not demonstrate that support.

Tests: `tests/test_deepseek_protocol.py` plus explicit real-container tests in
`integrations/harbor/tests/`. The latter require
`REPO_CODE_HARBOR_TEST_IMAGE` and contain labelled ScriptedProvider protocol
fixtures; their results are never reported as real model benchmark scores.
