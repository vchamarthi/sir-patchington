"""Behavioral queue tests against development SQLite storage."""

import importlib
import time
from dataclasses import FrozenInstanceError

import pytest


@pytest.fixture
def store(tmp_path):
    module = importlib.import_module("patchington.store")
    state = module.Store(f"sqlite:///{tmp_path / 'state.db'}")
    state.initialize()
    yield state
    state.close()


def enqueue(store, tenant="alpha", pr=1, delivery="delivery", **kwargs):
    return store.enqueue(tenant, "org/repo", pr, delivery, {"head": "abc"}, **kwargs)


def expire(store, job_id):
    # Advance persisted lease state directly, avoiding long wall-clock tests.
    from sqlalchemy import update

    from patchington.store import jobs

    with store.engine.begin() as connection:
        connection.execute(update(jobs).where(jobs.c.id == job_id).values(lease_expires_at=0))


def make_ready(store, job_id):
    from sqlalchemy import update

    from patchington.store import jobs

    with store.engine.begin() as connection:
        connection.execute(update(jobs).where(jobs.c.id == job_id).values(available_at=0))


def test_receipt_is_deduplicated_tenant_scoped_and_audited(store):
    job_id = enqueue(store)
    assert enqueue(store) == job_id
    assert enqueue(store, tenant="beta") != job_id
    assert store.get(job_id, "beta") is None
    assert [job.id for job in store.list_jobs("alpha")] == [job_id]
    assert store.get(job_id, "alpha").payload == {"head": "abc"}
    assert [event["action"] for event in store.events("alpha")] == ["job.enqueued"]


def test_job_is_immutable_and_returned_payload_cannot_mutate_storage(store):
    job_id = enqueue(store)
    job = store.get(job_id, "alpha")
    with pytest.raises(FrozenInstanceError):
        job.status = "completed"
    job.payload["head"] = "mutated"
    assert store.get(job_id, "alpha").payload["head"] == "abc"


def test_claim_excludes_same_pr_and_enforces_tenant_limit(store):
    first = enqueue(store)
    enqueue(store, delivery="second")
    enqueue(store, pr=2, delivery="third")
    other = enqueue(store, tenant="beta")
    claimed = store.claim("worker", tenant_limits={"alpha": 1, "beta": 1})
    assert claimed.id == first
    assert claimed.status == "running" and claimed.attempts == 1 and claimed.fence == 1
    assert store.claim("other", tenant_limits={"alpha": 1, "beta": 1}).id == other
    assert store.claim("third", tenant_limits={"alpha": 1, "beta": 1}) is None
    assert store.complete(first, "worker", claimed.fence, {"reviewed": True})
    assert store.claim("worker", tenant_limits={"alpha": 1}).pr == 1


def test_per_pr_exclusion_holds_with_spare_tenant_capacity(store):
    first = enqueue(store)
    enqueue(store, delivery="second")
    another_pr = enqueue(store, pr=2, delivery="third")
    assert store.claim("first", tenant_limits={"alpha": 10}).id == first
    assert store.claim("second", tenant_limits={"alpha": 10}).id == another_pr
    assert store.claim("third", tenant_limits={"alpha": 10}) is None


def test_reclaim_increments_fence_and_rejects_old_owner(store):
    job_id = enqueue(store)
    original = store.claim("old")
    expire(store, job_id)
    assert not store.heartbeat(job_id, "old", original.fence)
    assert not store.complete(job_id, "old", original.fence, {})
    assert not store.fail(job_id, "old", original.fence, "expired")
    current = store.claim("new")
    assert current.id == job_id
    assert current.fence > original.fence and current.attempts == 2
    assert not store.complete(job_id, "old", original.fence, {})
    assert not store.heartbeat(job_id, "new", original.fence)
    assert store.heartbeat(job_id, "new", current.fence)
    assert store.complete(job_id, "new", current.fence, {"ok": True})
    assert not store.fail(job_id, "new", current.fence, "after complete")
    assert store.get(job_id, "alpha").result == {"ok": True}


def test_failure_backoff_and_attempt_budget(store):
    job_id = enqueue(store, max_attempts=2)
    first = store.claim("worker")
    assert store.fail(job_id, "worker", first.fence, "transient")
    queued = store.get(job_id, "alpha")
    assert queued.status == "queued" and queued.available_at > time.time()
    assert store.claim("worker") is None
    make_ready(store, job_id)
    second = store.claim("worker")
    assert second.attempts == 2 and second.fence == 2
    assert store.fail(job_id, "worker", second.fence, "again")
    assert store.get(job_id, "alpha").status == "dead"
    assert store.claim("worker") is None


def test_expired_last_attempt_is_dead_and_releases_pr(store):
    job_id = enqueue(store, max_attempts=1)
    next_id = enqueue(store, delivery="next")
    store.claim("crashed")
    expire(store, job_id)
    assert store.claim("replacement").id == next_id
    assert store.get(job_id, "alpha").status == "dead"


def test_nonretryable_failure_is_terminal(store):
    job_id = enqueue(store)
    job = store.claim("worker")
    assert store.fail(job_id, "worker", job.fence, "invalid", retry=False)
    assert store.get(job_id, "alpha").status == "dead"


def test_state_survives_reopening_and_records_are_tenant_scoped(store):
    module = importlib.import_module("patchington.store")
    job_id = enqueue(store)
    store.put_record("alpha", "context", "one", {"tree": "a"})
    store.put_record("beta", "context", "one", {"tree": "b"})
    store.put_record("alpha", "context", "one", {"tree": "new"})
    reopened = module.Store(str(store.engine.url))
    reopened.initialize()
    try:
        assert reopened.get(job_id, "alpha") is not None
        assert reopened.get_record("alpha", "context", "one") == {"tree": "new"}
        assert reopened.get_record("beta", "context", "one") == {"tree": "b"}
        assert reopened.get_record("missing", "context", "one") is None
    finally:
        reopened.close()


def test_audit_and_outbox_are_durable_and_tenant_scoped(store):
    enqueue(store)
    store.audit("alpha", "user:1", "approval.signed", "candidate:1", {"digest": "abc"})
    assert store.events("beta") == []
    assert store.events("alpha")[-1]["actor"] == "user:1"
    pending = store.outbox("alpha")
    assert [item["action"] for item in pending] == ["job.enqueued", "approval.signed"]
    assert not store.ack_outbox("beta", pending[0]["id"])
    assert store.ack_outbox("alpha", pending[0]["id"])
    assert not store.ack_outbox("alpha", pending[0]["id"])
    assert len(store.outbox("alpha")) == 1


def test_receipt_rolls_back_when_initial_audit_cannot_persist(store):
    from sqlalchemy import event, func, select

    from patchington.store import jobs

    def reject_audit(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO audit_events"):
            raise RuntimeError("audit unavailable")

    event.listen(store.engine, "before_cursor_execute", reject_audit)
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            enqueue(store)
    finally:
        event.remove(store.engine, "before_cursor_execute", reject_audit)
    with store.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(jobs)) == 0


@pytest.mark.parametrize("kwargs", [{"tenant": ""}, {"pr": 0}, {"max_attempts": 0}])
def test_invalid_receipt_is_rejected(store, kwargs):
    with pytest.raises(ValueError):
        enqueue(store, **kwargs)


def test_invalid_claim_options_are_rejected(store):
    with pytest.raises(ValueError):
        store.claim("")
    with pytest.raises(ValueError):
        store.claim("worker", lease_seconds=0)
    with pytest.raises(ValueError):
        store.claim("worker", tenant_limits={"alpha": 0})


def test_role_queues_are_separate_but_share_pr_exclusion(store):
    review = enqueue(store)
    publish = store.enqueue("alpha", "org/repo", 1, "publish", {}, queue="publish")
    claimed = store.claim("reviewer", tenant_limits={"alpha": 10})
    assert claimed.id == review and claimed.queue == "review"
    assert store.claim("reviewer", tenant_limits={"alpha": 10}) is None
    assert store.claim("publisher", queue="publish", tenant_limits={"alpha": 10}) is None
    assert store.complete(review, "reviewer", claimed.fence, {})
    assert store.claim("publisher", queue="publish").id == publish


def test_invalid_role_queue_rejected(store):
    with pytest.raises(ValueError):
        store.enqueue("alpha", "org/repo", 1, "bad", {}, queue="")
    with pytest.raises(ValueError):
        store.claim("worker", queue="")


def test_worker_records_are_lease_fenced_and_derive_tenant_from_job(store):
    job_id = enqueue(store)
    first = store.claim("worker")
    assert store.put_job_record(job_id, "worker", first.fence, "context", "one", {"v": 1})
    assert store.get_record("alpha", "context", "one") == {"v": 1}
    assert store.get_record("beta", "context", "one") is None
    expire(store, job_id)
    assert not store.put_job_record(job_id, "worker", first.fence, "context", "one", {"v": 2})
    second = store.claim("worker")
    assert not store.put_job_record(job_id, "worker", first.fence, "context", "one", {"v": 2})
    assert store.put_job_record(job_id, "worker", second.fence, "context", "one", {"v": 3})
    assert store.get_record("alpha", "context", "one") == {"v": 3}


@pytest.mark.parametrize("kind", ["candidate", "evidence", "verification"])
def test_frozen_evidence_records_reject_different_content(store, kind):
    job_id = enqueue(store)
    job = store.claim("worker")
    assert store.put_job_record(job_id, job.owner, job.fence, kind, "one", {"v": 1})
    assert store.put_job_record(job_id, job.owner, job.fence, kind, "one", {"v": 1})
    with pytest.raises(ValueError, match="immutable"):
        store.put_job_record(job_id, job.owner, job.fence, kind, "one", {"v": 2})
    with pytest.raises(ValueError, match="immutable"):
        store.put_record("alpha", kind, "one", {"v": 2})
    assert store.get_record("alpha", kind, "one") == {"v": 1}


def test_schema_version_is_reported_and_initialization_is_idempotent(store):
    assert store.schema_version() == 1
    job_id = enqueue(store)
    store.initialize()
    assert store.schema_version() == 1
    assert store.get(job_id, "alpha") is not None


def test_unknown_schema_version_is_refused_without_touching_state(store):
    from sqlalchemy import update

    from patchington.store import schema_versions

    job_id = enqueue(store)
    with store.engine.begin() as connection:
        connection.execute(update(schema_versions).values(version=2))
    with pytest.raises(ValueError, match="unsupported schema version"):
        store.initialize()
    assert store.get(job_id, "alpha") is not None


def test_unversioned_existing_state_is_refused(store):
    from patchington.store import schema_versions

    with store.engine.begin() as connection:
        schema_versions.drop(connection)
    with pytest.raises(ValueError, match="unversioned"):
        store.initialize()


def test_empty_schema_version_marker_is_refused(store):
    from patchington.store import schema_versions

    with store.engine.begin() as connection:
        connection.execute(schema_versions.delete())
    with pytest.raises(ValueError, match="unsupported schema version"):
        store.initialize()
