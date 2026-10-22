import json

import httpx
import pytest

from patchington.contracts import ChangedFile, ChangeSnapshot, RepoPolicy
from patchington.github import AppTokenProvider, GitHubClient, GitHubError, SplitTokenProvider

SHA = "b" * 40


def policy(**changes):
    return RepoPolicy(tenant="acme", team="payments", installation_id=1, repository_id=2,
                      repo="acme/shop", **changes)


def snapshot():
    return ChangeSnapshot(tenant="acme", repository_id=2, installation_id=1, repo="acme/shop",
                          pr=7, base_sha="a" * 40, head_sha=SHA, head_ref="repair",
                          head_repo="acme/shop", files=(ChangedFile(path="src/a.py"),))


def workflow(**changes):
    result = {"id": 10, "head_sha": SHA, "name": "CI", "status": "completed",
              "conclusion": "failure", "repository": {"id": 2, "full_name": "acme/shop"}}
    return result | changes


def job(**changes):
    return {"id": 20, "run_id": 10, "head_sha": SHA, "name": "pytest", "status": "completed",
            "conclusion": "failure", "steps": [{"number": 1, "name": "Run tests",
                "status": "completed", "conclusion": "failure"}]} | changes


def handler(request):
    assert request.method == "GET"
    path = request.url.path
    if path == "/repos/acme/shop":
        return httpx.Response(200, json={"id": 2, "full_name": "acme/shop"})
    if path.endswith("/check-runs"):
        return httpx.Response(200, json={"total_count": 1, "check_runs": [{
            "id": 1, "head_sha": SHA, "name": "pytest", "status": "completed",
            "conclusion": "failure", "output": {"summary": "AssertionError: 1 != 2"}}]})
    if path.endswith("/statuses"):
        return httpx.Response(200, json=[{"id": 2, "context": "external", "state": "success"}])
    if path.endswith("/actions/runs"):
        assert request.url.params["head_sha"] == SHA
        return httpx.Response(200, json={"total_count": 1, "workflow_runs": [workflow()]})
    if path.endswith("/runs/10/jobs"):
        return httpx.Response(200, json={"total_count": 1, "jobs": [job()]})
    if path.endswith("/jobs/20/logs"):
        return httpx.Response(200, text="normal setup\nAssertionError: 1 != 2\nProcess exit code 1")
    raise AssertionError(str(request.url))


def client(handle=handler, tokens=None):
    return GitHubClient(tokens or (lambda *args: "read-token"), transport=httpx.MockTransport(handle))


def test_ci_review_collects_exact_head_workflows_jobs_steps_and_bounded_failure_evidence():
    report = client().ci_review(policy(), snapshot())
    assert report["head_sha"] == SHA
    assert report["state"] == "failure" and report["complete"] is True
    assert report["source"] == "github_rest" and report["observed_at"].endswith("+00:00")
    failed = report["workflows"][0]["jobs"][0]
    assert failed["steps"][0]["conclusion"] == "failure"
    assert "Run tests" in failed["failure_reason"]
    assert "AssertionError" in failed["log"]["excerpt"]
    assert failed["diagnosis"]["label"] == "observed failure"
    assert failed["diagnosis"]["next_actions"]


@pytest.mark.parametrize("part", ["workflow", "job", "check", "repository"])
def test_ci_identity_mismatch_is_unavailable_and_never_green(part):
    def handle(request):
        response = handler(request)
        data = response.json() if not request.url.path.endswith("/logs") else None
        if part == "workflow" and request.url.path.endswith("/actions/runs"):
            data["workflow_runs"][0]["head_sha"] = "c" * 40
        if part == "job" and request.url.path.endswith("/jobs"):
            data["jobs"][0]["run_id"] = 99
        if part == "check" and request.url.path.endswith("/check-runs"):
            data["check_runs"][0]["head_sha"] = "c" * 40
        if part == "repository" and request.url.path == "/repos/acme/shop":
            data["id"] = 99
        return httpx.Response(200, json=data) if data is not None else response
    report = client(handle).ci_review(policy(), snapshot())
    assert not report["complete"] and report["state"] != "success"
    assert report["limitations"]
    if part == "repository":
        assert report["state"] == "unavailable"


def test_ci_partial_or_permission_denied_does_not_block_collection_or_report_success():
    def handle(request):
        if request.url.path.endswith("/actions/runs"):
            return httpx.Response(403, json={"message": "secret must not be echoed"})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    assert report["state"] != "success" and not report["complete"]
    assert report["statuses"][0]["state"] == "success"
    assert "secret must not be echoed" not in str(report)


def test_ci_empty_and_skipped_results_are_unknown_not_executed_success():
    def handle(request):
        if request.url.path.endswith("/actions/runs"):
            return httpx.Response(200, json={"total_count": 0, "workflow_runs": []})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if request.url.path.endswith("/statuses"):
            return httpx.Response(200, json=[])
        return handler(request)
    assert client(handle).ci_review(policy(), snapshot())["state"] == "unknown"


def test_ci_uses_latest_status_for_each_context():
    def handle(request):
        if request.url.path.endswith("/actions/runs"):
            return httpx.Response(200, json={"total_count": 0, "workflow_runs": []})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if request.url.path.endswith("/statuses"):
            return httpx.Response(200, json=[{"id": 4, "context": "test", "state": "pending"},
                {"id": 3, "context": "test", "state": "failure"}])
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    assert report["state"] == "pending" and len(report["statuses"]) == 1


def test_ci_unsafe_pagination_and_log_redirects_never_leak_tokens():
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/jobs/20/logs"):
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/secret"})
        if request.url.path.endswith("/statuses"):
            return httpx.Response(200, json=[], headers={
                "Link": '<https://evil.test/statuses>; rel="next"'})
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    assert all(request.url.host == "api.github.com" for request in calls)
    assert not report["complete"]
    assert report["workflows"][0]["jobs"][0]["log"]["state"] == "unavailable"


def test_ci_log_secret_lines_are_wholly_withheld_and_untrusted_html_is_escaped():
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    def handle(request):
        if request.url.path.endswith("/jobs/20/logs"):
            return httpx.Response(200, text="error: token " + secret)
        if request.url.path.endswith("/runs/10/jobs"):
            return httpx.Response(200, json={"total_count": 1, "jobs": [job(
                name="</details><img src=x onerror=alert(1)> @owner")]})
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    from patchington.ci import render_ci_summary
    rendered = render_ci_summary(report)
    assert secret not in str(report) + rendered
    assert rendered.startswith("<details><summary>CI status and failures</summary>")
    assert rendered.count("</details>") == 1 and "<img" not in rendered
    assert "Next action" in rendered and "observed failure" in rendered


def test_ci_scoped_token_is_used_without_broadening_general_read_token():
    class Tokens:
        def __call__(self, *args):
            raise AssertionError("CI must use the CI purpose token")

        def ci_token(self, installation, repository):
            assert (installation, repository) == (1, 2)
            return "ci-only"
    def handle(request):
        assert request.headers["Authorization"] == "Bearer ci-only"
        return handler(request)
    assert client(handle, Tokens()).ci_review(policy(), snapshot())["state"] == "failure"


def test_ci_token_permissions_and_split_provider_forward_to_read_app():
    minted = []
    provider = object.__new__(AppTokenProvider)
    provider._mint = lambda installation, repo, permissions: minted.append(
        (installation, repo, permissions)) or "ci"
    assert provider.ci_token(1, 2) == "ci"
    assert minted == [(1, 2, {"actions": "read", "checks": "read", "statuses": "read",
                              "metadata": "read"})]
    split = SplitTokenProvider(provider, provider, {(1, 2): 99})
    assert split.ci_token(1, 2) == "ci" and minted[-1][0] == 1


def test_post_review_ci_report_revision_is_checked_before_any_request():
    with pytest.raises(GitHubError, match="CI.*commit"):
        client(lambda request: pytest.fail("must not request")).post_review(
            policy(post_reviews=True), snapshot(), (), ci_report={"head_sha": "c" * 40})


def test_post_review_adds_collapsed_ci_even_without_findings():
    written = []
    def tokens(*args):
        return "token"
    tokens.app_id = 42
    def handle(request):
        if request.url.path.endswith("/pulls/7"):
            repo = {"id": 2, "full_name": "acme/shop"}
            return httpx.Response(200, json={"number": 7, "state": "open", "draft": False,
                "changed_files": 1, "base": {"sha": "a" * 40, "repo": repo},
                "head": {"sha": SHA, "repo": repo}})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        written.append(json.loads(request.content)["body"])
        return httpx.Response(201, json={"id": 50})
    report = {"head_sha": SHA, "state": "unknown", "observed_at": "2026-10-22T12:00:00+00:00",
              "source": "fixture", "workflows": [], "checks": [], "statuses": [],
              "limitations": ["No checks observed"], "complete": True}
    assert client(handle, tokens).post_review(
        policy(post_reviews=True), snapshot(), (), ci_report=report) == "50"
    assert "<details><summary>CI status and failures</summary>" in written[0]


@pytest.mark.parametrize(("excerpt", "conclusion", "cause", "action"), [
    ("ModuleNotFoundError: No module named 'x'", "failure", "Missing Python", "dependency"),
    ("AssertionError: expected one", "failure", "assertion", "named failing test"),
    ("src/a.py:1:2: F821 Undefined name `x`", "failure", "static-analysis", "same linter"),
    ("SyntaxError: unexpected indent", "failure", "syntax", "same parser"),
    ("error TS2322: Type string is not assignable", "failure", "type incompatibility", "types"),
    ("", "timed_out", "timeout", "blocking"),
    ("", "cancelled", "cancelled", "concurrency"),
    ("error: No space left on device", "failure", "Runner storage", "runner capacity"),
])
def test_diagnosis_uses_concrete_error_evidence_for_targeted_next_actions(
        excerpt, conclusion, cause, action):
    def handle(request):
        if request.url.path.endswith("/jobs/20/logs"):
            return httpx.Response(200, text=excerpt)
        if request.url.path.endswith("/runs/10/jobs"):
            return httpx.Response(200, json={"total_count": 1,
                                            "jobs": [job(conclusion=conclusion)]})
        return handler(request)
    diagnosis = client(handle).ci_review(policy(), snapshot())["workflows"][0]["jobs"][0]["diagnosis"]
    assert cause in diagnosis["possible_cause"]
    assert action in " ".join(diagnosis["next_actions"])


def test_signed_log_download_has_no_app_auth_or_cookies_and_does_not_follow_again():
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.host == "logs.blob.core.windows.net":
            assert "authorization" not in request.headers and "cookie" not in request.headers
            return httpx.Response(200, text="AssertionError: observed failure")
        if request.url.path.endswith("/jobs/20/logs"):
            return httpx.Response(302, headers={
                "Location": "https://logs.blob.core.windows.net/job?sig=opaque",
                "Set-Cookie": "private=credential"})
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    assert report["workflows"][0]["jobs"][0]["log"]["state"] == "available"
    assert len([request for request in calls if request.url.host != "api.github.com"]) == 1
    assert "sig=opaque" not in str(report)


def test_signed_log_download_requests_bounded_tail_and_preserves_final_failure():
    tail = "finishing cleanup\nAssertionError: failure at end of long log"
    size = len(tail.encode())
    def handle(request):
        if request.url.host == "logs.blob.core.windows.net":
            assert request.headers["Range"] == "bytes=-64000"
            assert "authorization" not in request.headers and "cookie" not in request.headers
            return httpx.Response(206, text=tail, headers={
                "Content-Range": f"bytes {200_000 - size}-{199_999}/200000"})
        if request.url.path.endswith("/jobs/20/logs"):
            return httpx.Response(302, headers={
                "Location": "https://logs.blob.core.windows.net/job?sig=opaque"})
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    log = report["workflows"][0]["jobs"][0]["log"]
    assert log["state"] == "available" and "failure at end" in log["excerpt"]
    assert log["scope"] == "job_log_tail" and log["truncated"] is True
    from patchington.ci import render_ci_summary
    assert "tail" in render_ci_summary(report)


def test_signed_backend_ignoring_range_cannot_return_oversized_full_log():
    from patchington.ci import MAX_LOG_BYTES
    def handle(request):
        if request.url.host == "logs.blob.core.windows.net":
            assert request.headers["Range"] == "bytes=-64000"
            assert "authorization" not in request.headers
            return httpx.Response(200, text="error: " + "A" * MAX_LOG_BYTES)
        if request.url.path.endswith("/jobs/20/logs"):
            return httpx.Response(302, headers={
                "Location": "https://logs.blob.core.windows.net/job?sig=opaque"})
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    assert report["workflows"][0]["jobs"][0]["log"] == {"state": "unavailable", "excerpt": ""}


@pytest.mark.parametrize("location", [
    "http://logs.blob.core.windows.net/job", "https://logs.blob.core.windows.net.evil.test/job",
    "https://user@logs.blob.core.windows.net/job", "https://logs.blob.core.windows.net:444/job",
    "https://logs.blob.core.windows.net/job#secret", "https://127.0.0.1/job",
])
def test_unqualified_log_hosts_are_rejected_without_external_request(location):
    def handle(request):
        assert request.url.host == "api.github.com"
        if request.url.path.endswith("/jobs/20/logs"):
            return httpx.Response(302, headers={"Location": location})
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    assert report["workflows"][0]["jobs"][0]["log"]["state"] == "unavailable"


def test_signed_download_does_not_follow_a_second_redirect():
    def handle(request):
        assert request.url.host != "evil.test"
        if request.url.host == "logs.actions.githubusercontent.com":
            assert "authorization" not in request.headers
            return httpx.Response(302, headers={"Location": "https://evil.test/steal"})
        if request.url.path.endswith("/jobs/20/logs"):
            return httpx.Response(302, headers={
                "Location": "https://logs.actions.githubusercontent.com/job?sig=opaque"})
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    assert report["workflows"][0]["jobs"][0]["log"]["state"] == "unavailable"


def test_download_byte_budget_is_enforced_before_retaining_log_excerpt():
    from patchington.ci import MAX_LOG_BYTES
    def handle(request):
        if request.url.path.endswith("/jobs/20/logs"):
            return httpx.Response(200, text="error: " + "A" * MAX_LOG_BYTES)
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    assert report["workflows"][0]["jobs"][0]["log"] == {"state": "unavailable", "excerpt": ""}
    assert not report["complete"]


@pytest.mark.parametrize("part", ["output", "repository", "steps"])
def test_malformed_nested_ci_fields_become_partial_observations(part):
    def handle(request):
        if part == "output" and request.url.path.endswith("/check-runs"):
            body = handler(request).json()
            body["check_runs"][0]["output"] = "malformed"
            return httpx.Response(200, json=body)
        if part == "output" and request.url.path.endswith("/annotations"):
            return httpx.Response(200, json=[])
        if part == "repository" and request.url.path.endswith("/actions/runs"):
            return httpx.Response(200, json={"total_count": 1,
                "workflow_runs": [workflow(repository="malformed")]})
        if part == "steps" and request.url.path.endswith("/runs/10/jobs"):
            return httpx.Response(200, json={"total_count": 1, "jobs": [job(steps=["malformed"])]})
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    assert not report["complete"] and report["state"] != "success"


def test_missing_check_output_uses_bounded_sanitized_failure_annotations():
    def handle(request):
        if request.url.path.endswith("/check-runs"):
            body = handler(request).json()
            body["check_runs"][0]["output"] = {}
            return httpx.Response(200, json=body)
        if request.url.path.endswith("/annotations"):
            return httpx.Response(200, json=[{"path": "src/a.py", "annotation_level": "failure",
                                              "message": "error: inconsistent return type"}])
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    assert "inconsistent return type" in report["checks"][0]["evidence"]


def test_ci_model_context_prioritizes_failures_and_cannot_exceed_prompt_budget():
    from patchington.ci import model_ci_context
    report = {"head_sha": SHA, "state": "failure", "complete": True, "source": "fixture",
              "workflows": [], "statuses": [], "checks": [
                  {"id": number + 1, "name": "checks", "status": "completed",
                   "conclusion": "success" if number < 99 else "failure", "evidence": "é" * 10_000}
                  for number in range(100)]}
    result = model_ci_context(report, budget=2_000)
    assert result["head_sha"] == SHA and result["state"] == "failure"
    assert result["checks"][0]["conclusion"] == "failure"
    assert result["omitted_records"] > 0 and result["truncated"]
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 2_000


def test_old_failed_run_is_superseded_only_for_same_workflow_event_and_branch():
    def handle(request):
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if request.url.path.endswith("/actions/runs"):
            common = {"workflow_id": 50, "event": "pull_request", "head_branch": "repair"}
            return httpx.Response(200, json={"total_count": 2, "workflow_runs": [
                workflow(**common, run_number=2, conclusion="success"),
                workflow(**common, id=9, run_number=1)]})
        if request.url.path.endswith("/runs/10/jobs"):
            return httpx.Response(200, json={"total_count": 1, "jobs": [job(conclusion="success")]})
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    assert report["state"] == "success" and report["superseded_runs"] == [9]
    assert [run["id"] for run in report["workflows"]] == [10]


@pytest.mark.parametrize("distinct", [{"workflow_id": 51}, {"event": "push"},
                                       {"head_branch": "other"}])
def test_distinct_workflow_event_or_branch_failure_is_never_superseded(distinct):
    def handle(request):
        common = {"workflow_id": 50, "event": "pull_request", "head_branch": "repair"}
        if request.url.path.endswith("/actions/runs"):
            return httpx.Response(200, json={"total_count": 2, "workflow_runs": [
                workflow(**common, run_number=2, conclusion="success"),
                workflow(**(common | distinct), id=9, run_number=1)]})
        if request.url.path.endswith("/runs/9/jobs"):
            return httpx.Response(200, json={"total_count": 1, "jobs": [job(id=19, run_id=9)]})
        if request.url.path.endswith("/jobs/19/logs"):
            return httpx.Response(200, text="error: distinct workflow failed")
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    assert report["state"] == "failure" and report["superseded_runs"] == []
    assert len(report["workflows"]) == 2


def test_missing_pagination_and_request_bounds_produce_partial_observations(monkeypatch):
    from patchington import ci
    monkeypatch.setattr(ci, "MAX_REQUESTS", 3)
    report = client().ci_review(policy(), snapshot())
    assert not report["complete"] and report["state"] != "success"


def test_ci_response_stream_cannot_extend_past_time_bound(monkeypatch):
    from patchington import ci
    now = [0]
    monkeypatch.setattr(ci.time, "monotonic", lambda: now[0])
    class DelayedStream(httpx.SyncByteStream):
        def __iter__(self):
            now[0] = 100
            yield b"error: late data"
    def handle(request):
        if request.url.path.endswith("/jobs/20/logs"):
            return httpx.Response(200, stream=DelayedStream())
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    assert report["workflows"][0]["jobs"][0]["log"]["state"] == "unavailable"
    assert not report["complete"]


def test_control_sequences_cannot_hide_a_credential_from_filtering():
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    def handle(request):
        if request.url.path.endswith("/jobs/20/logs"):
            return httpx.Response(200, text="error: " + secret[:10] + "\x1b[31m" + secret[10:])
        return handler(request)
    report = client(handle).ci_review(policy(), snapshot())
    from patchington.ci import render_ci_summary
    assert secret not in str(report) + render_ci_summary(report)
