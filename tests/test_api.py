import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from patchington.api import Principal, create_app
from patchington.contracts import RepoPolicy
from patchington.store import Store


def setup(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/state.db")
    store.initialize()
    p = RepoPolicy(
        tenant="acme", team="payments", installation_id=1, repository_id=2, repo="acme/shop"
    )
    principal = Principal(
        actor="alice",
        tenant="acme",
        teams=("payments",),
        roles=("read", "approve"),
        token_digest=hashlib.sha256(b"test-alice-token").hexdigest(),
    )
    app = create_app(store, [p], [principal], webhook_secret=b"w" * 32, approval_secret=b"s" * 32)
    return TestClient(app), store


def payload():
    return {
        "action": "opened",
        "installation": {"id": 1},
        "repository": {"id": 2, "full_name": "acme/shop"},
        "number": 17,
        "pull_request": {"number": 17},
    }


def signed(body, delivery="delivery-1"):
    raw = json.dumps(body).encode()
    return raw, {
        "x-github-event": "pull_request",
        "x-github-delivery": delivery,
        "x-hub-signature-256": "sha256=" + hmac.new(b"w" * 32, raw, hashlib.sha256).hexdigest(),
    }


def test_signed_intake_is_durable_and_idempotent(tmp_path):
    client, store = setup(tmp_path)
    body, headers = signed(payload())
    first = client.post("/webhooks/github", content=body, headers=headers)
    second = client.post("/webhooks/github", content=body, headers=headers)
    assert first.status_code == 202 and first.json() == second.json()
    assert len(store.list_jobs("acme")) == 1


def test_spoofed_installation_or_signature_rejected(tmp_path):
    client, _ = setup(tmp_path)
    body, headers = signed(payload())
    assert client.post("/webhooks/github", content=body, headers={}).status_code == 401
    data = payload()
    data["installation"]["id"] = 666
    body, headers = signed(data)
    assert client.post("/webhooks/github", content=body, headers=headers).status_code == 403


def test_api_denies_anonymous_and_cross_tenant_reads(tmp_path):
    client, store = setup(tmp_path)
    job = store.enqueue("other", "other/secret", 1, "secret", {})
    assert client.get(f"/v1/jobs/{job}").status_code == 401
    assert (
        client.get(
            f"/v1/jobs/{job}", headers={"Authorization": "Bearer test-alice-token"}
        ).status_code
        == 404
    )


def test_unsupported_or_bot_command_event_never_authorizes_execution(tmp_path):
    client, store = setup(tmp_path)
    body, headers = signed(payload())
    headers["x-github-event"] = "issue_comment"
    assert client.post("/webhooks/github", content=body, headers=headers).status_code == 202
    assert store.list_jobs("acme") == []


def test_oversized_request_rejected_before_parse(tmp_path):
    client, _ = setup(tmp_path)
    response = client.post("/webhooks/github", content=b"x" * 2_000_001)
    assert response.status_code == 413
