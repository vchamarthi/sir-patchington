# Operations

These files package the service. They do not enroll a repository, provision
credentials, publish an image, create a cluster, or start unattended model work.
The checked-in production configuration deliberately has no repository/principal
enrollment and fails production startup. Replace it with reviewed owner-controlled
configuration before starting services. Publishing and GitHub review comments
remain disabled in repository policies until explicitly enabled.

## Authorities and prerequisites

| Process | Credentials | Network / execution authority |
| --- | --- | --- |
| API (`serve`) | Database, webhook secret, approval signing secret | PostgreSQL; private API behind authenticated TLS ingress |
| Review/repair worker (`worker`) | Database, read App, approved Claude subscription authentication | GitHub/Claude HTTPS; dedicated Docker daemon over client-authenticated TLS |
| Publisher (`publisher`) | Database, read App, separate write App, approval verification secret | GitHub HTTPS; no model authentication or Docker daemon |
| Schema bootstrap (`migrate`) | Database only | PostgreSQL; exits after schema initialization |

Use distinct GitHub Apps for read access and publication. A repository's
`installation_id` is the read App installation that signs incoming webhook
identity. `write_installation_id` is the **different write App installation** used
by the publisher. Obtain each from GitHub; never infer equality. Restrict both
Apps to enrolled repositories and minimum permissions. The API receives neither
App key. Workers receive no write App or approval secret. The publisher receives
no model authentication. `post_reviews` also requires write authority and its
write-installation mapping. Configure `required_github_checks` with owner-chosen
CI check names; an empty list leaves CI outcome explicitly unknown.
Pin `required_github_check_app_ids` to the actual trusted CI producer Apps when
requiring those checks; matching a check name alone does not establish provenance.

The API and publisher must receive identical approval-secret bytes. Generate
independent webhook and approval secrets of at least 32 bytes. Principal
configuration contains only SHA-256 digests of strong random private bearer
tokens, plus tenant/team/role scopes. Protect the original tokens separately.
Do not place secrets in repository configuration, model prompts, audit details,
source trees, image layers, or CI variables. `.env.example` contains paths and
safe defaults; real `.env` and `.local/` content are ignored by Git and excluded
from the image build context.

The image currently targets **Linux amd64**. Python runtime dependencies come
from `uv.lock` with frozen synchronization; source is imported directly, avoiding
an unpinned package-build backend. Official Python, uv, and Node images are
digest pinned. Git and Docker CLI come from Debian signed package repositories;
their exact package versions are resolved during build, so whole-image rebuilds
are not claimed to be bit-for-bit reproducible. Pin, scan, and retain the final
published service image digest for deployment and rollback.

## Configure and build

Copy `deploy/config.production.toml` into a private configuration file. Add real
`[[repositories]]` and `[[principals]]` entries. Requirements and explicit check
argv must be owner-controlled. Every enrolled repository needs actual checks and
requirements. Keep `publish_enabled = false` and `post_reviews = false` during
qualification. Set allowed/denied paths, approver identities, retry budgets and
required GitHub CI checks deliberately. Production refuses SQLite, fixture model
execution, unpinned execution images, missing enrollment, missing checks, short
secrets, and write App credentials in API/worker processes.

Provision a **separate trusted check image** with dependencies and independent
owner-owned regression assertions already installed. Record its actual image
digest in `execution_image`. The checked-in Python image is a minimal boundary
fixture; it does not contain your project's dependencies or `/trusted/check_change.py`.
Never let the candidate or model choose the image, install dependencies, edit
acceptance commands, or supply publication credentials. Required regression
checks must fail on the original head and pass on the exact candidate.

Build without credentials or model calls:

```sh
docker build --platform linux/amd64 -t sir-patchington:local .
docker run --rm --network none --read-only --cap-drop ALL sir-patchington:local --help
PATCHINGTON_TEST_SERVICE_IMAGE=sir-patchington:local uv run pytest tests/test_deployment.py -q
```

Claude Code is installed from the official npm package at exact version
`2.1.306`, with npm integrity metadata in `deploy/claude/package-lock.json`.
Its published engine requirement is Node >=22; the image uses a pinned official
Node 22 binary rather than Debian's older Node 20. Package installation,
`--version`, and CLI help checks do not authenticate or generate model output.
Build-time npm uses `--jitless` for compatibility with amd64 emulation; the
runtime environment is unmodified. Only the pinned official postinstall is
invoked explicitly after scripts-disabled npm installation, to link its
integrity-checked native binary; the image build verifies `claude --version`.
Runtime model generation requires both worker role and explicit
`PATCHINGTON_ALLOW_LIVE_MODEL=1`. No provider API fallback is configured. An
operator must separately qualify subscription authentication, model identity,
quota, and supported CLI flags after any client upgrade.

## Dedicated execution daemon

Workers coordinate checks through a **dedicated rootless Docker daemon**, preferably
on a separate disposable execution VM with no service, GitHub, cloud or model
credentials. Configure server and client certificates, server-name verification,
and client-authenticated TLS on port 2376. Firewall that port to authorized worker
hosts/pods only. Never expose plaintext port 2375. A daemon client certificate is
powerful execution authority even when the daemon is rootless: do not reuse a
developer workstation daemon or a daemon running production workloads.

Only the worker receives `DOCKER_HOST`, `DOCKER_TLS_VERIFY=1`, and
`DOCKER_CERT_PATH`. Provision `ca.pem`, `cert.pem`, and `key.pem` as worker-only
secrets. Do not mount a host Docker socket into API, worker, publisher, or check
containers. Do not mount the user's home directory. The verifier stages source
with `docker cp` into daemon-managed volumes, so remote execution does not need
host bind paths. It mounts no service configuration, credentials or artifacts
into test containers. Pre-provision the exact trusted execution image on that
daemon; checks use `--pull=never` and fail closed if it is unavailable.

Docker's kernel boundary is not a VM isolation claim. Recreate execution VMs on
suspected compromise, cap host/container resources, and monitor orphaned
`patchington-*` containers/volumes if a worker is killed before cleanup. Do not
remove unrelated daemon resources. Source import may still manipulate a
same-process test harness; high-assurance acceptance needs owner-controlled
assertions outside the untrusted program process.

## Compose on one host

Provision `.env` from `.env.example` and private secret files under an owner-only
directory, for example `.local/secrets/` with mode 0700. Local Compose secrets are
bind mounts rather than encrypted secret storage; ensure the container UID 10001
can read the selected files while their host parent remains private. Never use
`docker compose config` without `--quiet` in shared logs: resolved environment
and paths can expose deployment details. Key-file contents are loaded into the
specific role process by `deploy/entrypoint.py`, with no logging of their values.

Store the database DSN in its secret file:
`postgresql+psycopg://patchington:<URL-escaped-password>@postgres:5432/patchington`.
The password must match the PostgreSQL password file. PostgreSQL has no published
host port and belongs only to the internal state network. The API binds the
host loopback interface at port 8000; add an operator-controlled TLS reverse
proxy for remote access. Compose networks do not enforce destination allowlists;
use host firewall/egress controls to limit worker access to the dedicated daemon
and GitHub/Claude endpoints, and publisher access to GitHub.

After enrollment and secrets are reviewed:

```sh
docker compose config --quiet
docker compose build
docker compose up -d postgres
docker compose --profile admin run --rm migrate
docker compose run --rm api doctor --role serve --config /etc/patchington/config.toml
docker compose up -d api
```

The database and shared artifact named volumes survive container replacement.
Back up both. `migrate` currently bootstraps schema v1; it does not silently
upgrade an older schema. A supported v1 marker is required on existing state;
unknown/newer markers and unversioned pre-existing tables are refused. Use an
empty dedicated database/schema for first initialization, or a reviewed future
migration for legacy state. Never stamp a version onto unknown tables merely to
bypass this check. Do not use `down --volumes` as a restart operation.

Workers and publishers are opt-in profiles. Enable workers only after approving
their authentication and live model execution, then set
`PATCHINGTON_ALLOW_LIVE_MODEL=1` and start the `workers` profile. Worker-only
subscription authentication is copied from the selected private credential file
into a writable private container home. Refreshes remain local to that container;
provision fresh source credentials when required. Never mount all of host HOME
or inject provider API keys. Start the `publisher` profile only after exact-candidate
approval and a reviewed publication policy; possessing a write key alone does
not bypass those gates. Starting more worker replicas does not raise per-tenant
concurrency; every replica must use identical trusted quota configuration.
Continuous workers also reconcile enrolled open PRs at startup and every
`reconcile_interval_seconds` (default 300); authoritative snapshot/policy receipts
deduplicate replica discoveries. `--once` leaves reconciliation explicit through
the `reconcile` CLI command. This is read App activity, not model generation or
publication authority.

## Kubernetes

`deploy/render.py` renders Kubernetes JSON; it never connects to a cluster.
Supply an actually published service-image digest, a reviewed configuration,
exact PostgreSQL/daemon host addresses and a storage class supporting shared
ReadWriteMany artifacts. For example, substitute your own verified values:

```sh
uv run python deploy/render.py \
  --image "$PATCHINGTON_PUBLISHED_IMAGE_DIGEST" \
  --config .local/config.production.toml \
  --docker-host tcp://10.42.0.10:2376 \
  --docker-daemon-cidr 10.42.0.10/32 \
  --postgres-cidr 10.42.0.20/32 \
  --artifact-storage-class your-rwx-class > .local/kubernetes.json
```

Those addresses are documentation examples, not a provisioned daemon/database.
The renderer rejects tag-only images, plaintext daemon ports, broad CIDRs, and
daemon addresses outside the supplied host allowlist. It also refuses empty
repository/principal enrollment and an unspecified artifact storage class. It validates syntax, not
registry contents, database certificates, daemon ownership, or actual isolation.
Workers and publishers render with **zero replicas** unless explicitly requested.
Nonzero workers require `--allow-live-model --read-app-id <id>`; publishers require
both `--read-app-id <id>` and `--write-app-id <id>`.

Before applying, create the namespace and these operator-managed Secrets in it:

| Secret | Required keys | Mounted roles |
| --- | --- | --- |
| `patchington-state` | `database-url` | API, worker, publisher, migrate |
| `patchington-intake` | `webhook-secret`, `approval-secret` | API |
| `patchington-read-app` | `app.pem` | worker, publisher |
| `patchington-write-app` | `app.pem` | publisher only |
| `patchington-approval` | `approval-secret` | publisher only; same bytes as intake |
| `patchington-model` | `credentials.json` | worker only |
| `patchington-docker-tls` | `ca.pem`, `cert.pem`, `key.pem` | worker only |

Use a secret manager or `kubectl create secret --from-file` from private local
files; do not put secret values in manifests or shell history. External production
PostgreSQL should use TLS with `sslmode=verify-full` and a trusted server CA.
Configure its own backups, availability and database access controls. Kubernetes
does not provision PostgreSQL or an execution daemon through these manifests.

Review the rendered artifact, then perform cluster-side dry-run/policy admission
and apply only as an operator-authorized deployment. Restricted Pod Security,
nonroot UID, dropped capabilities, read-only roots, disabled service-account
token mounts, resource requests/limits, namespace quota, termination grace,
health probes, API disruption budget and default-deny NetworkPolicies are
rendered. No host paths, host sockets, public ingress or embedded secrets exist.
API ingress allows only the configured ingress-controller namespace; establish
authenticated TLS routing separately. PostgreSQL and daemon connections have
exact host allowlists. Worker/publisher public HTTPS egress is IPv4 port 443,
excluding private ranges; use a domain-allowlisting egress gateway where required.
DNS policies assume kube-system DNS, so review CNI/NodeLocal DNS compatibility.
NetworkPolicies require a supporting CNI and must be tested with actual denied
connections. RWX storage behavior, secret access RBAC, quotas and available
amd64 node capacity also require cluster qualification. A valid render is not
proof that these controls are enforced.

## Observe, stop, recover

`/livez` proves only that the API responds. `/readyz` checks database reachability;
it does not qualify model authentication, GitHub installation scopes, a Docker
daemon, checks, worker progress, or publication. `/metrics` is authenticated.
Track queued age, live/expired leases, attempts/dead jobs, pending outbox events,
worker heartbeat loss, candidate verification failures and publication conflicts.
Read tenant-scoped audit events for the exact job/candidate and observed outcome.
Outbox consumers deliver at least once and deduplicate by event ID. External CI
observation and a remote branch readback are separate from local verification.
Tenant operators can read `GET /v1/outbox` and acknowledge a delivered event with
`POST /v1/outbox/{id}/ack`; no external notification sender is installed. Later CI
observation is bounded by repository `ci_poll_attempts` (default 30) with queue
backoff capped at 300 seconds. Empty required-check configuration, missing trusted
producer/SHA proof, or exhausted polling leaves the outcome unknown/pending.

For a model, credential, fence, tenant-isolation or publication incident, stop
worker/publisher processes first and disable policy publication. Preserve jobs,
audit, immutable artifacts and remote branch identity. Revoke affected App keys,
subscription sessions or daemon certificates as indicated. Do not delete jobs to
hide failures or mark pending CI green. A crashed worker's lease expires; polling
reclaims bounded remaining attempts or marks exhausted work dead. A new delivery
identity is required for an intentional operator retry of a dead delivery.

Use a 150-second termination grace with the default 120-second lease, and allow
in-flight work to finish or lose its lease cleanly. Database fences reject stale
workers; external publication additionally needs its approval and atomic Git
controls. On rollback, stop publishers, restore the previously qualified image
digest and matching configuration, verify schema compatibility, then qualify
workers before restoring them. Do not restore an old database snapshot over new
publication history without reconciliation against actual remote refs.

Before production rollout, prove restored PostgreSQL/artifacts from backups,
duplicate and out-of-order webhook behavior, process-kill reclaim, actual blocked
test-network/secret access, exact-candidate approval expiry, ambiguous publication
reconciliation and remote readback on a disposable private repository. None of
these files establishes production throughput or model repair quality. CI uses
offline fixtures, actual PostgreSQL and isolated local containers; no real model
generation, App credentials, release publishing or bot writes are enabled.

Pinned action commits were resolved on 2026-10-21 from GitHub's official API:
[checkout v4](https://api.github.com/repos/actions/checkout/git/ref/tags/v4),
[setup-python v5](https://api.github.com/repos/actions/setup-python/git/ref/tags/v5),
and [setup-uv v6 annotated tag](https://api.github.com/repos/astral-sh/setup-uv/git/tags/d0d8abe699bfb85fec6de9f7adb5ae17292296ff).
The workflow uses the dereferenced setup-uv commit. Claude package version,
Node engine requirement and integrity came from the
[official npm registry metadata](https://registry.npmjs.org/@anthropic-ai/claude-code/2.1.306).
