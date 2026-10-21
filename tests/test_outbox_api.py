import hashlib

from fastapi.testclient import TestClient

from patchington.api import Principal, create_app
from patchington.store import Store


def test_outbox_delivery_requires_tenant_operator_and_ack_is_tenant_scoped(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/state.db")
    store.initialize()
    store.audit("alpha", "worker", "job.dead", "job-1", {})
    store.audit("beta", "worker", "job.dead", "private-job", {})
    identities = [
        Principal(
            actor="operator",
            tenant="alpha",
            teams=(),
            roles=roles,
            token_digest=hashlib.sha256(token.encode()).hexdigest(),
        )
        for roles, token in ((("operate",), "operator-token"), (("read",), "reader-token"))
    ]
    client = TestClient(
        create_app(store, [], identities, webhook_secret=b"w" * 32, approval_secret=b"a" * 32)
    )
    assert client.get("/v1/outbox").status_code == 401
    assert (
        client.get("/v1/outbox", headers={"Authorization": "Bearer reader-token"}).status_code
        == 403
    )
    headers = {"Authorization": "Bearer operator-token"}
    events = client.get("/v1/outbox", headers=headers).json()
    assert len(events) == 1 and events[0]["resource"] == "job-1"
    foreign = store.outbox("beta")[0]["id"]
    assert client.post(f"/v1/outbox/{foreign}/ack", headers=headers).status_code == 404
    assert client.post(f"/v1/outbox/{events[0]['id']}/ack", headers=headers).status_code == 200
    assert client.get("/v1/outbox", headers=headers).json() == []
    assert len(store.outbox("beta")) == 1
