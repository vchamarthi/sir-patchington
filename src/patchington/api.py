"""Authenticated tenant/team API and signed, bounded GitHub intake."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import asdict
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import Field

from .contracts import Candidate, Frozen, RepoPolicy, Verification
from .records import candidate_record as resolve_candidate
from .store import Store


class Principal(Frozen):
    actor: str
    tenant: str
    teams: tuple[str, ...]
    roles: tuple[str, ...]
    token_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def create_app(
    store: Store,
    policies: list[RepoPolicy],
    principals: list[Principal],
    *,
    webhook_secret: bytes,
    approval_secret: bytes,
) -> FastAPI:
    if min(len(webhook_secret), len(approval_secret)) < 32:
        raise ValueError("webhook and approval secrets must each contain at least 32 bytes")
    if len({(p.installation_id, p.repository_id) for p in policies}) != len(policies):
        raise ValueError("repository identity may belong to only one policy")
    app = FastAPI(title="Sir Patchington", docs_url=None, redoc_url=None)
    index = {(p.installation_id, p.repository_id): p for p in policies}

    def authenticate(authorization: str = Header(default="")) -> Principal:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "authentication required")
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        for principal in principals:
            if hmac.compare_digest(digest, principal.token_digest):
                return principal
        raise HTTPException(401, "invalid identity")

    def permitted(principal: Principal, repo: str, role: str = "read") -> RepoPolicy:
        policy = next(
            (
                p
                for p in policies
                if p.repo == repo and p.tenant == principal.tenant and p.team in principal.teams
            ),
            None,
        )
        if policy is None or role not in principal.roles:
            raise HTTPException(404, "resource not found")
        return policy

    @app.get("/livez")
    def live():
        return {"service": "sir-patchington", "status": "alive"}

    @app.get("/readyz")
    def ready():
        try:
            with store.engine.connect() as connection:
                from sqlalchemy import text

                connection.execute(text("SELECT 1"))
        except Exception:
            raise HTTPException(503, "state service unavailable") from None
        return {"status": "ready"}

    @app.post("/webhooks/github", status_code=202)
    async def webhook(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2_000_000:
                raise HTTPException(413, "webhook exceeds limit")
        signature = "sha256=" + hmac.new(webhook_secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(401, "invalid webhook signature")
        event = request.headers.get("x-github-event", "")
        allowed = {
            "pull_request": {"opened", "synchronize", "reopened", "ready_for_review", "closed"},
            "pull_request_review": {"submitted", "edited", "dismissed"},
            "pull_request_review_comment": {"created", "edited", "deleted"},
        }
        if event not in allowed:
            return {"ignored": True}
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("webhook must be an object")
            if value.get("action") not in allowed[event]:
                return {"ignored": True}
            policy = index.get((value["installation"]["id"], value["repository"]["id"]))
            if policy is None or value["repository"]["full_name"] != policy.repo:
                raise HTTPException(403, "repository installation is not enrolled")
            pr = value.get("number") or value["pull_request"]["number"]
            delivery = request.headers.get("x-github-delivery", "")
            if (
                not isinstance(pr, int)
                or isinstance(pr, bool)
                or pr < 1
                or not delivery
                or len(delivery) > 200
            ):
                raise ValueError("invalid webhook identity")
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "malformed webhook") from None
        payload = {
            "kind": "review",
            "event": event,
            "action": value["action"],
            "installation_id": policy.installation_id,
            "repository_id": policy.repository_id,
        }
        job = store.enqueue(
            policy.tenant, policy.repo, pr, delivery, payload, max_attempts=policy.max_attempts
        )
        return {"job_id": job}

    @app.get("/v1/jobs")
    def jobs(principal: Principal = Depends(authenticate)):
        result = []
        for job in store.list_jobs(principal.tenant):
            try:
                permitted(principal, job.repo)
                result.append(asdict(job))
            except HTTPException:
                pass
        return result

    @app.get("/v1/jobs/{job_id}")
    def job(job_id: str, principal: Principal = Depends(authenticate)):
        found = store.get(job_id, principal.tenant)
        if found is None:
            raise HTTPException(404, "resource not found")
        permitted(principal, found.repo)
        return asdict(found)

    def candidate_record(candidate_id: str, principal: Principal):
        record = resolve_candidate(store, principal.tenant, candidate_id)
        if record is None:
            raise HTTPException(404, "resource not found")
        candidate = Candidate.model_validate_json(json.dumps(record["candidate"]))
        verification = Verification.model_validate_json(json.dumps(record["verification"]))
        policy = permitted(principal, candidate.snapshot.repo, "approve")
        origin = store.get(record["job_id"], principal.tenant)
        if (
            origin is None
            or origin.status != "completed"
            or not origin.result
            or origin.result.get("candidate_id") != candidate_id
            or origin.repo != candidate.snapshot.repo
            or origin.pr != candidate.snapshot.pr
            or verification.candidate_id != candidate_id
            or candidate.id != candidate_id
            or policy.digest != candidate.policy_digest
        ):
            raise HTTPException(409, "candidate is not a current completed result")
        if principal.actor not in policy.approvers:
            raise HTTPException(403, "identity is not a repository approver")
        return candidate, verification, policy

    @app.post("/v1/candidates/{candidate_id}/approve", status_code=202)
    def approve(candidate_id: str, principal: Principal = Depends(authenticate)):
        from .publisher import sign_approval

        candidate, verification, policy = candidate_record(candidate_id, principal)
        if not verification.passed or not policy.publish_enabled:
            raise HTTPException(409, "publication disabled or required verification incomplete")
        approval = sign_approval(
            candidate, verification, principal.actor, approval_secret, int(time.time()) + 900
        )
        store.put_record(
            principal.tenant, "approval", candidate_id, approval.model_dump(mode="json")
        )
        job_id = store.enqueue(
            principal.tenant,
            policy.repo,
            candidate.snapshot.pr,
            "approval-" + approval.nonce,
            {"kind": "publish", "candidate_id": candidate_id},
            max_attempts=10,
            queue="publish",
        )
        store.audit(
            principal.tenant,
            principal.actor,
            "candidate.approved",
            candidate_id,
            {"job_id": job_id, "evidence_digest": verification.evidence_digest},
        )
        return {"job_id": job_id, "candidate_id": candidate_id}

    @app.post("/v1/candidates/{candidate_id}/reject")
    def reject(candidate_id: str, principal: Principal = Depends(authenticate)):
        candidate_record(candidate_id, principal)
        store.put_record(principal.tenant, "approval", candidate_id, {"rejected": True})
        store.audit(principal.tenant, principal.actor, "candidate.rejected", candidate_id, {})
        return {"rejected": True}

    @app.get("/v1/audit")
    def audit(principal: Principal = Depends(authenticate)):
        if "operate" not in principal.roles:
            raise HTTPException(403, "operator role required")
        # Audit contains metadata across tenant teams, reserved for tenant operators.
        return store.events(principal.tenant)

    @app.get("/metrics")
    def metrics(principal: Principal = Depends(authenticate)):
        counts: dict[str, int] = {}
        for row in store.list_jobs(principal.tenant):
            try:
                permitted(principal, row.repo)
                counts[row.status] = counts.get(row.status, 0) + 1
            except HTTPException:
                pass
        lines = ["# TYPE patchington_recent_jobs gauge"]
        lines += [
            f'patchington_recent_jobs{{status="{status}"}} {count}'
            for status, count in sorted(counts.items())
        ]
        return Response("\n".join(lines) + "\n", media_type="text/plain")

    @app.get("/v1/outbox")
    def outbox(principal: Principal = Depends(authenticate)):
        if "operate" not in principal.roles:
            raise HTTPException(403, "operator role required")
        return store.outbox(principal.tenant)

    @app.post("/v1/outbox/{event_id}/ack")
    def acknowledge(event_id: str, principal: Principal = Depends(authenticate)):
        if "operate" not in principal.roles:
            raise HTTPException(403, "operator role required")
        if not store.ack_outbox(principal.tenant, event_id):
            raise HTTPException(404, "resource not found")
        return {"acknowledged": True}

    return app
