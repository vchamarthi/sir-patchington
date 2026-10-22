import gzip
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def policy(**changes):
    from patchington.contracts import RepoPolicy
    return RepoPolicy(tenant="acme", team="payments", installation_id=1, repository_id=2,
                      repo="acme/shop", **changes)


def pull(**changes):
    repo = {"id": 2, "full_name": "acme/shop"}
    value = {"number": 7, "state": "open", "draft": False, "merged": False,
             "title": "Fix logic", "body": "Owner request", "changed_files": 1,
             "base": {"sha": "a" * 40, "ref": "main", "repo": repo},
             "head": {"sha": "b" * 40, "ref": "repair", "repo": repo}}
    value.update(changes)
    return value


def fixture_handler(request):
    path = request.url.path
    if path == "/repos/acme/shop":
        return httpx.Response(200, json={"id": 2, "full_name": "acme/shop"})
    if path == "/repos/acme/shop/pulls/7":
        return httpx.Response(200, json=pull())
    if path.endswith("/files"):
        return httpx.Response(200, json=[{"filename": "src/a.py", "status": "modified",
                                         "patch": "@@ -1 +1 @@\n-old\n+new"}])
    if path.endswith("/reviews"):
        return httpx.Response(200, json=[])
    if path == "/graphql":
        return httpx.Response(200, json={"data": {"repository": {"databaseId": 2,
            "pullRequest": {"reviewThreads": {"nodes": [],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
    raise AssertionError(f"Unexpected request {request.method} {request.url}")


def test_app_tokens_are_repository_and_permission_scoped_and_jwt_bounded():
    from patchington.github import API_VERSION, AppTokenProvider

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    calls = []

    def handle(request):
        claims = jwt.decode(request.headers["Authorization"].split()[1], key.public_key(),
                            algorithms=["RS256"], issuer="42")
        assert claims["exp"] - claims["iat"] <= 600
        assert claims["iat"] <= time.time()
        assert request.headers["X-GitHub-Api-Version"] == API_VERSION
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(201, json={"token": f"scoped-{len(calls)}",
                                        "expires_at": "2099-01-01T00:00:00Z"})

    provider = AppTokenProvider(42, pem, transport=httpx.MockTransport(handle))
    assert provider(1, 2) == "scoped-1"
    assert provider(1, 2, write=True) == "scoped-2"
    assert provider(1, 3) == "scoped-3"
    assert [body["repository_ids"] for body in calls] == [[2], [2], [3]]
    assert calls[0]["permissions"]["contents"] == "read"
    assert calls[1]["permissions"]["contents"] == "write"
    assert "pull_requests" not in calls[1]["permissions"]
    assert "administration" not in calls[1]["permissions"]
    assert provider.review_token(1, 2) == "scoped-4"
    assert "contents" not in calls[3]["permissions"]
    assert calls[3]["permissions"]["pull_requests"] == "write"


def test_app_provider_refuses_redirect_and_invalid_identifiers():
    from patchington.github import AppTokenProvider, GitHubError

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.test/steal"})

    provider = AppTokenProvider(42, pem, transport=httpx.MockTransport(handle))
    with pytest.raises(GitHubError, match="status 302"):
        provider(1, 2)
    with pytest.raises(ValueError):
        provider(0, 2)
    assert len(calls) == 1


def test_snapshot_is_authoritative_and_scoped():
    from patchington.github import GitHubClient
    scopes = []
    client = GitHubClient(lambda *args: scopes.append(args) or "private-token",
                          transport=httpx.MockTransport(fixture_handler))
    snapshot = client.snapshot(policy(), 7)
    assert snapshot.head_sha == "b" * 40 and snapshot.base_sha == "a" * 40
    assert snapshot.files[0].path == "src/a.py"
    assert snapshot.head_repo == "acme/shop"
    assert all(scope == (1, 2, False) for scope in scopes)


@pytest.mark.parametrize("change", [{"draft": True}, {"state": "closed"},
    {"head": {"sha": "b" * 40, "ref": "repair", "repo": {"id": 3,
                                                                     "full_name": "evil/fork"}}},
    {"changed_files": 101}])
def test_snapshot_rejects_ineligible_pull_requests(change):
    from patchington.github import GitHubClient, GitHubError
    def handle(request):
        if request.url.path == "/repos/acme/shop/pulls/7":
            return httpx.Response(200, json=pull(**change))
        return fixture_handler(request)
    with pytest.raises(GitHubError):
        GitHubClient(lambda *args: "token", transport=httpx.MockTransport(handle)).snapshot(policy(), 7)


def test_snapshot_refuses_cross_host_pagination_without_leaking_token():
    from patchington.github import GitHubClient, GitHubError
    requests = []
    def handle(request):
        requests.append(str(request.url))
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[], headers={"Link":
                '<https://evil.test/steal>; rel="next"'})
        return fixture_handler(request)
    with pytest.raises(GitHubError, match="pagination"):
        GitHubClient(lambda *args: "secret", transport=httpx.MockTransport(handle)).snapshot(policy(), 7)
    assert not any("evil.test" in url for url in requests)


def test_snapshot_rejects_head_that_moves_during_pagination():
    from patchington.github import GitHubClient, GitHubError
    reads = 0
    def handle(request):
        nonlocal reads
        if request.url.path == "/repos/acme/shop/pulls/7":
            reads += 1
            value = pull()
            if reads == 2:
                value["head"]["sha"] = "c" * 40
            return httpx.Response(200, json=value)
        return fixture_handler(request)
    with pytest.raises(GitHubError, match="changed"):
        GitHubClient(lambda *args: "token", transport=httpx.MockTransport(handle)).snapshot(policy(), 7)


def test_snapshot_reads_all_file_and_review_thread_comment_pages():
    from patchington.github import GitHubClient
    calls = []
    def handle(request):
        calls.append(str(request.url))
        if request.url.path.endswith("/files"):
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json=[])
            response = fixture_handler(request)
            response.headers["Link"] = ('<https://api.github.com/repos/acme/shop/pulls/7/'
                                        'files?per_page=100&page=2>; rel="next"')
            return response
        if request.url.path == "/graphql":
            body = json.loads(request.content)
            comments = {"nodes": [{"id": "C1", "body": "first", "author": {"login": "a"},
                                    "updatedAt": "2026-10-20"}],
                        "pageInfo": {"hasNextPage": True, "endCursor": "next-comment"}}
            if body["variables"].get("thread"):
                return httpx.Response(200, json={"data": {"node": {"comments": {
                    "nodes": [{"id": "C2", "body": "second", "author": {"login": "b"},
                               "updatedAt": "2026-10-21"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
            return httpx.Response(200, json={"data": {"repository": {"databaseId": 2,
                "pullRequest": {"reviewThreads": {"nodes": [{"id": "T1", "path": "src/a.py",
                    "line": 1, "isResolved": False, "isOutdated": False, "comments": comments}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
        return fixture_handler(request)
    snapshot = GitHubClient(lambda *args: "token", transport=httpx.MockTransport(handle)).snapshot(policy(), 7)
    assert "second" in snapshot.threads[0].body
    assert any("page=2" in url for url in calls)


def test_reviews_disabled_and_invalid_anchors_never_write():
    from patchington.contracts import Finding
    from patchington.github import GitHubClient, GitHubError
    client = GitHubClient(lambda *args: "token", transport=httpx.MockTransport(fixture_handler))
    snapshot = client.snapshot(policy(), 7)
    with pytest.raises(GitHubError, match="disabled"):
        client.post_review(policy(), snapshot, ())
    finding = Finding(path="src/a.py", line=55, severity="high", category="correctness",
                      claim="Bad condition", trigger="x", expected="y", reason="reason",
                      status="supported")
    with pytest.raises(GitHubError, match="anchor"):
        client.post_review(policy(post_reviews=True), snapshot, (finding,))


def test_review_marker_reconciles_only_comments_owned_by_the_authenticated_app():
    from patchington.contracts import Finding
    from patchington.github import GitHubClient
    comments = [{"id": 9, "body": "<!-- sir-patchington:review:v1:2:7 -->\nforged",
                 "performed_via_github_app": {"id": 999}, "user": {"type": "Bot"}}]
    writes = []
    def tokens(*args):
        return "token"
    tokens.app_id = 42
    def handle(request):
        if request.url.path.endswith("/issues/7/comments"):
            if request.method == "GET":
                return httpx.Response(200, json=comments)
            writes.append(request)
            body = json.loads(request.content)["body"]
            comments.append({"id": 10, "body": body, "performed_via_github_app": {"id": 42},
                             "user": {"type": "Bot"}})
            return httpx.Response(201, json={"id": 10})
        return fixture_handler(request)
    client = GitHubClient(tokens, transport=httpx.MockTransport(handle))
    p = policy(post_reviews=True)
    snapshot = client.snapshot(p, 7)
    finding = Finding(path="src/a.py", line=1, severity="high", category="correctness",
                      claim="Bad condition", trigger="x", expected="y", reason="reason",
                      status="supported")
    assert client.post_review(p, snapshot, (finding,)) == "10"
    assert client.post_review(p, snapshot, (finding,)) == "10"
    assert len(writes) == 1
    assert "/blob/" + "b" * 40 + "/src/a.py#L1" in comments[-1]["body"]


def test_checks_paginates_runs_and_statuses_without_inventing_success():
    from patchington.github import GitHubClient
    def handle(request):
        if request.url.path.endswith("/check-runs"):
            rows = [{"id": 1, "head_sha": "b" * 40, "status": "in_progress", "conclusion": None}]
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json={"check_runs": []})
            return httpx.Response(200, json={"check_runs": rows}, headers={"Link":
                f'<https://api.github.com{request.url.path}?per_page=100&page=2>; rel="next"'})
        if request.url.path.endswith("/statuses"):
            return httpx.Response(200, json=[{"context": "build", "state": "pending"}])
        return fixture_handler(request)
    client = GitHubClient(lambda *args: "token", transport=httpx.MockTransport(handle))
    result = client.checks(policy(), client.snapshot(policy(), 7))
    assert result["head_sha"] == "b" * 40
    assert result["check_runs"][0]["conclusion"] is None
    assert result["statuses"][0]["state"] == "pending"


def test_git_environment_does_not_inherit_auth_or_git_configuration(monkeypatch, tmp_path):
    from patchington.github import git_environment
    monkeypatch.setenv("GIT_CONFIG_COUNT", "100")
    monkeypatch.setenv("GIT_CONFIG_KEY_99", "core.sshCommand")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-reach-git")
    monkeypatch.setenv("GIT_SSH_COMMAND", "malicious")
    monkeypatch.setenv("LD_PRELOAD", "/evil.so")
    env = git_environment(tmp_path)
    assert not {"GITHUB_TOKEN", "GIT_SSH_COMMAND", "LD_PRELOAD", "GIT_CONFIG_COUNT"} & env.keys()
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    authenticated = git_environment(tmp_path, "one-operation-token")
    assert authenticated["GIT_CONFIG_COUNT"] == "1"
    assert "GIT_CONFIG_KEY_99" not in authenticated


def test_http_response_byte_bound_is_enforced_before_json_parsing():
    from patchington.github import GitHubClient, GitHubError
    client = GitHubClient(lambda *args: "token", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 10_000_001)))
    with pytest.raises(GitHubError, match="bound"):
        client.snapshot(policy(), 7)


def test_http_stops_reading_a_response_once_the_stream_exceeds_the_bound():
    from patchington.github import GitHubClient, GitHubError
    class UnboundedResponse(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * 5_000_001
            yield b"x" * 5_000_001
            raise AssertionError("Response was read past its byte bound")
    client = GitHubClient(lambda *args: "token", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=UnboundedResponse())))
    with pytest.raises(GitHubError, match="bound"):
        client.snapshot(policy(), 7)


def test_numeric_repository_pagination_is_allowed_only_for_the_same_repository():
    from patchington.github import GitHubClient
    def handle(request):
        if request.url.path == "/repositories/2/pulls/7/files":
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/files"):
            response = fixture_handler(request)
            response.headers["Link"] = ('<https://api.github.com/repositories/2/pulls/7/'
                                        'files?per_page=100&page=2>; rel="next"')
            return response
        return fixture_handler(request)
    snapshot = GitHubClient(lambda *args: "token", transport=httpx.MockTransport(handle)).snapshot(policy(), 7)
    assert snapshot.head_sha == "b" * 40


def test_review_rechecks_stale_head_after_comment_pagination():
    from patchington.github import GitHubClient, GitHubError
    moved = False
    def tokens(*args):
        return "token"
    tokens.app_id = 42
    def handle(request):
        nonlocal moved
        if request.url.path.endswith("/issues/7/comments"):
            assert request.method == "GET", "stale review must not write"
            moved = True
            return httpx.Response(200, json=[])
        if moved and request.url.path == "/repos/acme/shop/pulls/7":
            value = pull()
            value["head"]["sha"] = "c" * 40
            return httpx.Response(200, json=value)
        return fixture_handler(request)
    client = GitHubClient(tokens, transport=httpx.MockTransport(handle))
    p = policy(post_reviews=True)
    snapshot = client.snapshot(p, 7)
    with pytest.raises(GitHubError, match="Stale"):
        client.post_review(p, snapshot, ())


def test_compressed_responses_are_bounded_after_decompression_without_double_decoding():
    from patchington.github import GitHubClient
    def handle(request):
        response = fixture_handler(request)
        return httpx.Response(200, content=gzip.compress(response.content),
                              headers={"Content-Encoding": "gzip"})
    snapshot = GitHubClient(lambda *args: "token", transport=httpx.MockTransport(handle)).snapshot(policy(), 7)
    assert snapshot.head_sha == "b" * 40


def test_split_provider_never_confuses_read_and_write_app_installation_ids():
    from patchington.github import GitHubError, SplitTokenProvider
    calls = []
    class Provider:
        def __init__(self, app_id):
            self.app_id = app_id
        def __call__(self, installation, repo, write=False):
            calls.append((self.app_id, installation, repo, write))
            return f"token-{self.app_id}"
        def review_token(self, installation, repo):
            calls.append((self.app_id, installation, repo, "review"))
            return "review"
    split = SplitTokenProvider(Provider(42), Provider(43), {(1, 2): 99})
    assert split(1, 2) == "token-42"
    assert split(1, 2, write=True) == "token-43"
    assert split.review_token(1, 2) == "review"
    assert split.app_id == 43
    assert calls == [(42, 1, 2, False), (43, 99, 2, True), (43, 99, 2, "review")]
    with pytest.raises(GitHubError, match="mapping"):
        split(1, 3, write=True)
    with pytest.raises(GitHubError, match="mapping"):
        split.review_token(1, 3)


def test_model_report_markup_images_html_and_mentions_render_as_literal_text():
    from markdown_it import MarkdownIt

    from patchington.contracts import Finding
    from patchington.github import GitHubClient
    written = []
    def tokens(*args):
        return "token"
    tokens.app_id = 42
    def handle(request):
        if request.url.path.endswith("/issues/7/comments"):
            if request.method == "GET":
                return httpx.Response(200, json=[])
            written.append(json.loads(request.content)["body"])
            return httpx.Response(201, json={"id": 1})
        return fixture_handler(request)
    attack = '![track](https://evil.test/track.png) <img src="https://evil.test/pixel"> @owner @org/team'
    tricky = '`break` ``more``\n<!-- forged -->\n# fake heading'
    client = GitHubClient(tokens, transport=httpx.MockTransport(handle))
    p = policy(post_reviews=True)
    snapshot = client.snapshot(p, 7)
    finding = Finding(path="src/a.py", line=1, severity="high", category="correctness",
                      claim=attack, trigger=tricky, expected=attack, reason=attack, status="supported")
    client.post_review(p, snapshot, (finding,))
    tokens = MarkdownIt("commonmark").parse(written[0])
    children = [child for token in tokens for child in (token.children or [])]
    assert not any(token.type in {"image", "html_inline"} for token in children)
    assert not any(token.type == "html_block" and "forged" in token.content for token in tokens)
    assert any(token.type == "code_inline" and attack in token.content for token in children)
    assert not any(token.type == "text" and "@owner" in token.content for token in children)
    anchors = [token.attrGet("href") for token in children if token.type == "link_open"]
    assert anchors == ["https://github.com/acme/shop/blob/" + "b" * 40 + "/src/a.py#L1"]


def test_review_limitations_are_literal_and_bounded_before_comment_publication():
    from markdown_it import MarkdownIt

    from patchington.github import GitHubClient
    written = []

    def tokens(*args):
        return "token"

    tokens.app_id = 42

    def handle(request):
        if request.url.path.endswith("/issues/7/comments"):
            if request.method == "GET":
                return httpx.Response(200, json=[])
            written.append(json.loads(request.content)["body"])
            return httpx.Response(201, json={"id": 1})
        return fixture_handler(request)

    client = GitHubClient(tokens, transport=httpx.MockTransport(handle))
    p = policy(post_reviews=True)
    snapshot = client.snapshot(p, 7)
    attack = '</details> ![track](https://evil.test/pixel) <img src="x"> @owner `escape`'
    limitations = (attack, *("\u00e9" * 20_000 for _ in range(100)))
    client.post_review(p, snapshot, (), review_limitations=limitations)
    body = written[0]
    assert len(body.encode()) < 15_000 and "omitted" in body
    assert "**Review coverage is limited.**" in body
    parsed = MarkdownIt("commonmark").parse(body)
    children = [child for token in parsed for child in (token.children or [])]
    assert not any(token.type in {"image", "html_inline", "link_open"} for token in children)
    assert any(token.type == "code_inline" and attack in token.content for token in children)
    assert not any(token.type == "text" and "@owner" in token.content for token in children)


def test_open_pr_discovery_is_paginated_scoped_and_skips_disallowed_drafts_and_forks():
    from patchington.github import GitHubClient
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/repos/acme/shop/pulls":
            assert request.url.params["state"] == "open"
            if request.url.params.get("page") == "2":
                second = pull()
                second["number"] = 8
                return httpx.Response(200, json=[second])
            draft = pull(draft=True)
            draft["number"] = 9
            fork = pull()
            fork["number"] = 10
            fork["head"]["repo"] = {"id": 3, "full_name": "other/fork"}
            return httpx.Response(200, json=[pull(), draft, fork], headers={"Link":
                '<https://api.github.com/repos/acme/shop/pulls?state=open&per_page=100&page=2>; rel="next"'})
        return fixture_handler(request)
    scopes = []
    client = GitHubClient(lambda *args: scopes.append(args) or "token", transport=httpx.MockTransport(handle))
    assert client.list_open_prs(policy()) == [7, 8]
    assert all(scope == (1, 2, False) for scope in scopes)
    assert "/graphql" not in calls


def test_open_pr_discovery_refuses_identity_mismatch_in_a_list_row():
    from patchington.github import GitHubClient, GitHubError
    def handle(request):
        if request.url.path == "/repos/acme/shop/pulls":
            row = pull()
            row["base"]["repo"] = {"id": 666, "full_name": "other/private"}
            return httpx.Response(200, json=[row])
        return fixture_handler(request)
    with pytest.raises(GitHubError, match="identity"):
        GitHubClient(lambda *args: "token", transport=httpx.MockTransport(handle)).list_open_prs(policy())


def test_review_queue_guard_runs_after_token_mint_before_the_http_write():
    from patchington.github import GitHubClient
    minted = False
    def tokens(*args):
        return "read-token"
    def review_token(*args):
        nonlocal minted
        minted = True
        return "write-token"
    tokens.app_id = 42
    tokens.review_token = review_token
    def handle(request):
        if request.url.path.endswith("/issues/7/comments"):
            assert request.method == "GET", "stale queue authority must not post"
            return httpx.Response(200, json=[])
        return fixture_handler(request)
    def guard():
        if minted:
            raise PermissionError("lease lost during token mint")
    client = GitHubClient(tokens, transport=httpx.MockTransport(handle))
    p = policy(post_reviews=True)
    snapshot = client.snapshot(p, 7)
    with pytest.raises(PermissionError, match="lease"):
        client.post_review(p, snapshot, (), guard=guard)


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_api_rate_limit_and_server_errors_are_explicitly_retryable(status):
    from patchington.github import GitHubClient, GitHubTransientError
    client = GitHubClient(lambda *args: "token", transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={"message": "private detail"})))
    with pytest.raises(GitHubTransientError) as error:
        client.snapshot(policy(), 7)
    assert "private detail" not in str(error.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_api_client_errors_are_not_misclassified_as_transient(status):
    from patchington.github import GitHubClient, GitHubError, GitHubTransientError
    client = GitHubClient(lambda *args: "token", transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={})))
    with pytest.raises(GitHubError) as error:
        client.snapshot(policy(), 7)
    assert not isinstance(error.value, GitHubTransientError)


def test_api_network_failure_is_retryable_but_invalid_json_is_not():
    from patchington.github import GitHubClient, GitHubError, GitHubTransientError
    def handle(request):
        raise httpx.ConnectError("private network detail", request=request)
    client = GitHubClient(lambda *args: "token", transport=httpx.MockTransport(handle))
    with pytest.raises(GitHubTransientError):
        client.snapshot(policy(), 7)
    client = GitHubClient(lambda *args: "token", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"invalid")))
    with pytest.raises(GitHubError) as error:
        client.snapshot(policy(), 7)
    assert not isinstance(error.value, GitHubTransientError)


def test_snapshot_captures_all_human_review_summary_pages_and_preserves_review_state():
    from patchington.github import GitHubClient
    def handle(request):
        if request.url.path.endswith("/reviews"):
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json=[{"id": 33, "body": "Old requirement dismissed",
                    "state": "DISMISSED", "commit_id": "a" * 40,
                    "submitted_at": "2026-10-21T01:00:00Z", "user": {"login": "owner", "type": "User"}}])
            return httpx.Response(200, json=[
                {"id": 31, "body": "Please fix the owner requirement", "state": "CHANGES_REQUESTED",
                 "commit_id": "b" * 40, "submitted_at": "2026-10-21T00:00:00Z",
                 "user": {"login": "owner", "type": "User"}},
                {"id": 32, "body": "Bot summary must not feed itself", "state": "COMMENTED",
                 "commit_id": "b" * 40, "submitted_at": "2026-10-21T00:00:00Z",
                 "user": {"login": "patchington[bot]", "type": "Bot"}}], headers={"Link":
                '<https://api.github.com/repos/acme/shop/pulls/7/reviews?per_page=100&page=2>; rel="next"'})
        return fixture_handler(request)
    snapshot = GitHubClient(lambda *args: "token", transport=httpx.MockTransport(handle)).snapshot(policy(), 7)
    assert [thread.id for thread in snapshot.threads] == ["review:31", "review:33"]
    current, dismissed = snapshot.threads
    assert "CHANGES_REQUESTED" in current.body and "owner requirement" in current.body
    assert current.author == "owner" and current.path is None and current.line is None
    assert not current.resolved and not current.outdated
    assert dismissed.resolved and dismissed.outdated
    assert dismissed.updated_at == "2026-10-21T01:00:00Z"


def test_inline_threads_and_review_summaries_share_one_total_byte_bound():
    from patchington.github import GitHubClient, GitHubError
    def handle(request):
        if request.url.path.endswith("/reviews"):
            return httpx.Response(200, json=[{"id": 1, "body": "y" * 600, "state": "COMMENTED",
                "commit_id": "b" * 40, "submitted_at": "2026-10-21",
                "user": {"login": "owner", "type": "User"}}])
        if request.url.path == "/graphql":
            return httpx.Response(200, json={"data": {"repository": {"databaseId": 2,
                "pullRequest": {"reviewThreads": {"nodes": [{"id": "T1", "path": "src/a.py",
                    "line": 1, "isResolved": False, "isOutdated": False, "comments": {
                        "nodes": [{"id": "C1", "body": "x" * 600,
                                   "author": {"login": "owner"}, "updatedAt": "2026-10-21"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None}}}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
        return fixture_handler(request)
    with pytest.raises(GitHubError, match="bound"):
        GitHubClient(lambda *args: "token", transport=httpx.MockTransport(handle)).snapshot(
            policy(max_source_bytes=1024), 7)
