# GitHub boundary and publication

`github.py` is a credential-bearing boundary for GitHub Cloud. REST requests pin `X-GitHub-Api-Version: 2026-03-10`; enterprise endpoints are deliberately rejected until their API and clone-host mappings are qualified. HTTP environment proxies and redirects are disabled. Pagination accepts only the same API host, endpoint, and repository identity, including GitHub's numeric `/repositories/{id}` form. Responses are streamed with a 10 MB ceiling, pagination has page/item limits, and repeated cursors fail closed.

`AppTokenProvider` signs RS256 App JWTs, backdates issuance by 60 seconds, and mints a fresh token scoped to exactly one repository. No broad installation-token cache exists.

| Purpose | Installation token permissions |
| --- | --- |
| Read | contents, pull requests, checks, statuses, metadata: read |
| CI inspection (`ci_token`) | actions, checks, statuses, metadata: read |
| Candidate push (`write=True`) | contents: write; metadata: read |
| Review comment (`review_token`) | pull requests: write; metadata: read |

Install the App only on approved repositories. App configuration must grant these permissions, but each minted token requests only the purpose-specific subset. Custom token-provider callbacks must preserve this isolation; production uses `AppTokenProvider` directly. App keys, approval keys, scoped tokens, and publisher access belong outside model processes and repository-execution containers.

When read and write credentials belong to different Apps, their installation IDs also differ. `SplitTokenProvider` takes an explicit `(read installation, repository ID) → write installation` mapping, mints reads with the read App, and routes push/review tokens through the enrolled write App. Missing mappings fail closed. Its App identity for review ownership is the write App. Review posting is dispatched through the publisher queue; read workers carry no write-App key.

## Source and review capture

`snapshot` checks the repository's numeric identity, reads the current PR, all bounded file pages, and all bounded GraphQL review-thread and nested-comment pages. Resolved/outdated state and author/update provenance are retained. File counts must match GitHub's declared changed-file count. A second PR read rejects changed head, base, branch, title, body, or file count. Closed, merged, missing-head, and unauthorized draft/fork states fail closed. A snapshot is an observed version, not a transaction across GitHub's independently mutable objects.

Paginated top-level human review summaries are included as synthetic `review:<id>` feedback threads. Bodies retain the review state; dismissed reviews are marked resolved, old-commit reviews are marked outdated, and author/submission provenance is preserved. Explicit Bot summaries and unsubmitted pending reviews are excluded; unknown author metadata remains untrusted data. Inline comments and summaries share one total feedback byte bound. General issue discussions remain an extension; issue-comment text never authorizes commands.

`list_open_prs(policy)` returns eligible PR numbers for reconciliation without reading source or review threads. It validates repository/list-row identity, follows bounded pagination, and skips deleted-head and policy-disallowed draft/fork rows. More than 1,000 listed PRs or duplicate identities fail closed; discovery does not silently claim coverage beyond that ceiling. A separate authoritative snapshot pins each discovered PR's current head before queueing.

`fetch` initializes a fresh private repository, fetches the exact base/head SHA, and checks their commit identities. It constructs the Git URL from the authorized repository rather than trusting clone URLs supplied by PR content. It creates a fresh environment with no inherited credentials or Git configuration, disables hooks, credential helpers, URL redirects, file/SSH transports, submodules, fsmonitor, and automatic maintenance, and supplies an HTTPS header only to the fetch/push subprocess. Tokens never appear in command arguments or persistent remote configuration. Git output and execution time are bounded. Place fetch storage under a service disk quota: these process limits do not certify a maximum remote pack size.

`post_review` is disabled by default. It validates supported findings against current right-side diff anchors and writes one stable App-owned issue-comment summary with exact-commit file links. Marker ownership requires both Bot identity and the authenticated App ID; a lookalike marker from another actor is ignored. Identical content performs no write, changed content updates the owned comment, and duplicate owned markers require reconciliation. Failed writes are not blindly retried; the next invocation lists markers again. Queue per-PR exclusion is still required to prevent simultaneous comment creators. These are summaries with validated source links, not GitHub inline review-thread creation.

Model-authored text renders in dynamic-length CommonMark code spans. Embedded Markdown/HTML/images, arbitrary links, and mentions remain literal; source links are constructed by trusted code from validated commit/path/line identities. Tests parse the rendered structure to verify that only the intended source anchor becomes a link.

`checks` returns paginated runs/statuses for the exact requested SHA with their actual statuses and conclusions. Pending or missing conclusions remain pending or missing; this method does not turn CI observations into local verification or a merge decision.

`ci_review` additionally captures workflow runs, jobs, steps and bounded failure
evidence for the exact reviewed repository and head SHA. The dedicated read App
must grant **Actions: read**; existing installations may need their owner to
accept that permission before logs/jobs become accessible. Read workers request
this permission only in a purpose-specific CI token; push and comment tokens
retain their separate scopes. Inspection performs no rerun, cancel or workflow
mutation.

Known credential signatures are withheld before log excerpts enter review
context or comments. Qualified HTTPS job-log redirects use a fresh client with
no forwarded App authentication, cookies or environment proxy settings. Logs
use a validated bounded tail range when the qualified download service supports
it; the evidence and dropdown label that scope and truncation. Oversized full
responses are not silently treated as complete logs. Logs
from unsupported hosts, missing/expired logs, rejected pagination and exhausted
budgets remain explicitly unavailable or partial. Diagnosis distinguishes
observed errors from likely causes and suggests investigation actions without
executing them. Pattern filtering is not complete secret detection or proof of
root cause.

The pipeline records CI evidence alongside its findings and supplies a compact
CI context to the model. Material CI changes invalidate cached generations; an
observation timestamp alone does not. When `post_reviews` is enabled, a report is
queued even with zero supported findings. `post_review` checks the CI head
against the review snapshot and appends an escaped
`<details><summary>CI status and failures</summary>` section with per-job status,
failure evidence, next actions and coverage limits. The existing publisher queue,
lease guard, stale-head recheck and App-owned comment reconciliation still apply.

GitHub CI can change after the observation without a commit change. The comment
states when evidence was collected; it is a review-time snapshot, not a live
dashboard or certification that every required check executed.

[Synthetic dropdown example](examples/review-ci-summary.md) shows the emitted
review section using mocked CI results.

## Approval and push

`sign_approval` is a pure helper used only after API authentication/RBAC. The actor must come from authenticated server state. Its HMAC covers candidate identity, evidence digest, policy digest, expected head, tested base, actor, nonce, and expiry. Expiry is at most 24 hours. The publisher independently checks signature, expiry, current approver membership, tenant/repository/installation scope, policy, checks, container-verification provenance, durable evidence and log digests, current PR identity, and fork/default/base-branch restrictions. Local fixture verification cannot publish.

The worker workspace is never inspected for publication. The publisher fetches the pinned source into a new repository and reconstructs the candidate from its frozen patch with canonical metadata. Candidate ID, tree, commit, and single parent must match the approved object. Approval and PR eligibility are rechecked at the write boundary.

One explicit refspec is sent with `--atomic` and `--force-with-lease=refs/heads/<branch>:<expected-sha>`. The candidate is already proven to be a direct child of that expected SHA. Git's expected-value ref transaction rejects a competing head update. No non-force REST ref update is treated as compare-and-swap. The publisher reads remote refs and GitHub's commit/tree/parent object after the write. A lost success response is reconciled by that readback, with no duplicate push. A readback mismatch fails with an uncertain-outcome error.

An optional trusted queue guard is checked before reconstruction, after reconstruction, and immediately before the actual push after PR/ref reads and token minting. The last callback also revalidates approval expiry and identity. The coordinator supplies lease/epoch and durable-approval revocation checks through that guard; a model cannot supply or disable it.

`GitHubTransientError` distinguishes HTTP transport failures, HTTP 429, and HTTP 5xx from policy/validation failures and other HTTP 4xx. `PublishTransientError` preserves a retryable uncertain readback outcome. Queue retries remain bounded and always repeat authoritative reconciliation; an already-written exact candidate is read back without another push. Approval expiry, revocation, stale head/base, or malformed object identities still block publication.

If a Git push fails and authoritative readback proves both tested refs stayed unchanged, a bounded retry is allowed. Git subprocess stderr cannot reliably distinguish network interruption from server protection rejection, so protection rejection may consume bounded retries; it never becomes success or bypasses server rules. A moved base or foreign head is never retried as that unchanged-state case.

The independently mutable base branch cannot be atomically locked by updating only the PR head. Base is checked before and after push; a detected concurrent base advance returns `published_stale_base` and `requires_reverification=true`. PR closure/draft changes can also race an API read and Git ref transaction. No merges, auto-merge mutation, branch-protection bypass, or automatic rollback is implemented. Branch protections remain server-enforced.

## Model-bound context

`ContextIndex` applies a separate read policy (`context_paths`, defaulting to `src/`, `tests/`, `docs/`, and explicit `context_exclusions`). Credential filenames and recognized private-key/token/literal-secret signatures are withheld even when a read path permits them. `report` records graph coverage, counts, and omissions without content or matched credential values. Python import/symbol parsing is static; other languages remain lexical.

`model_snapshot` applies the same file/patch exclusions and recursively withholds credential-bearing PR title/body/thread fields. Comment text is capped at 20 KB per field and snapshot text at 100 KB, prioritizing owner text; omissions are explicit. Raw immutable snapshot identity remains available for provenance. `retrieve_related` joins already indexed, explicitly authorized same-tenant/same-team repositories under one byte budget; it performs no implicit fetch. This conservative pattern filter can withhold harmless examples and cannot detect every secret. Missing owner feedback must be treated as incomplete acceptance evidence, not as permission to ignore it.

`ClaudeCLI` exposes `CLIENT_VERSION = "2.1.306"`. Before generation it performs only an isolated offline `--version` check with a 15-second/64-KB bound, then requires exact client compatibility. Both version checks and generation force `DISABLE_AUTOUPDATER=1` in their restricted subprocess environments, so these invocations cannot update the approved client automatically. Other Claude invocations or external installers can still change the executable; an unexpected version remains a hard failure. Generation preserves only `PATH`, `HOME`, `LANG`, `TMPDIR`, `USER`, and `LOGNAME` plus the forced updater setting; the account names support macOS Keychain lookup without forwarding GitHub, approval, provider, or loader secrets. This string check is not cryptographic executable provenance or proof of subscription/account qualification. Deployment image pinning and external client/account qualification remain required. Tests mock version and generation subprocess results; they never invoke a model or authenticate an account.

## Verification and qualification limits

Tests use mocked HTTP and temporary local bare Git repositories only. They exercise scoped JWTs/tokens, bounded pagination, nested comments, stale state, marker ownership/idempotency, clean reconstruction despite corrupt worker `.git`, HMAC tampering/expiry, evidence corruption, expected-head races, ambiguous responses, and remote object readback. They do not install an App, perform live GitHub writes, call models, or certify GitHub production availability, rate limits, enterprise compatibility, server rules, or multi-replica deployment isolation.

Primary API references checked on 2026-10-21:

- [REST API versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions)
- [App JWT requirements](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)
- [Single-repository installation tokens and permissions](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app)
- [REST pagination](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api)
- [PR files and server listing ceiling](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files)
- [Top-level PR review summaries](https://docs.github.com/en/rest/pulls/reviews#list-reviews-for-a-pull-request)
- [GraphQL PR and review-thread schema](https://docs.github.com/en/graphql/reference/pulls)
- [Issue comments on PRs](https://docs.github.com/en/rest/issues/comments#create-an-issue-comment)
- [Git expected-value leases and atomic pushes](https://git-scm.com/docs/git-push)
