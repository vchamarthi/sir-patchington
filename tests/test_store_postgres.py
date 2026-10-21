"""Real PostgreSQL concurrency tests, isolated in disposable per-test schemas.

Set PATCHINGTON_TEST_DATABASE_URL to a PostgreSQL database whose role may create
schemas. Absence skips explicitly; these tests never silently substitute SQLite.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url

from patchington.store import Store, audit_events, jobs, metadata, tenants

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_store():
    configured = os.environ.get("PATCHINGTON_TEST_DATABASE_URL")
    if not configured:
        pytest.skip("PATCHINGTON_TEST_DATABASE_URL is required for real PostgreSQL tests")
    url = make_url(configured)
    if url.get_backend_name() != "postgresql":
        pytest.fail("PostgreSQL integration tests require an actual PostgreSQL DSN")
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    schema = "test_queue_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_url = url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
        hide_password=False,
    )
    store = None
    try:
        store = Store(scoped_url)
        store.initialize()
        yield store, scoped_url
    finally:
        if store:
            store.close()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def parallel(url, count, operation):
    barrier = threading.Barrier(count)

    def run(index):
        replica = Store(url)
        try:
            barrier.wait(timeout=10)
            return operation(replica, index)
        finally:
            replica.close()

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(run, range(count)))


def enqueue(store, tenant="alpha", pr=1, delivery="delivery", **kwargs):
    return store.enqueue(tenant, "org/repo", pr, delivery, {"head": "abc"}, **kwargs)


def test_pg_duplicate_delivery_race_has_one_receipt_and_audit(pg_store):
    store, url = pg_store
    ids = parallel(url, 8, lambda replica, _: enqueue(replica))
    assert len(set(ids)) == 1
    with store.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(jobs)) == 1
        assert connection.scalar(select(func.count()).select_from(audit_events)) == 1


def test_pg_same_pr_claim_race_has_one_live_lease(pg_store):
    store, url = pg_store
    for index in range(12):
        enqueue(store, delivery=f"event-{index}")
    claimed = parallel(
        url,
        8,
        lambda replica, index: replica.claim(
            f"worker-{index}",
            tenant_limits={"alpha": 20},
        ),
    )
    active = [job for job in claimed if job]
    assert len(active) == 1
    assert store.claim("extra", tenant_limits={"alpha": 20}) is None
    assert store.complete(active[0].id, active[0].owner, active[0].fence, {})
    assert store.claim("replacement", tenant_limits={"alpha": 20}) is not None


def test_pg_tenant_quota_survives_parallel_replicas(pg_store):
    store, url = pg_store
    for index in range(12):
        enqueue(store, pr=index + 1, delivery=f"event-{index}")
    claimed = parallel(
        url,
        8,
        lambda replica, index: replica.claim(
            f"worker-{index}",
            tenant_limits={"alpha": 3},
        ),
    )
    active = [job for job in claimed if job]
    assert 1 <= len(active) <= 3
    while (job := store.claim("fill-capacity", tenant_limits={"alpha": 3})) is not None:
        active.append(job)
    assert len(active) == 3
    assert len({job.id for job in active}) == 3
    with store.engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(jobs)
                .where(
                    jobs.c.status == "running",
                )
            )
            == 3
        )


def test_pg_busy_tenant_does_not_block_another_tenant(pg_store):
    store, _ = pg_store
    enqueue(store)
    expected = enqueue(store, tenant="beta")
    with store.engine.begin() as connection:
        connection.execute(select(tenants).where(tenants.c.tenant == "alpha").with_for_update())
        started = time.monotonic()
        claimed = store.claim("other-tenant")
        assert claimed.id == expected
        assert time.monotonic() - started < 2


def test_pg_real_db_clock_expiry_rejects_mutations_and_reclaim_fences(pg_store):
    store, url = pg_store
    job_id = enqueue(store)
    first = store.claim("same-owner", lease_seconds=1)
    with store.engine.connect() as connection:
        connection.execute(text("SELECT pg_sleep(1.05)"))
    replica = Store(url)
    try:
        assert not replica.heartbeat(job_id, first.owner, first.fence)
        assert not replica.complete(job_id, first.owner, first.fence, {})
        assert not replica.fail(job_id, first.owner, first.fence, "too late")
        reclaimed = replica.claim("same-owner")
        assert reclaimed.id == job_id and reclaimed.fence == first.fence + 1
        assert reclaimed.attempts == first.attempts + 1
        assert not store.complete(job_id, first.owner, first.fence, {"stale": True})
        assert replica.complete(job_id, reclaimed.owner, reclaimed.fence, {"current": True})
    finally:
        replica.close()


def test_pg_heartbeat_extends_real_lease_and_owner_cannot_shorten_it(pg_store):
    store, _ = pg_store
    job_id = enqueue(store)
    claimed = store.claim("worker", lease_seconds=1)
    assert store.heartbeat(job_id, "worker", claimed.fence, lease_seconds=3)
    extended = store.get(job_id, "alpha").lease_expires_at
    assert store.heartbeat(job_id, "worker", claimed.fence, lease_seconds=1)
    assert store.get(job_id, "alpha").lease_expires_at >= extended
    with store.engine.connect() as connection:
        connection.execute(text("SELECT pg_sleep(1.05)"))
    assert store.claim("replacement") is None
    assert store.complete(job_id, "worker", claimed.fence, {})


def test_pg_expired_final_attempt_is_dead_and_durable(pg_store):
    store, url = pg_store
    job_id = enqueue(store, max_attempts=1)
    store.claim("crashed", lease_seconds=1)
    with store.engine.connect() as connection:
        connection.execute(text("SELECT pg_sleep(1.05)"))
    assert store.claim("replacement") is None
    replica = Store(url)
    try:
        assert replica.get(job_id, "alpha").status == "dead"
        assert replica.events("alpha")[-1]["action"] == "job.dead"
        assert len(replica.outbox("alpha")) == 3
    finally:
        replica.close()


def test_pg_role_queues_keep_global_pr_exclusion(pg_store):
    store, _ = pg_store
    review_id = enqueue(store)
    publish_id = enqueue(store, delivery="publish", queue="publish")
    review = store.claim("reviewer", tenant_limits={"alpha": 4})
    assert review.id == review_id
    assert store.claim("publisher", queue="publish", tenant_limits={"alpha": 4}) is None
    assert store.complete(review.id, review.owner, review.fence, {})
    assert store.claim("publisher", queue="publish").id == publish_id


def test_pg_records_and_outbox_survive_replica_reopen_and_scope_tenant(pg_store):
    store, url = pg_store
    enqueue(store)
    store.put_record("alpha", "approval", "candidate", {"digest": "immutable"})
    replica = Store(url)
    try:
        assert replica.get_record("beta", "approval", "candidate") is None
        assert replica.get_record("alpha", "approval", "candidate") == {"digest": "immutable"}
        pending = replica.outbox("alpha")
        assert not replica.ack_outbox("beta", pending[0]["id"])
        assert replica.ack_outbox("alpha", pending[0]["id"])
        assert len(store.outbox("alpha")) == 1
    finally:
        replica.close()


def test_pg_retry_backoff_and_budget(pg_store):
    store, _ = pg_store
    job_id = enqueue(store, max_attempts=2)
    first = store.claim("worker")
    assert store.fail(job_id, first.owner, first.fence, "transient")
    assert store.claim("worker") is None
    with store.engine.begin() as connection:
        connection.execute(update(jobs).where(jobs.c.id == job_id).values(available_at=0))
    second = store.claim("worker")
    assert second.attempts == 2
    assert store.fail(job_id, second.owner, second.fence, "again")
    assert store.get(job_id, "alpha").status == "dead"


def test_pg_replica_schema_initialization_is_serialized(pg_store):
    store, url = pg_store
    with store.engine.begin() as connection:
        metadata.drop_all(connection)
    parallel(url, 8, lambda replica, _: replica.initialize())
    assert enqueue(store)


def test_pg_worker_state_fences_and_immutable_candidate_race(pg_store):
    store, url = pg_store
    job_id = enqueue(store)
    first = store.claim("worker", lease_seconds=1)
    assert store.put_job_record(job_id, first.owner, first.fence, "context", "one", {"v": 1})
    with store.engine.connect() as connection:
        connection.execute(text("SELECT pg_sleep(1.05)"))
    assert not store.put_job_record(job_id, first.owner, first.fence, "context", "one", {"v": 2})
    second = store.claim("worker")
    assert not store.put_job_record(job_id, first.owner, first.fence, "context", "one", {"v": 2})
    assert store.put_job_record(job_id, second.owner, second.fence, "context", "one", {"v": 3})

    def write_candidate(replica, index):
        try:
            replica.put_record("alpha", "candidate", "same-key", {"v": index})
            return True
        except ValueError as error:
            assert "immutable" in str(error)
            return False

    assert parallel(url, 8, write_candidate).count(True) == 1
    assert store.get_record("alpha", "candidate", "same-key")["v"] in range(8)


def test_pg_unsupported_version_is_refused_without_schema_migration(pg_store):
    from patchington.store import schema_versions

    store, _ = pg_store
    assert store.schema_version() == 1
    job_id = enqueue(store)
    with store.engine.begin() as connection:
        connection.execute(update(schema_versions).values(version=2))
    with pytest.raises(ValueError, match="unsupported schema version"):
        store.initialize()
    assert store.get(job_id, "alpha") is not None
