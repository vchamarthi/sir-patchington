# Security model

PR code, descriptions, review comments, model output, and worker workspaces are
untrusted inputs. Models cannot run tools, choose commands/images, grant roles,
change trusted requirements, access App keys, sign approvals, or merge PRs.

Authenticated principals are tenant/team scoped. Original approval tokens must
be strong random secrets; configuration stores only SHA-256 digests. Place the
API behind authenticated TLS routing. The HMAC webhook and approval secrets are
different secrets, at least 32 bytes each. The API and publisher share only the
approval signing/verification secret. Rotate secrets using the runbook; rotation
invalidates outstanding approvals.

PostgreSQL and the artifact store are trusted control-plane infrastructure.
Application-level scoping is not database row-level security. A host/database
administrator can change state. Hashes detect artifact changes; they do not make
a compromised host trustworthy. Protect, back up and restrict both stores.

The API can enqueue work and issue exact approvals according to server-owned
RBAC. Read workers can fetch enrolled source and use the explicitly enabled
subscription model adapter. Publisher credentials can write enrolled PR branches;
keep that role separate, install its App only on enrolled repositories, and
retain server branch protections. Candidate publication is disabled by default.
The source worker's filesystem and model account must never be mounted inside
execution containers. Source secret-pattern filtering is incomplete DLP; keep
credentials out of repositories and constrain context paths.

Execution containers run as nonroot, without network, capabilities, inherited
environment, credentials or Docker sockets; their source/root filesystems are
read-only and resources/time/output are bounded. Docker still shares a kernel.
Use a dedicated disposable execution VM/rootless daemon with client-authenticated
TLS. A Docker-client certificate is powerful execution authority. Do not connect
workers to a personal or production-workload daemon.

Owner-controlled checks must be provisioned outside the candidate. Required
regression checks must fail on the original head and pass on the candidate.
Use independent processes/services for acceptance assertions when candidate code
could interfere with its test harness. Passing finite tests never proves the
absence of bugs or malicious behavior.

Publication reconstructs the exact approved patch and commit in a fresh fetched
repository. Expected-head Git leases reject competing updates, and readback
verifies parent/tree/commit. Durable approval revocation is checked immediately
before the effect. A revocation arriving after the final check cannot retract a
Git transaction already in progress; rejection is not a remote rollback. Base
branch advancement and PR state changes cannot be locked atomically with a PR
head update, so detected base drift queues reverification and prevents a clean
CI-complete outcome. No merge or automatic rollback is implemented.

Report vulnerabilities privately to the repository owner. Do not include real
source credentials, customer data or private authentication material in issues.
