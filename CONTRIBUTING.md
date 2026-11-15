# Contributing

Install Python 3.11+ and `python -m pip install -e '.[dev,chatgpt]'`.
Use a fresh state directory outside the task workspace. Keep credentials and
model authorization in private Host storage, never in tests or task containers.

Run `python -m pytest -p pytest_asyncio.plugin tests -q` for developer checks.
Some isolation tests require Linux root; a skipped test is not an acceptance.
Use a disposable Linux host/CI for `scripts/validate.py --suite native --timeout-seconds 1800` and the
documented Docker suite separately. Live model tests are opt-in and may consume
account usage; never enable them in an untrusted pull-request workflow.

Preserve Memory v1 defaults, source/evidence provenance, independent validation,
revocation, and Release gates. A saved Draft or successful model response does
not authorize capability activation. Report IMPLEMENTED, TESTED, BLOCKED and
NOT_RUN separately; do not describe a deterministic or scripted test as a model
performance gain.

Do not commit local datasets, official solutions/hidden tests, runtime state,
Host signing keys, raw user traces, OAuth files, `.env`, model weights, virtual
environments or generated build outputs. Small synthetic unit-test fixtures are
allowed when they are necessary to reproduce behavior and have no private data.

For documentation-only/publication changes run `scripts/check_distribution.py`.
`SHA256SUMS` describes the published source snapshot; regenerate it when making
a new reviewed source snapshot, rather than claiming old digests cover changes.
