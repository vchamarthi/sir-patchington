"""Authenticated App actor ownership; all GitHub responses are mocked."""

import json
from dataclasses import FrozenInstanceError

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from test_github import fixture_handler, policy

from patchington.github import AppTokenProvider, GitHubClient, GitHubError, SplitTokenProvider


def test_actor_lookup_authenticates_app_then_uses_repository_metadata_only_token():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path in {"/app", "/app/installations/1/access_tokens"}:
            jwt.decode(request.headers["Authorization"].split()[1], key.public_key(),
                       algorithms=["RS256"], issuer="42")
        if request.url.path == "/app":
            return httpx.Response(200, json={"id": 42, "slug": "sir-review"})
        if request.url.path.endswith("/access_tokens"):
            assert json.loads(request.content) == {
                "repository_ids": [2], "permissions": {"metadata": "read"}}
            return httpx.Response(201, json={"token": "metadata-only"})
        assert request.url.path == "/users/sir-review[bot]"
        assert request.headers["Authorization"] == "Bearer metadata-only"
        return httpx.Response(200, json={"id": 91, "login": "sir-review[bot]", "type": "Bot"})

    provider = AppTokenProvider(42, pem, transport=httpx.MockTransport(handle))
    actor = provider.review_actor(1, 2)
    assert (actor.id, actor.login, actor.app_id) == (91, "sir-review[bot]", 42)
    with pytest.raises(FrozenInstanceError):
        actor.id = 99
    assert calls == ["/app", "/app/installations/1/access_tokens", "/users/sir-review[bot]"]


@pytest.mark.parametrize(("app", "user"), [
    ({"id": 99, "slug": "sir-review"}, {}),
    ({"id": 42, "slug": "../wrong"}, {}),
    ({"id": 42, "slug": "sir-review"}, {"id": 91, "login": "other[bot]", "type": "Bot"}),
    ({"id": 42, "slug": "sir-review"}, {"id": 91, "login": "sir-review[bot]", "type": "User"}),
    ({"id": 42, "slug": "sir-review"}, {"id": 0, "login": "sir-review[bot]", "type": "Bot"}),
    ({"id": 42, "slug": "sir-review"}, {"id": True, "login": "sir-review[bot]", "type": "Bot"}),
])
def test_actor_lookup_refuses_malformed_or_mismatched_identity(monkeypatch, app, user):
    monkeypatch.setattr("patchington.github.jwt.encode", lambda *args, **kwargs: "app-jwt")

    def handle(request):
        if request.url.path == "/app":
            return httpx.Response(200, json=app)
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "metadata-only"})
        return httpx.Response(200, json=user)

    provider = AppTokenProvider(42, "unused", transport=httpx.MockTransport(handle))
    with pytest.raises(GitHubError):
        provider.review_actor(1, 2)


@pytest.mark.parametrize(("comment_actor", "performed_app", "expected"), [
    (91, None, "9"), (92, None, "10"), (91, {"id": 999}, "10"), (91, {"id": 42}, "9"),
])
def test_authenticated_actor_reconciles_nullable_app_metadata_without_adopting_lookalikes(
        monkeypatch, comment_actor, performed_app, expected):
    monkeypatch.setattr("patchington.github.jwt.encode", lambda *args, **kwargs: "app-jwt")
    comments = [{"id": 9, "body": "<!-- sir-patchington:review:v1:2:7 -->\nold",
                 "performed_via_github_app": performed_app,
                 "user": {"type": "Bot", "id": comment_actor, "login": "sir-review[bot]"}}]
    writes = []

    def handle(request):
        if request.url.path == "/app":
            return httpx.Response(200, json={"id": 42, "slug": "sir-review"})
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "scoped-token"})
        if request.url.path == "/users/sir-review[bot]":
            return httpx.Response(200, json={"id": 91, "login": "sir-review[bot]", "type": "Bot"})
        if request.url.path.endswith("/issues/7/comments"):
            if request.method == "GET":
                return httpx.Response(200, json=comments)
            writes.append(request.method)
            comments.append({"id": 10, "body": json.loads(request.content)["body"],
                "performed_via_github_app": None,
                "user": {"id": 91, "login": "sir-review[bot]", "type": "Bot"}})
            return httpx.Response(201, json={"id": 10})
        if request.url.path.endswith("/issues/comments/9"):
            writes.append(request.method)
            comments[0]["body"] = json.loads(request.content)["body"]
            return httpx.Response(200, json={"id": 9})
        return fixture_handler(request)

    transport = httpx.MockTransport(handle)
    provider = AppTokenProvider(42, "unused", transport=transport)
    client = GitHubClient(provider, transport=transport)
    p = policy(post_reviews=True)
    snapshot = client.snapshot(p, 7)
    assert client.post_review(p, snapshot, ()) == expected
    assert client.post_review(p, snapshot, ()) == expected
    assert writes == (["PATCH"] if expected == "9" else ["POST"])


def test_actor_lookup_failure_never_falls_back_to_legacy_app_proof():
    def tokens(*args):
        return "token"

    def rejected(*args):
        raise GitHubError("actor lookup failed")

    tokens.app_id = 42
    tokens.review_actor = rejected
    writes = []

    def handle(request):
        if request.url.path.endswith("/issues/7/comments"):
            if request.method == "GET":
                return httpx.Response(200, json=[])
            writes.append(request)
            return httpx.Response(201, json={"id": 1})
        return fixture_handler(request)

    client = GitHubClient(tokens, transport=httpx.MockTransport(handle))
    p = policy(post_reviews=True)
    snapshot = client.snapshot(p, 7)
    with pytest.raises(GitHubError, match="actor lookup"):
        client.post_review(p, snapshot, ())
    assert not writes


def test_split_actor_lookup_uses_only_mapped_reporter_installation():
    calls = []

    class Provider:
        app_id = 43

        def review_actor(self, installation, repository):
            calls.append((installation, repository))
            return "actor"

    split = SplitTokenProvider(object(), Provider(), {(1, 2): 99})
    assert split.review_actor(1, 2) == "actor" and calls == [(99, 2)]
    with pytest.raises(GitHubError, match="mapping"):
        split.review_actor(1, 3)
