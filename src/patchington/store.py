"""Transactional state and leased work queue; PostgreSQL is the production backend.

All authority to finish work is a live (job, owner, fence) lease. A fence increases
on every claim, including reclaim by the same owner. Callers must carry it through
every stage and must not treat a successful heartbeat as publication authority.
"""

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    event,
    func,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import StaticPool

metadata = MetaData()
SCHEMA_VERSION = 1
schema_versions = Table(
    "patchington_schema_version",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("installed_at", Float, nullable=False),
)
tenants = Table("queue_tenants", metadata, Column("tenant", String(200), primary_key=True))
jobs = Table(
    "queue_jobs",
    metadata,
    Column("sequence", Integer, primary_key=True, autoincrement=True),
    Column("id", String(36), nullable=False, unique=True),
    Column("tenant", String(200), nullable=False),
    Column("repo", String(500), nullable=False),
    Column("pr", Integer, nullable=False),
    Column("delivery_id", String(200), nullable=False),
    Column("queue", String(100), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("status", String(20), nullable=False),
    Column("owner", String(200)),
    Column("fence", Integer, nullable=False, default=0),
    Column("attempts", Integer, nullable=False, default=0),
    Column("max_attempts", Integer, nullable=False),
    Column("result", JSON),
    Column("error", Text),
    Column("created_at", Float, nullable=False),
    Column("available_at", Float, nullable=False),
    Column("lease_expires_at", Float),
    UniqueConstraint("tenant", "delivery_id", name="uq_queue_delivery"),
    CheckConstraint("pr > 0 AND attempts >= 0 AND max_attempts > 0 AND fence >= 0"),
    CheckConstraint("status IN ('queued', 'running', 'completed', 'dead')"),
    CheckConstraint("status != 'running' OR (owner IS NOT NULL AND lease_expires_at IS NOT NULL)"),
)
Index("ix_queue_ready", jobs.c.queue, jobs.c.status, jobs.c.available_at, jobs.c.sequence)
Index("ix_queue_active_pr", jobs.c.tenant, jobs.c.repo, jobs.c.pr, jobs.c.status)
audit_events = Table(
    "audit_events",
    metadata,
    Column("sequence", Integer, primary_key=True, autoincrement=True),
    Column("id", String(36), nullable=False, unique=True),
    Column("tenant", String(200), nullable=False),
    Column("actor", String(200), nullable=False),
    Column("action", String(200), nullable=False),
    Column("resource", String(600), nullable=False),
    Column("details", JSON, nullable=False),
    Column("occurred_at", Float, nullable=False),
)
Index("ix_audit_tenant", audit_events.c.tenant, audit_events.c.sequence)
outbox_events = Table(
    "outbox_events",
    metadata,
    Column("sequence", Integer, primary_key=True, autoincrement=True),
    Column("id", String(36), nullable=False, unique=True),
    Column("tenant", String(200), nullable=False),
    Column("event", JSON, nullable=False),
    Column("acknowledged_at", Float),
)
Index("ix_outbox_tenant", outbox_events.c.tenant, outbox_events.c.acknowledged_at)
records = Table(
    "tenant_records",
    metadata,
    Column("tenant", String(200), primary_key=True),
    Column("kind", String(200), primary_key=True),
    Column("key", String(600), primary_key=True),
    Column("value", JSON, nullable=False),
    Column("updated_at", Float, nullable=False),
)


@dataclass(frozen=True)
class Job:
    id: str
    tenant: str
    repo: str
    pr: int
    queue: str
    payload: dict
    status: str
    owner: str | None
    fence: int
    attempts: int
    result: dict | None
    error: str | None
    max_attempts: int
    available_at: float
    lease_expires_at: float | None


def _string(value: str, name: str, maximum: int = 200) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be nonempty and at most {maximum} characters")


def _positive(value: int, name: str, maximum: int = 86400) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")


def _json(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("value must be a JSON object")
    # A detached JSON value prevents caller mutations and rejects NaN/non-JSON data.
    return json.loads(json.dumps(value, allow_nan=False))


class Store:
    def __init__(self, database_url: str):
        if database_url.startswith("postgres://"):
            database_url = "postgresql+psycopg://" + database_url[len("postgres://") :]
        elif database_url.startswith("postgresql://"):
            database_url = "postgresql+psycopg://" + database_url[len("postgresql://") :]
        options: dict[str, Any] = {"pool_pre_ping": True, "hide_parameters": True}
        if database_url.startswith("sqlite:"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if database_url in {"sqlite://", "sqlite:///:memory:"}:
                options["poolclass"] = StaticPool
        else:
            # Quota checks must observe claims committed after the tenant lock.
            options["isolation_level"] = "READ COMMITTED"
        self.engine = create_engine(database_url, **options)
        self._sqlite = self.engine.dialect.name == "sqlite"
        if not self._sqlite and self.engine.dialect.name != "postgresql":
            self.engine.dispose()
            raise ValueError("only PostgreSQL and development SQLite are supported")
        self._lock = threading.RLock()
        if self._sqlite:
            # SQLite has no row locks. Serialize writers across Store instances via
            # BEGIN IMMEDIATE, and shared-memory connections inside an instance.
            event.listen(self.engine, "connect", self._sqlite_connect)
            event.listen(self.engine, "begin", self._sqlite_begin)

    @staticmethod
    def _sqlite_connect(connection, _record):
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _sqlite_begin(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    @contextmanager
    def _transaction(self):
        if self._sqlite:
            with self._lock, self.engine.begin() as connection:
                yield connection
        else:
            with self.engine.begin() as connection:
                yield connection

    def initialize(self) -> None:
        with self._transaction() as connection:
            if not self._sqlite:
                # Serialize only schema bootstrap, including concurrent API startup.
                connection.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtext(func.current_schema()),
                            func.hashtext("patchington-schema-v1"),
                        )
                    )
                )
            existing = set(inspect(connection).get_table_names())
            if schema_versions.name in existing:
                self._schema_version(connection)
                metadata.create_all(connection)
                return
            if existing:
                raise ValueError("unversioned existing schema; reviewed migration is required")
            metadata.create_all(connection)
            connection.execute(
                schema_versions.insert().values(
                    version=SCHEMA_VERSION,
                    installed_at=self._now(connection),
                )
            )

    @staticmethod
    def _schema_version(connection) -> int:
        versions = list(connection.scalars(select(schema_versions.c.version)))
        if versions != [SCHEMA_VERSION]:
            raise ValueError("unsupported schema version; this binary supports only v1")
        return SCHEMA_VERSION

    def schema_version(self) -> int:
        with self._transaction() as connection:
            if not inspect(connection).has_table(schema_versions.name):
                raise ValueError("unversioned schema; initialize an empty schema first")
            return self._schema_version(connection)

    def close(self) -> None:
        self.engine.dispose()

    def _insert(self, table):
        return sqlite_insert(table) if self._sqlite else postgres_insert(table)

    def _now(self, connection) -> float:
        expression = (
            (func.julianday("now") - 2440587.5) * 86400.0
            if self._sqlite
            else func.extract("epoch", func.clock_timestamp())
        )
        return float(connection.scalar(select(expression)))

    @staticmethod
    def _job(row) -> Job:
        return Job(**{name: row[name] for name in Job.__dataclass_fields__})

    def _audit(self, connection, tenant, actor, action, resource, details, now) -> None:
        item = {
            "id": str(uuid4()),
            "tenant": tenant,
            "actor": actor,
            "action": action,
            "resource": resource,
            "details": details,
            "occurred_at": now,
        }
        connection.execute(audit_events.insert().values(**item))
        connection.execute(
            outbox_events.insert().values(
                id=item["id"],
                tenant=tenant,
                event=item,
            )
        )

    def enqueue(
        self,
        tenant: str,
        repo: str,
        pr: int,
        delivery_id: str,
        payload: dict,
        max_attempts: int = 3,
        *,
        queue: str = "review",
    ) -> str:
        _string(tenant, "tenant")
        _string(repo, "repo", 500)
        _positive(pr, "pr", 2**31 - 1)
        _string(delivery_id, "delivery_id")
        _string(queue, "queue", 100)
        _positive(max_attempts, "max_attempts", 100)
        payload = _json(payload)
        with self._transaction() as connection:
            connection.execute(self._insert(tenants).values(tenant=tenant).on_conflict_do_nothing())
            now = self._now(connection)
            job_id = str(uuid4())
            inserted = connection.execute(
                self._insert(jobs)
                .values(
                    id=job_id,
                    tenant=tenant,
                    repo=repo,
                    pr=pr,
                    delivery_id=delivery_id,
                    payload=payload,
                    queue=queue,
                    status="queued",
                    fence=0,
                    attempts=0,
                    max_attempts=max_attempts,
                    created_at=now,
                    available_at=now,
                )
                .on_conflict_do_nothing(index_elements=["tenant", "delivery_id"])
                .returning(jobs.c.id)
            )
            if inserted.scalar_one_or_none() is None:
                return connection.scalar(
                    select(jobs.c.id).where(
                        jobs.c.tenant == tenant,
                        jobs.c.delivery_id == delivery_id,
                    )
                )
            self._audit(
                connection,
                tenant,
                "queue",
                "job.enqueued",
                job_id,
                {"repo": repo, "pr": pr, "delivery_id": delivery_id},
                now,
            )
            return job_id

    def claim(
        self,
        owner: str,
        lease_seconds: int = 120,
        tenant_limits: dict[str, int] | None = None,
        *,
        queue: str = "review",
    ) -> Job | None:
        _string(owner, "owner")
        _string(queue, "queue", 100)
        _positive(lease_seconds, "lease_seconds")
        limits = dict(tenant_limits or {})
        for tenant, limit in limits.items():
            _string(tenant, "tenant")
            _positive(limit, "tenant limit", 10000)
        with self._transaction() as connection:
            excluded_jobs: list[str] = []
            excluded_tenants: list[str] = []
            while True:
                now = self._now(connection)
                eligible = or_(
                    and_(jobs.c.status == "queued", jobs.c.available_at <= now),
                    and_(jobs.c.status == "running", jobs.c.lease_expires_at <= now),
                )
                query = select(jobs).where(eligible, jobs.c.queue == queue)
                if excluded_jobs:
                    query = query.where(jobs.c.id.not_in(excluded_jobs))
                if excluded_tenants:
                    query = query.where(jobs.c.tenant.not_in(excluded_tenants))
                row = (
                    connection.execute(
                        query.order_by(
                            jobs.c.available_at,
                            jobs.c.sequence,
                        )
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    return None
                tenant = row["tenant"]
                # Every claim for this tenant must hold its durable mutex. Under
                # READ COMMITTED the subsequent count sees preceding committed claims.
                locked = connection.execute(
                    select(tenants.c.tenant)
                    .where(
                        tenants.c.tenant == tenant,
                    )
                    .with_for_update(skip_locked=True)
                ).scalar_one_or_none()
                if locked is None:
                    excluded_tenants.append(tenant)
                    continue
                now = self._now(connection)
                if row["attempts"] >= row["max_attempts"]:
                    connection.execute(
                        update(jobs)
                        .where(jobs.c.id == row["id"])
                        .values(
                            status="dead",
                            owner=None,
                            lease_expires_at=None,
                            error="lease expired after final attempt",
                        )
                    )
                    self._audit(
                        connection,
                        tenant,
                        "queue",
                        "job.dead",
                        row["id"],
                        {"reason": "attempts exhausted", "fence": row["fence"]},
                        now,
                    )
                    continue
                active = and_(
                    jobs.c.tenant == tenant,
                    jobs.c.status == "running",
                    jobs.c.lease_expires_at > now,
                )
                count = connection.scalar(select(func.count()).select_from(jobs).where(active))
                if count >= limits.get(tenant, 1):
                    excluded_tenants.append(tenant)
                    continue
                pr_busy = connection.scalar(
                    select(jobs.c.id)
                    .where(
                        active,
                        jobs.c.repo == row["repo"],
                        jobs.c.pr == row["pr"],
                    )
                    .limit(1)
                )
                if pr_busy:
                    excluded_jobs.append(row["id"])
                    continue
                fence = row["fence"] + 1
                claimed = (
                    connection.execute(
                        update(jobs)
                        .where(jobs.c.id == row["id"])
                        .values(
                            status="running",
                            owner=owner,
                            fence=fence,
                            attempts=row["attempts"] + 1,
                            lease_expires_at=now + lease_seconds,
                        )
                        .returning(jobs)
                    )
                    .mappings()
                    .one()
                )
                self._audit(
                    connection,
                    tenant,
                    owner,
                    "job.claimed",
                    row["id"],
                    {"fence": fence, "attempt": claimed["attempts"]},
                    now,
                )
                return self._job(claimed)

    def _owned(self, connection, job_id, owner, fence):
        row = (
            connection.execute(select(jobs).where(jobs.c.id == job_id).with_for_update())
            .mappings()
            .first()
        )
        now = self._now(connection)
        if (
            row is None
            or row["status"] != "running"
            or row["owner"] != owner
            or row["fence"] != fence
            or row["lease_expires_at"] <= now
        ):
            return None, now
        return row, now

    def heartbeat(self, job_id, owner, fence, lease_seconds=120) -> bool:
        _positive(lease_seconds, "lease_seconds")
        with self._transaction() as connection:
            row, now = self._owned(connection, job_id, owner, fence)
            if row is None:
                return False
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    lease_expires_at=max(row["lease_expires_at"], now + lease_seconds),
                )
            )
            return True

    def complete(self, job_id, owner, fence, result: dict) -> bool:
        result = _json(result)
        with self._transaction() as connection:
            row, now = self._owned(connection, job_id, owner, fence)
            if row is None:
                return False
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    status="completed",
                    owner=None,
                    lease_expires_at=None,
                    result=result,
                    error=None,
                )
            )
            self._audit(
                connection, row["tenant"], owner, "job.completed", job_id, {"fence": fence}, now
            )
            return True

    def fail(self, job_id, owner, fence, error: str, retry: bool = True) -> bool:
        if not isinstance(error, str):
            raise ValueError("error must be a string")
        with self._transaction() as connection:
            row, now = self._owned(connection, job_id, owner, fence)
            if row is None:
                return False
            status = "queued" if retry and row["attempts"] < row["max_attempts"] else "dead"
            delay = min(300, 2 ** (row["attempts"] - 1))
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    status=status,
                    owner=None,
                    lease_expires_at=None,
                    error=error[:8192],
                    available_at=now + delay,
                )
            )
            self._audit(
                connection,
                row["tenant"],
                owner,
                "job.retry" if status == "queued" else "job.dead",
                job_id,
                {"fence": fence, "attempt": row["attempts"]},
                now,
            )
            return True

    def get(self, job_id, tenant: str) -> Job | None:
        _string(tenant, "tenant")
        with self._transaction() as connection:
            row = (
                connection.execute(
                    select(jobs).where(
                        jobs.c.id == job_id,
                        jobs.c.tenant == tenant,
                    )
                )
                .mappings()
                .first()
            )
            return self._job(row) if row else None

    def list_jobs(self, tenant: str, limit: int = 100) -> list[Job]:
        _string(tenant, "tenant")
        _positive(limit, "limit", 1000)
        with self._transaction() as connection:
            return [
                self._job(row)
                for row in connection.execute(
                    select(jobs)
                    .where(
                        jobs.c.tenant == tenant,
                    )
                    .order_by(jobs.c.sequence)
                    .limit(limit)
                ).mappings()
            ]

    def audit(self, tenant, actor, action, resource, details: dict) -> None:
        _string(tenant, "tenant")
        _string(actor, "actor")
        _string(action, "action")
        _string(resource, "resource", 600)
        details = _json(details)
        with self._transaction() as connection:
            self._audit(connection, tenant, actor, action, resource, details, self._now(connection))

    def events(self, tenant, limit=100) -> list[dict]:
        _string(tenant, "tenant")
        _positive(limit, "limit", 1000)
        with self._transaction() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(audit_events)
                    .where(
                        audit_events.c.tenant == tenant,
                    )
                    .order_by(audit_events.c.sequence)
                    .limit(limit)
                ).mappings()
            ]

    def outbox(self, tenant, limit=100) -> list[dict]:
        _string(tenant, "tenant")
        _positive(limit, "limit", 1000)
        with self._transaction() as connection:
            return list(
                connection.scalars(
                    select(outbox_events.c.event)
                    .where(
                        outbox_events.c.tenant == tenant,
                        outbox_events.c.acknowledged_at.is_(None),
                    )
                    .order_by(outbox_events.c.sequence)
                    .limit(limit)
                )
            )

    def ack_outbox(self, tenant, event_id) -> bool:
        _string(tenant, "tenant")
        with self._transaction() as connection:
            result = connection.execute(
                update(outbox_events)
                .where(
                    outbox_events.c.tenant == tenant,
                    outbox_events.c.id == event_id,
                    outbox_events.c.acknowledged_at.is_(None),
                )
                .values(acknowledged_at=self._now(connection))
            )
            return result.rowcount == 1

    def put_record(self, tenant, kind, key, value: dict) -> None:
        _string(tenant, "tenant")
        _string(kind, "kind")
        _string(key, "key", 600)
        value = _json(value)
        with self._transaction() as connection:
            now = self._now(connection)
            self._record(connection, tenant, kind, key, value, now)
            self._audit(
                connection,
                tenant,
                "state",
                "record.put",
                f"{kind}:{key}"[:600],
                {"kind": kind, "key": key},
                now,
            )

    def _record(self, connection, tenant, kind, key, value, now):
        statement = self._insert(records).values(
            tenant=tenant,
            kind=kind,
            key=key,
            value=value,
            updated_at=now,
        )
        if kind in {"candidate", "evidence", "verification"}:
            connection.execute(statement.on_conflict_do_nothing())
            existing = connection.scalar(
                select(records.c.value).where(
                    records.c.tenant == tenant,
                    records.c.kind == kind,
                    records.c.key == key,
                )
            )
            if existing != value:
                raise ValueError(f"{kind} record is immutable")
            return
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=["tenant", "kind", "key"],
                set_={
                    "value": value,
                    "updated_at": now,
                },
            )
        )

    def put_job_record(self, job_id, owner, fence, kind, key, value: dict) -> bool:
        """Persist worker-derived state only while its exact lease is valid."""
        _string(kind, "kind")
        _string(key, "key", 600)
        value = _json(value)
        with self._transaction() as connection:
            row, now = self._owned(connection, job_id, owner, fence)
            if row is None:
                return False
            self._record(connection, row["tenant"], kind, key, value, now)
            self._audit(
                connection,
                row["tenant"],
                owner,
                "record.put",
                f"{kind}:{key}"[:600],
                {"kind": kind, "key": key, "job_id": job_id, "fence": fence},
                now,
            )
            return True

    def get_record(self, tenant, kind, key) -> dict | None:
        _string(tenant, "tenant")
        _string(kind, "kind")
        _string(key, "key", 600)
        with self._transaction() as connection:
            return connection.scalar(
                select(records.c.value).where(
                    records.c.tenant == tenant,
                    records.c.kind == kind,
                    records.c.key == key,
                )
            )
