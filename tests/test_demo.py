import json

import httpx
import pytest


@pytest.mark.docker
@pytest.mark.filterwarnings("error:Using.*httpx.*starlette.testclient.*")
def test_actual_offline_demo_runs_intake_approval_docker_cas_ci_and_rereview(tmp_path, monkeypatch):
    from patchington.demo import run_demo

    def forbid_external_http(*args, **kwargs):
        raise AssertionError("demo must never call an external HTTP endpoint")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbid_external_http)
    output = tmp_path / "demo"
    receipt = run_demo(output)
    assert receipt["status"] == "completed"
    assert receipt["webhook"]["status_code"] == 202
    assert receipt["webhook"]["deduplicated"] is True
    assert receipt["review"]["state"] == "awaiting_approval"
    assert receipt["review_report"]["state"] == "review_reported"
    assert receipt["approval"]["status_code"] == 202
    assert receipt["publication"]["state"] == "published"
    assert receipt["remote_readback"]["actual_head"] == receipt["candidate"]["commit_sha"]
    assert receipt["remote_readback"]["base_unchanged"] is True
    assert receipt["remote_readback"]["actual_parent"] == receipt["candidate"]["parent_sha"]
    assert receipt["verification"]["passed"] is True
    assert receipt["verification"]["checks"][0]["baseline_exit"] == 1
    assert receipt["verification"]["checks"][0]["candidate_exit"] == 0
    assert receipt["verification"]["environment"].startswith("docker:")
    assert receipt["observation"]["state"] == "ci_observed_rereview_queued"
    assert receipt["ci_observations"][0]["check_runs"][0]["app"]["id"] == 42
    assert receipt["rereview"]["state"] == "review_complete"
    assert receipt["model_calls"] == ["review", "refute", "repair", "review", "refute"]
    assert receipt["simulated"] == ["model generation", "GitHub PR/review metadata", "GitHub CI result"]
    assert json.loads((output / "receipt.json").read_text()) == receipt
    assert (output / "state.db").exists()
    assert (output / "fixture-remote.git").is_dir()
    assert all(job["status"] == "completed" for job in receipt["jobs"])


def test_demo_refuses_nonempty_or_symlink_output_directory(tmp_path):
    from patchington.demo import run_demo

    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("owned")
    with pytest.raises(ValueError, match="empty"):
        run_demo(existing)
    alias = tmp_path / "alias"
    alias.symlink_to(existing, target_is_directory=True)
    with pytest.raises(ValueError):
        run_demo(alias)
    assert (existing / "keep").read_text() == "owned"


def test_fixture_review_guard_runs_immediately_before_simulated_effect(tmp_path):
    from patchington.contracts import ChangedFile, ChangeSnapshot, Finding, RepoPolicy
    from patchington.demo import LocalForge

    policy = RepoPolicy(tenant="demo", team="dev", installation_id=1, repository_id=2,
                        repo="demo/fixture", post_reviews=True)
    patch = "@@ -1 +1 @@\n-old\n+new\n"
    snapshot = ChangeSnapshot(tenant="demo", installation_id=1, repository_id=2, repo=policy.repo,
        pr=17, base_sha="a" * 40, head_sha="b" * 40, head_ref="feature", head_repo=policy.repo,
        files=(ChangedFile(path="src/x.py", patch=patch),))
    finding = Finding(path="src/x.py", line=1, severity="high", category="correctness",
        claim="Fixture defect", trigger="input", expected="expected", reason="owner fixture",
        status="supported")
    forge = LocalForge(tmp_path / "unused-local-remote")
    forge.snapshot = lambda *args: snapshot

    def rejected():
        assert forge.reviews == []
        raise PermissionError("fence lost")

    try:
        with pytest.raises(PermissionError):
            forge.post_review(policy, snapshot, (finding,), guard=rejected)
        assert forge.reviews == []
        calls = []
        forge.post_review(policy, snapshot, (finding,), guard=lambda: calls.append(len(forge.reviews)))
        assert calls == [0]
        assert len(forge.reviews) == 1
    finally:
        forge._http.close()
