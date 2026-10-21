import hashlib

import pytest
from fastapi.testclient import TestClient
from test_pipeline import fixture

from patchington.api import Principal, create_app
from patchington.execution import LocalVerifier
from patchington.pipeline import Pipeline
from patchington.store import Store


def completed_candidate(tmp_path, **policy_updates):
    store, policy, forge, model, artifacts = fixture(tmp_path)
    policy = policy.model_copy(update={"publish_enabled": True, **policy_updates})
    job_id = store.enqueue("acme", "acme/shop", 17, "review", {"kind": "review"})
    result = Pipeline(store, [policy], forge, model,
        LocalVerifier(trusted=True, artifacts=artifacts), artifacts).run_once("worker")
    assert result["state"] == "awaiting_approval"
    return store, policy, forge, model, artifacts, result["candidate_id"], job_id


def api(store, policy):
    principals = []
    identities = [("owner", "alice", "acme", ("payments",), ("read", "approve")),
                  ("reader", "alice", "acme", ("payments",), ("read",)),
                  ("other-team", "alice", "acme", ("platform",), ("read", "approve")),
                  ("other-tenant", "alice", "other", ("payments",), ("read", "approve")),
                  ("intruder", "mallory", "acme", ("payments",), ("read", "approve"))]
    for token, actor, tenant, teams, roles in identities:
        principals.append(Principal(actor=actor, tenant=tenant, teams=teams, roles=roles,
                                    token_digest=hashlib.sha256(token.encode()).hexdigest()))
    return TestClient(create_app(store, [policy], principals, webhook_secret=b"w" * 32,
                                approval_secret=b"s" * 32))


@pytest.mark.parametrize("token,status", [(None, 401), ("reader", 404), ("other-team", 404),
                                          ("other-tenant", 404), ("intruder", 403)])
def test_approval_requires_authenticated_tenant_team_role_and_actor_membership(tmp_path, token, status):
    store, policy, _, _, _, candidate_id, _ = completed_candidate(tmp_path)
    client = api(store, policy)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.post(f"/v1/candidates/{candidate_id}/approve", headers=headers,
                           json={"actor": "alice", "approved": True})
    assert response.status_code == status
    assert store.get_record("acme", "approval", candidate_id) is None
    assert not [job for job in store.list_jobs("acme") if job.queue == "publish"]


def test_exact_completed_candidate_approval_uses_authenticated_actor_and_queues_only_publisher(tmp_path):
    store, policy, _, _, _, candidate_id, _ = completed_candidate(tmp_path)
    client = api(store, policy)
    result = client.post(f"/v1/candidates/{candidate_id}/approve",
                         headers={"Authorization": "Bearer owner"}, json={"actor": "mallory"})
    assert result.status_code == 202
    approval = store.get_record("acme", "approval", candidate_id)
    assert approval["actor"] == "alice" and approval["candidate_id"] == candidate_id
    queued = store.get(result.json()["job_id"], "acme")
    assert queued.queue == "publish" and queued.repo == policy.repo and queued.pr == 17


def test_candidate_record_cannot_borrow_a_different_completed_pr_job(tmp_path):
    store, policy, _, _, _, candidate_id, _ = completed_candidate(tmp_path)
    record = store.get_record("acme", "candidate", candidate_id)
    # Seed an upstream association error before the immutable record is first stored.
    store = Store(f"sqlite:///{tmp_path}/mismatched.db")
    store.initialize()
    wrong = store.enqueue("acme", "acme/shop", 18, "other-review", {})
    lease = store.claim("other-worker")
    assert store.complete(wrong, "other-worker", lease.fence, {"candidate_id": candidate_id})
    record["job_id"] = wrong
    store.put_record("acme", "candidate", candidate_id, record)
    response = api(store, policy).post(f"/v1/candidates/{candidate_id}/approve",
                                      headers={"Authorization": "Bearer owner"})
    assert response.status_code == 409
    assert store.get_record("acme", "approval", candidate_id) is None


def test_noncurrent_or_disabled_candidate_cannot_be_approved(tmp_path):
    store, policy, _, _, _, candidate_id, _ = completed_candidate(tmp_path)
    disabled = policy.model_copy(update={"publish_enabled": False})
    response = api(store, disabled).post(f"/v1/candidates/{candidate_id}/approve",
                                        headers={"Authorization": "Bearer owner"})
    assert response.status_code == 409


def test_reject_replaces_authority_with_a_durable_revocation(tmp_path):
    store, policy, _, _, _, candidate_id, _ = completed_candidate(tmp_path)
    client = api(store, policy)
    headers = {"Authorization": "Bearer owner"}
    assert client.post(f"/v1/candidates/{candidate_id}/approve", headers=headers).status_code == 202
    assert client.post(f"/v1/candidates/{candidate_id}/reject", headers=headers).status_code == 200
    assert store.get_record("acme", "approval", candidate_id) == {"rejected": True}
