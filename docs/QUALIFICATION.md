# Qualification record

## Function and hunk context — 2026-10-22

The production PR path now selects original-line excerpts around changed hunks
and enclosing Python functions. It reserves source budget for callers, guards,
tests, contracts and guidance, then redistributes unused space. Canonical source
and its full-file hash remain local; finding admission and citation validation
reject lines outside the exact model-visible ranges. `v5-function-hunk-context`
invalidates older review generations and reports. Publication adds a bounded,
revision-bound Context coverage dropdown from controller evidence.

Behavioral failures preceded implementation for large-file selection, test/caller
discovery, omitted-line citations, reporter coverage propagation, new-file hunks,
out-of-range patch termination, scope shadowing, nested validation helpers and
decorator-only changes. Tests also cover UTF-8 accounting, deletion-only changes,
far-away caller preconditions, literal coverage rendering and mismatched-revision
publication denial.

An offline same-commit replay on `e583fbe` expanded context from 8 changed
files to all 25 eligible changed files out of 27, within 100 KB. Two changed files
remained credential-pattern exclusions. This replay supplied 1,372 of 3,296 added
lines, 30 files and 134 source ranges. Broader file coverage is not complete review.
The replay used the final selector without model or GitHub calls. A subsequent
exact-head live review is qualified separately from this pre-publication record.

The [function-context receipt](qualification-function-context-2026-10-22.json)
records current source hashes and checks. Live model quality, production scale,
dynamic call resolution and complete test coverage are not established. The
existing review-only reader/reporter Apps and Claude subscription boundary remain
unchanged. No automatic repair, merge or deployment is authorized by this update.

## Earlier review pipeline and CI update — 2026-10-22

That source revision passed **375 tests**, with **15 explicit skips**: 13
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
authorized first-party Team account and usage credits disabled. The actual
adapter then returned schema-validated `CLAUDE_CODE_OK` from Sonnet 5.5. Its first
live attempt exposed missing `USER`/`LOGNAME` account identity for macOS Keychain;
a behavioral regression preceded that fix and service/provider secrets remain
excluded. This is a structured client smoke check, not a code-review quality
evaluation. Both adapter subprocess environments force `DISABLE_AUTOUPDATER=1`
so a review cannot update its own qualified client; two behavioral failures
preceded that fix. No provider fallback was introduced.
The dedicated reader and reporter Apps are now installed on Sir Patchington
only. Actual App API calls verified their separate identities, exact grants and
single-repository scope. The reader has Actions read; the reporter has PR write
and metadata read, without contents write. Review posting remains explicitly
policy-controlled and candidate publication requires exact approval.

A first manual live run on PR #1 at `fceb92b` used actual Sonnet review and
refutation calls. The App-based CI collector captured one successful workflow,
one job and 17 steps for that exact head. The run returned `needs_owner`: only
12 context files fit the 100 KB retrieval budget, and credential-pattern filtering
withheld some synthetic test files. This did not fully review all 24 changed files.
One model-supported finding contradicted the deliberate skipped-check policy;
the other, a terminal-error omission from bounded log evidence, was reproduced
with a mocked 64 KB response containing 25 error lines. These are bounded
execution observations, not a precision or recall benchmark.

That run exposed a report defect: stored review limitations were not displayed
in the comment. The current update makes those limitations visible, clarifies
the intentional CI policy and invalidates older cached generations. A subsequent
manual run and posted-comment readback are qualified separately from this
pre-publication record.

## Additional manual integration findings — 2026-10-22

The next real run on `4c054a7` posted the collapsed CI report and coverage
warning. The model's remaining proposal used an off-by-one source line; the
exact citation gate withheld it as `needs_evidence`. This is a false-negative
limitation, not proof that the change has no defects. The current source adds
coordinator-provided line numbers and prioritizes changed runtime source ahead
of large documents. New mocked regressions failed before those changes.

GitHub returned null `performed_via_github_app` metadata on the App-authored
comment. The publisher now verifies the App using its JWT and binds ownership
to the server-resolved numeric Bot ID and login. Live replay retained comment
`6345360996` and verified its exact body and authenticated actor. This qualifies
that observed reconciliation path, not production deployment isolation.

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
