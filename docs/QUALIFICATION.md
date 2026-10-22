# Qualification record

## Review pipeline and CI update — 2026-10-22

The current local source passed **355 tests**, with **15 explicit skips**: 13
PostgreSQL tests require a disposable test database and two service-image tests
require a rebuilt image. Real Docker original/candidate checks and the offline
intake → review → approval → local Git publication → rereview demonstration ran
successfully. Model generation, GitHub metadata, CI results and comment writes
remain simulated in that demonstration.

Ruff (`src tests deploy scripts`), `uv lock --check`, wheel/sdist build,
`git diff --check` and a redacted secret scan of changed files passed. Behavioral
regressions were observed before implementation for irrelevant context starving
dependencies, unsupported critic labels reaching repair, unchanged-line findings,
finding identity collisions, missing CI integration and bounded CI log tails.

The new controls prioritize scoped context, require fingerprint-bound exact
source citations and counterarguments, abstain on invalid/conflicting judgments,
and filter exact duplicates and out-of-change anchors. CI inspection captures
exact-head job/step failure evidence and emits a collapsed review section even
when there are no supported code findings. Its HTTP transport and error paths
were tested with mocked GitHub responses, not a real App installation.

The [update receipt](qualification-review-pipeline-2026-10-22.json) records current
input hashes, tests, skipped prerequisites and remaining limits. The original
[baseline receipt](qualification-receipt.json) retains its dated 2026-10-21
measurements and links this update; its old coverage/image/audit measurements
must not be read as new measurements of this source revision.

No live model-quality benchmark or deployment was performed. Claude Code
`2.1.306` is now installed and its macOS code signature verifies; the exact
adapter pin is unchanged. A separate live subscription preflight confirmed the
authorized first-party Team account and usage credits disabled. Generation has
not yet been qualified in this record. Both adapter subprocess environments
force `DISABLE_AUTOUPDATER=1` so a review cannot update its own qualified client;
two behavioral failures preceded that fix. No provider fallback was introduced.
Operational CI inspection still needs Actions read permission on the dedicated
read App; no live App enrollment is claimed here. Review posting remains
explicitly policy-controlled and candidate publication requires exact approval.

## Original baseline — 2026-10-21

The following evidence and delivery history describe the original dated baseline.

The repository is an implemented control plane with local integration evidence.
It is not a certified production service or a measured replacement for a
commercial reviewer. In particular, no real model generation or real-model API
test was performed, and no existing repository was enrolled or changed.

## Evidence collected

- Strict policy, context, transport, model-schema, candidate, approval and
  publication tests exercise both allowed paths and adversarial denial paths.
- Real disposable PostgreSQL schemas exercise eight-replica claim/receipt
  contention, tenant capacity, per-PR exclusion, lease fencing, recovery,
  immutable records and concurrent schema initialization. SQLite tests are
  separate development behavior evidence.
- Real temporary Git repositories exercise canonical reconstruction, protected
  paths, binary-asset preservation, source admission above 5,000 files, branch
  races and exact parent/tree/commit remote readback.
- Real Docker checks exercise original-failing/candidate-passing regression
  evidence, early-exit tampering, source mutation, blocked network, absent
  credentials/socket, nonroot execution, read-only roots and cleanup.
- The offline demo executes signed ASGI intake, durable queue work, Docker
  verification, authenticated approval, clean reconstruction and local bare-Git
  compare-and-swap. It explicitly simulates model responses, GitHub metadata
  and CI. Its JSON receipt is the machine-readable boundary.
- Container-image startup and API/PostgreSQL readiness checks use isolated
  synthetic enrollment without App credentials or model authentication.

The [machine-readable receipt](qualification-receipt.json) records 263 passing
main-suite tests and two image-dependent skips subsequently passed in the
55-test frozen-image/PostgreSQL/deployment run: 265 distinct tests validated,
89.04% statement coverage. Ruff, lock/build, Compose, dependency audit (36
packages; zero known vulnerabilities), staged secret scan and the offline CLI
demonstration passed. One locked Starlette TestClient deprecation warning remains. A green suite supports these bounded
control-flow claims; it does not measure finding precision, bug recall, repair
generalization, production throughput or uptime.

## Required before real organization operation

1. Provision and protect PostgreSQL, shared artifact storage, backups and a
   dedicated execution daemon. Demonstrate restore and process-kill recovery in
   the actual deployment. Verify TLS, egress controls and storage permissions.
2. Install separate scoped GitHub Apps only on authorized repositories; verify
   their real installation IDs, webhook delivery, rate limits, branch rules and
   ambiguous-write reconciliation on a disposable private repository.
3. Provision project-specific trusted images and independent acceptance checks.
   Validate native/hardware/security lanes that the repository actually needs.
4. Separately qualify the pinned subscription CLI's supported flags, auth,
   model identity and quota. Keep API-provider fallback disabled. Requalify after
   client updates. No current live-auth or billing qualification is asserted here.
5. Evaluate real historical/labeled PRs by team and defect class. Measure useful
   findings, false positives, missed bugs, harmful fixes, abstentions and cost.
   Fixture success is not model-quality evidence.
6. Retain exact candidate approval and server merge/release controls. Enable
   comments and publication only in explicit repository policies after the
   surrounding deployment has been qualified.

These are operational prerequisites for the user-controlled environment; the
code repository does not silently create credentials, start model work, deploy
infrastructure, or bypass them.

## GitHub delivery

Created and verified private repository `vchamarthi/sir-patchington`. The initial
implementation commit was `7b7cc79b12e61b68e656b0de18b1e02356b07231`; remote readback
matched local Git exactly. The repository disables auto-merge, uses read-only
workflow tokens, and prevents Actions from approving PR reviews. Initial CI
started successfully; the latest run is visible under the repository's Actions.

GitHub rejected private `main` branch protection with HTTP 403 because this
account requires GitHub Pro for that feature. The repository remains private.
Required reviews, required CI, force-push/deletion prevention and conversation
resolution were therefore **not enabled as server-enforced main-branch rules**.
The bot's separate exact-approval/no-merge controls remain implemented. Before
using this repository as a shared production control plane, enable equivalent
server-side rules on a supported private account/organization plan. No account
upgrade, visibility change or paid service was performed.
