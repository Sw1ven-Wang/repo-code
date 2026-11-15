# Security and assurance boundaries

## Threat model

repo-code is a local, single-operator tool. The trusted operator, installed Host package, operating system kernel, Python runtime and isolation backend are trusted. Submitted source, test code, LLM content, MCP responses and evolved implementations are not trusted. This is not a multi-tenant hosted service, a formal sandbox proof, or protection against a privileged attacker controlling the Host.

The authoritative SQLite database, HMAC key, hidden evaluation records, model credentials and Publisher remain outside task mounts. Evolved Python is never imported in the privileged Host. File path allowlists are not substitutes for OS isolation.

## Backends

`native` is an explicit Linux development/test backend: fixed launcher, chroot, randomly allocated nonprivileged UID, cleared supplementary groups/environment, no-new-privileges, libseccomp syscall denials, resource limits and owned process-group cleanup. It requires root/capabilities. Use a disposable VM/container. It has not undergone a formal escape audit and is not claimed equivalent to a production container platform. Host hard death can leave uncertain process lifetime; recovery blocks rather than guessing.

`docker` uses a preinstalled digest-pinned image, non-root UID, no network, read-only root, dropped capabilities and resource limits. Code is included, but the delivery environment has no Docker daemon; this backend and Windows/macOS integration are unverified. MCP currently refuses a non-native stdio adapter.

`local` deliberately lacks isolation. Its results cannot count as trusted external evidence for critical requirements or evolution activation. No automatic fallback from isolation to local mode exists.

Native dependencies are prepared from the trusted host environment into a read-only runtime kit. Do not install untrusted packages in that Host environment. Network access/package installation from candidate code is blocked. Docker users should build and review their own task image separately.

## Verification means registered observable behavior, not universal correctness

The Host never imports DUT modules. Python function and ASGI adapters run inside the untrusted domain and return observations; expected values and comparison run outside. A Python function adapter verifies its exposed JSON RPC behavior, not an unforgeable proof of arbitrary internal Python semantics. A hostile program may special-case observed inputs. Private held-out cases reduce, but do not eliminate, gaming.

Ordinary pytest/diagnostic output is `SANDBOX_IN_PROCESS`, including exit codes and collection reports. It cannot satisfy any mandatory check, including global-required checks. Each Check freezes its permitted authorities; diagnostics remain supplementary. A read-only test file does not make a same-interpreter verdict trustworthy.

Delivery is scoped to `registered_checks_only`. Incomplete contracts, weak requirements or bad expected values remain an operator-review risk. Generated acceptance drafts are not auto-approved. Required checks are validated independently of requirement coverage. SOURCE, BUILD and ARTIFACT evidence are not interchangeable.

## IPC and recovery

A per-Run/audience token authenticates loopback IPC. Only Host-registered tool IDs may be submitted. Same request key and parameters returns the original operation; changed parameters are rejected. Response loss is resolved by querying the original operation. This is local duplicate recognition, not universal exactly-once delivery.

Unknown process/MCP effects are never automatically replayed. File Journal recovery compares current bytes with prepared before/after states and preserves external edits. Journal+filesystem are not a multi-file ACID transaction. Parent directory creation may remain after a compensated batch.

Run-level executor locks prevent two live task loops. Cancellation admission revokes future operations before requesting process termination. A timeout or lack of ownership proof remains `NEEDS_RECONCILIATION`. A stop request is not proof that all external side effects were undone.

## Evolution and hidden evaluation

Gates, cohorts, signing, budgets and release authorization are not worker capabilities. Every candidate is tied to a complete release; per-component blind promotion is prohibited. Existing runs retain their pinned release, knowledge and model configuration.

Only DEV evidence is available to candidate construction. Hidden prompts, traces and detailed scores remain private. Verdict exposure propagates to descendants and immutable full-component content identities. Hidden eligibility is checked and reserved in one transaction, before execution; abandoned reservations remain consumed. Random candidate IDs or build metadata do not erase prior use. Identical task payloads are detected even after ID renaming; detecting all semantic near-duplicates still requires curated cohort families.

Scripted-model/fixture reports are `INTEGRATION_ONLY` and cannot activate in ordinary state. The explicit offline demo enables a separate operator flag solely in its private demo state. Do not reuse demo state as production state. L2 self-modification is off by default and requires explicit operator consent plus all gates.

G4 now executes a fixed candidate-bound compatibility/safety suite: every evolved component is invoked through its isolated ABI, and a fresh Host runs coding, control, authority, mutation, knowledge and delegation regressions against the exact candidate release. The Publisher rejects missing cases or mismatched release/environment/suite identities. This finite suite and the separately shipped full tests do not constitute exhaustive security certification. Any change to trusted code needs normal human review and regression, not an RSI bypass.

## Secrets and retention

Known credential forms are redacted from audit display; this is best-effort pattern matching, not guaranteed secret discovery. Raw artifacts, messages, backups and source snapshots can contain private code and data. Restrict access to the private state directory.

Calling a real model sends selected task content to the configured provider. The operator must authorize that transfer and avoid loading secrets in the task source. No telemetry destination is configured by the project.

`knowledge revoke` prevents further usage, including previously pinned snapshots; it does not securely erase historical CA blobs, backups or provider copies. Fine-grained privacy erasure is **not implemented**. Where deletion is mandatory, stop the Host, revoke access, retire affected state/backups under the operator's policy, and obtain confirmation for external copies. Do not claim cryptographic erase from file unlinking.

Backups include the local HMAC key and source bytes. Keep them outside project roots and access-controlled. Restoration only targets a new directory and fails closed on incomplete markers. Maintenance is offline; do not run it concurrently with other Hosts.

## Reporting

Report problems with version, backend, platform and a minimal synthetic reproduction. Never attach live keys, private state databases, hidden task corpora or recovery blobs to a public issue.

## v1.5.1 control and data limitations

Forward file effects recheck authority inside the writer fence and a bounded SQLite write transaction. Cancellation does not mark a Run terminal while any file/process/MCP Operation is executing or uncertain. A previously executed effect may remain when cancellation arrives after it; cancellation is not a guarantee of rollback. A partial batch is compensated before settlement, subject to version checks.

MCP sending is treated conservatively: EOF, timeout, malformed replies and write/drain failures after a send attempt can represent an already-performed effect. Such operations remain UNCERTAIN; even stopping the server does not establish external rollback. The adapter does not invent a remote idempotency guarantee.

State migration is offline only. Stop every old Host before opening schema 15 with v1.5.1. Schema 16 prevents a newly started v1.5.0 writer from accepting the database; it does not fence an old process that was improperly left running. Old signed acceptance/gate evidence is not re-certified under the new Host. See the upgrade guide before using an existing state root.
