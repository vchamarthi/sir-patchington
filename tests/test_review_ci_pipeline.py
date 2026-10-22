"""CI context/report integration using fixture forge responses only."""

import json

import httpx
from test_pipeline import FINDING, fixture

from patchington.github import GitHubClient
from patchington.pipeline import Pipeline


def report(snapshot, state="failure", observed_at="2026-10-22T14:00:00Z"):
    return {"head_sha": snapshot.head_sha, "repository_id": snapshot.repository_id,
            "repo": snapshot.repo, "observed_at": observed_at, "source": "fixture",
            "state": state, "complete": True, "workflows": [], "checks": [],
            "statuses": [], "limitations": ["Synthetic CI state, no GitHub API call."]}


def engine(tmp_path):
    store, policy, forge, model, artifacts = fixture(tmp_path)
    policy = policy.model_copy(update={"post_reviews": True, "repair_enabled": False})
    forge.ci_review = lambda p, s: report(s)
    model.responses["review"] = {"findings": []}
    return store, policy, forge, model, Pipeline(store, [policy], forge, model, None,
                                              artifacts, publisher=object())


def test_clean_code_review_still_posts_the_captured_ci_summary(tmp_path):
    store, policy, forge, model, pipeline = engine(tmp_path)
    captured_payloads = []
    def review(payload):
        captured_payloads.append(payload)
        return {"findings": []}
    model.responses["review"] = review
    job = store.enqueue("acme", policy.repo, 17, "ci-review", {})
    assert pipeline.run_once("reviewer")["state"] == "review_complete"
    record = store.get_record("acme", "findings", job)
    assert record["ci"]["state"] == "failure"
    assert captured_payloads[0]["ci"]["head_sha"] == forge.snapshot(policy, 17).head_sha
    posted = []
    forge.post_review = lambda p, s, findings, **kw: posted.append((findings, kw["ci_report"])) or "1"
    assert pipeline.run_once("publisher", queue="publish")["state"] == "review_reported"
    assert posted[0][0] == () and posted[0][1] == record["ci"]


def test_changed_ci_state_invalidates_generation_but_observation_time_alone_does_not(tmp_path):
    store, policy, forge, model, pipeline = engine(tmp_path)
    store.enqueue("acme", policy.repo, 17, "ci-first", {})
    pipeline.run_once("reviewer")
    calls = list(model.calls)
    forge.ci_review = lambda p, s: report(s, observed_at="2026-10-22T14:01:00Z")
    store.enqueue("acme", policy.repo, 17, "ci-same", {})
    assert pipeline.run_once("reviewer")["reused_generation"]
    assert model.calls == calls
    forge.ci_review = lambda p, s: report(s, "success")
    store.enqueue("acme", policy.repo, 17, "ci-fixed", {})
    assert not pipeline.run_once("reviewer").get("reused_generation")
    assert len(model.calls) > len(calls)


def test_ci_from_another_commit_is_rejected_before_model_generation(tmp_path):
    store, policy, forge, model, pipeline = engine(tmp_path)
    forge.ci_review = lambda p, s: {**report(s), "head_sha": "0" * 40}
    store.enqueue("acme", policy.repo, 17, "wrong-ci-head", {})
    result = pipeline.run_once("reviewer")
    assert result["state"] == "incomplete" and model.calls == []


def test_incomplete_review_context_is_disclosed_with_findings_in_published_summary(tmp_path):
    store, policy, forge, model, pipeline = engine(tmp_path)
    snapshot = forge.snapshot(policy, 17).model_copy(update={"body": "Owner context " + "x" * 25_000})
    forge.snapshot = lambda p, pr: snapshot
    model.responses["review"] = {"findings": [FINDING]}
    job = store.enqueue("acme", policy.repo, 17, "partial-review", {})
    assert pipeline.run_once("reviewer")["state"] == "needs_owner"
    record = store.get_record("acme", "findings", job)
    assert record["findings"][0]["status"] == "supported" and record["limitations"]
    written = []

    def tokens(*args):
        return "fixture-token"

    tokens.app_id = 42

    def handle(request):
        if request.url.path.endswith("/pulls/17"):
            repo = {"id": policy.repository_id, "full_name": policy.repo}
            return httpx.Response(200, json={"number": 17, "state": "open", "draft": False,
                "changed_files": 1, "base": {"sha": snapshot.base_sha, "repo": repo},
                "head": {"sha": snapshot.head_sha, "repo": repo}})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        written.append(json.loads(request.content)["body"])
        return httpx.Response(201, json={"id": 50})

    forge.post_review = GitHubClient(tokens, transport=httpx.MockTransport(handle)).post_review
    assert pipeline.run_once("publisher", queue="publish")["state"] == "review_reported"
    body = written[0]
    assert "**Review coverage is limited.**" in body
    assert body.index("Review coverage is limited") < body.index("<details>")
    assert "<details><summary>Review limitations</summary>" in body
    assert all(limitation in body for limitation in record["limitations"])
    assert "CI status and failures" in body and FINDING["claim"] in body
