# Durable state

`patchington.store.Store` uses SQLAlchemy Core and PostgreSQL/psycopg for production.
SQLite is a development backend with serialized transactions; SQLite tests do not
qualify replica concurrency or production capacity. Initialize the schema before
use and call `close()` when stopping a process. PostgreSQL bootstrap is protected
by a schema-specific transactional advisory lock, so simultaneous replica startup
does not race table creation. `initialize()` atomically initializes an empty,
dedicated schema with a supported v1 version marker and tables. Known v1 is
idempotent. Unknown/newer versions, an empty/multiple version marker, and
unversioned pre-existing tables are refused without migration or deletion.
`schema_version()` reports the supported installed version. A version marker is
not a certification against manual column/index drift. `initialize()` does not
migrate an older schema. Run database backups, retention, restore drills, and
reviewed schema migrations as deployment operations.

## Queue authority

`enqueue(tenant, repo, pr, delivery_id, payload, max_attempts=3, queue="review")`
commits a job, its receipt audit, and its outbox event in one transaction. The
unique `(tenant, delivery_id)` key deduplicates concurrent and later redelivery;
the original immutable receipt wins, including its payload, repository, PR, queue,
and retry budget. A duplicate does not add another audit entry. Delivery identity
is tenant-wide, so internal publish jobs require distinct delivery identifiers.

`claim(owner, lease_seconds=120, tenant_limits=None, queue="review")` returns a
frozen `Job` or `None`. Role queues require exact queue-name matching. Claims
across all queues share the same tenant quota and `(tenant, repo, pr)` exclusion.
The default limit is one live job per tenant; explicit limits must be positive.
All replicas must receive the same trusted quota configuration. These arguments
are scheduler policy, never webhook or model input.

PostgreSQL claims use `FOR UPDATE SKIP LOCKED` on candidate jobs and on a durable
tenant mutex row. Under explicitly configured `READ COMMITTED`, the subsequent
live-lease count sees earlier committed claims for that tenant. A busy tenant can
be skipped while other tenants progress. Ready work is considered in available
time and insertion sequence order; skipped locks and tenant/PR constraints can
change execution order. `None` means no claim was obtained in this transaction,
including transient contention, so workers must poll again with bounded delay.

Each claim increments both `attempts` and a monotonic, job-scoped `fence`. Worker
authority is the entire `(job_id, owner, fence)` tuple. `heartbeat`, `complete`,
`fail`, and `put_job_record` lock the job and compare its current owner, fence,
status, and lease expiration against fresh **database time**. An expired lease
cannot be extended or finished, even before another worker reclaims it. Reclaim
by the same owner still increments the fence. Heartbeats never shorten a lease.
Fences protect database mutations; downstream external side effects still need
their own exact-candidate approval, stale-head checks, and atomic publication
controls. A lease is not approval to push code.

States are `queued`, `running`, `completed`, and `dead`. Explicit retryable
failures return to `queued` after `min(300, 2**(attempts-1))` seconds. Nonretryable
failures and exhausted budgets become `dead`. Expired running jobs may be
reclaimed; an expired final attempt becomes `dead` when a worker polls that role
queue. Polling is the reaper; there is no hidden background process. A dead job
does not retry itself or block another delivery for the same PR. The original
delivery remains deduplicated, so operator retries need a new delivery identifier
and retain the prior audit history.

## Records, audit, and outbox

Every public record, job read, audit read, and outbox read requires a tenant. The
queue scheduler is trusted internal code and can claim across tenants. This is
application-enforced tenant isolation, not PostgreSQL row-level security. Keep
database credentials restricted to service processes and enforce tenant RBAC
before invoking the store. `Job` fields are frozen; returned JSON values are
detached copies, so changing a returned dict does not modify persisted state.

- `put_record(tenant, kind, key, value)` is an administrative write. It is not
  protected by a worker lease.
- `put_job_record(job_id, owner, fence, kind, key, value)` derives the tenant from
  the job and atomically refuses stale worker writes, returning `False`.
- Both methods make kinds `candidate`, `evidence`, and `verification` immutable:
  first content wins, identical repeated content is accepted, and different
  content at the same tenant/kind/key raises `ValueError` with no partial write.
  Other record kinds support replacement. Frozen evidence must use these kinds.
- `audit(tenant, actor, action, resource, details)` atomically appends an audit
  event and corresponding outbox event. Job lifecycle and record writes use the
  same transaction as their audit/outbox additions. Heartbeats deliberately do
  not append events.
- `events(tenant, limit=100)` reads audit events in insertion order.
- `outbox(tenant, limit=100)` reads unacknowledged event objects in insertion
  order; `ack_outbox(tenant, event_id)` acknowledges exactly one matching pending
  event and returns whether the acknowledgment changed state. Audit survives
  acknowledgment. Consumers deliver first and acknowledge afterward, and must
  deduplicate by event `id`: outbox delivery is **at least once**, with no claim
  of exactly-once delivery or a built-in remote dispatcher.

JSON objects are copied/validated before writes. SQLAlchemy suppresses bound
parameters in database errors. Callers must still avoid putting credentials in
payloads, audit details, or exception messages; stored errors are bounded to
8 KiB. No queue or audit state lives only in process memory.

## Verification and limits

Run development behavior checks with `uv run pytest tests/test_store.py -q`.
Set `PATCHINGTON_TEST_DATABASE_URL` to a disposable actual PostgreSQL database
whose role can create schemas, then run
`uv run pytest tests/test_store.py tests/test_store_postgres.py -q`.
The PostgreSQL tests create and drop separate random schemas and never substitute
SQLite. Without the environment variable they explicitly skip.

Tests exercise concurrent duplicate receipt, concurrent per-PR and tenant quota
claims from independent engines, progress past a locked tenant, actual database
clock expiry, stale owner/fence rejection, heartbeat extension, reclaim, retry
budgets, durable audit/outbox/records, fenced worker state, concurrent immutable
record writes, and concurrent empty-schema bootstrap. SQLite additionally checks
receipt/audit rollback on a simulated audit write failure. These are bounded
local correctness tests. They do not establish production throughput, fairness,
database failover behavior, backup recovery, cross-region safety, or model quality.
