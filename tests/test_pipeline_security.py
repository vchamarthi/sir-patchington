import json
import time

import pytest
from test_api_approvals import completed_candidate
from test_pipeline import FINDING, fixture

from patchington.contracts import Candidate, Verification
from patchington.execution import LocalVerifier
from patchington.pipeline import Pipeline
from patchington.publisher import sign_approval


def publication(tmp_path, **policy_updates):
    store, policy, forge, model, artifacts, candidate_id, source_job = completed_candidate(
        tmp_path, **policy_updates)
    record = store.get_record("acme", "candidate", candidate_id)
    candidate = Candidate.model_validate_json(json.dumps(record["candidate"]))
    verification = Verification.model_validate_json(json.dumps(record["verification"]))
    approval = sign_approval(candidate, verification, "alice", b"s" * 32, int(time.time()) + 300)
    store.put_record("acme", "approval", candidate_id, approval.model_dump(mode="json"))
    return store, policy, forge, model, artifacts, candidate, source_job


def test_approval_revoked_during_reconstruction_cannot_reach_remote_effect(tmp_path):
    store, policy, forge, model, artifacts, candidate, _ = publication(tmp_path)
    remote_effects = []
    class Publisher:
        def publish(self, *args, guard=None, **kwargs):
            store.put_record("acme", "approval", candidate.id, {"rejected": True})
            guard()
            remote_effects.append("push")
            return {"status": "published"}
    store.enqueue("acme", policy.repo, 17, "publication", {"kind": "publish",
                   "candidate_id": candidate.id}, queue="publish")
    result = Pipeline(store, [policy], forge, model, None, artifacts,
                      publisher=Publisher()).run_once("publisher", queue="publish")
    assert result["state"] == "incomplete"
    assert remote_effects == []
    assert store.get_record("acme", "publication", candidate.id) is None


def test_publication_job_pr_must_match_the_approved_candidate(tmp_path):
    store, policy, forge, model, artifacts, candidate, _ = publication(tmp_path)
    class Publisher:
        def publish(self, *args, **kwargs):
            pytest.fail("publisher called for a mismatched queue PR identity")
    store.enqueue("acme", policy.repo, 18, "publication", {"kind": "publish",
                   "candidate_id": candidate.id}, queue="publish")
    result = Pipeline(store, [policy], forge, model, None, artifacts,
                      publisher=Publisher()).run_once("publisher", queue="publish")
    assert result["state"] == "incomplete"


def test_review_report_cannot_borrow_uncompleted_or_different_pr_evidence(tmp_path):
    store, policy, forge, model, artifacts, candidate, _ = publication(tmp_path)
    source = store.enqueue("acme", policy.repo, 18, "unfinished-source", {})
    store.put_record("acme", "findings", source, {"snapshot": candidate.snapshot.model_dump(mode="json"),
                     "findings": [{**FINDING, "status": "supported"}]})
    store.enqueue("acme", policy.repo, 17, "report", {"kind": "report", "source_job": source},
                   queue="publish")
    calls = []
    forge.post_review = lambda *args, **kwargs: calls.append(args) or "comment"
    result = Pipeline(store, [policy], forge, model, None, artifacts,
                      publisher=object()).run_once("publisher", queue="publish")
    assert result["state"] == "incomplete" and calls == []


@pytest.mark.parametrize("apps,allowed", [((999,), False), ((42, 999), False),
                                         ((42, 42), False), ((42,), True)])
def test_ci_observation_requires_one_current_check_from_a_pinned_app(tmp_path, apps, allowed):
    store, policy, forge, model, artifacts, candidate, _ = publication(tmp_path,
        required_github_checks=("build",), required_github_check_app_ids=(42,))
    forge.snapshot = lambda *args: candidate.snapshot.model_copy(update={"head_sha": candidate.commit_sha})
    forge.checks = lambda *args: {"head_sha": candidate.commit_sha, "statuses": [], "check_runs": [
        {"id": index, "name": "build", "head_sha": candidate.commit_sha, "status": "completed",
         "conclusion": "success", "app": {"id": app}} for index, app in enumerate(apps)]}
    store.enqueue("acme", policy.repo, 17, "observe", {"kind": "observe", "candidate_id": candidate.id},
                   queue="observe")
    result = Pipeline(store, [policy], forge, model, None, artifacts).run_once("observer", queue="observe")
    assert (result["state"] == "ci_observed_rereview_queued") is allowed
    rereviews = [job for job in store.list_jobs("acme") if job.payload.get("after_publication")]
    assert bool(rereviews) is allowed


def test_repeat_delivery_reuses_a_completed_generation_without_rewriting_candidate_evidence(tmp_path):
    store, policy, forge, model, artifacts, candidate_id, _ = completed_candidate(tmp_path)
    before = store.get_record("acme", "candidate", candidate_id)
    model_calls = list(model.calls)
    store.enqueue("acme", policy.repo, 17, "duplicate-generation", {"kind": "review"})
    result = Pipeline(store, [policy], forge, model,
        LocalVerifier(trusted=True, artifacts=artifacts), artifacts).run_once("second-worker")
    assert result["state"] == "awaiting_approval" and result["candidate_id"] == candidate_id
    assert result["reused_generation"]
    assert model.calls == model_calls
    assert store.get_record("acme", "candidate", candidate_id) == before


def test_new_generation_with_identical_candidate_reconciles_immutable_existing_evidence(tmp_path):
    store, policy, forge, model, artifacts, candidate_id, _ = completed_candidate(tmp_path)
    before = store.get_record("acme", "candidate", candidate_id)
    candidate = Candidate.model_validate_json(json.dumps(before["candidate"]))
    proof = Verification.model_validate_json(json.dumps(before["verification"]))
    approval = sign_approval(candidate, proof, "alice", b"s" * 32, int(time.time()) + 300)
    store.put_record("acme", "approval", candidate_id, approval.model_dump(mode="json"))
    model.name = "offline-fixture-new-generation"
    next_job = store.enqueue("acme", policy.repo, 17, "new-generation", {"kind": "review"})
    result = Pipeline(store, [policy], forge, model,
        LocalVerifier(trusted=True, artifacts=artifacts), artifacts).run_once("new-worker")
    assert result["state"] == "awaiting_approval" and result["candidate_id"] == candidate_id
    assert store.get_record("acme", "candidate", candidate_id) == before
    current = store.get_record("acme", "candidate-current", candidate_id)
    assert current["job_id"] == next_job
    assert current["candidate"] == before["candidate"]
    assert store.get_record("acme", "verification", current["verification"]["evidence_digest"])
    assert store.get_record("acme", "approval", candidate_id).get("superseded")


def test_pipeline_model_payload_never_reintroduces_excluded_secret_patch_or_source(tmp_path):
    from patchington.contracts import ChangedFile
    store, policy, forge, model, artifacts = fixture(tmp_path)
    original = forge.snapshot(policy, 17)
    secret = "SYNTHETIC_ENV_FILE_CONTENT"
    forge.snapshot = lambda *args: original.model_copy(update={"files": original.files + (
        ChangedFile(path="src/.env", patch=secret),)})
    payloads = []
    def review(payload):
        payloads.append(payload)
        return {"findings": []}
    model.responses["review"] = review
    model.responses["refute"] = review
    store.enqueue("acme", policy.repo, 17, "omitted-source", {"kind": "review"})
    result = Pipeline(store, [policy], forge, model,
        LocalVerifier(trusted=True, artifacts=artifacts), artifacts).run_once("worker")
    assert result["state"] == "needs_owner"
    assert payloads and secret not in json.dumps(payloads)
    assert "repair" not in model.calls


def test_withheld_owner_feedback_prevents_automatic_repair(tmp_path):
    store, policy, forge, model, artifacts = fixture(tmp_path)
    original = forge.snapshot(policy, 17)
    token = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    forge.snapshot = lambda *args: original.model_copy(update={"body": "Owner says " + token})
    store.enqueue("acme", policy.repo, 17, "omitted-feedback", {"kind": "review"})
    result = Pipeline(store, [policy], forge, model,
        LocalVerifier(trusted=True, artifacts=artifacts), artifacts).run_once("worker")
    assert result["state"] == "needs_owner" and "repair" not in model.calls


def test_cached_generation_cannot_leave_active_proof_bound_to_a_dead_reproof_job(tmp_path):
    store, policy, forge, model, artifacts, candidate_id, _ = completed_candidate(tmp_path)
    interrupted = store.enqueue("acme", policy.repo, 17, "interrupted-reproof", {"kind": "review"})
    lease = store.claim("interrupted-worker")
    association = store.get_record("acme", "candidate-current", candidate_id)
    association["job_id"] = interrupted
    assert store.put_job_record(interrupted, "interrupted-worker", lease.fence,
                                "candidate-current", candidate_id, association)
    assert store.fail(interrupted, "interrupted-worker", lease.fence, "interrupted", retry=False)
    store.enqueue("acme", policy.repo, 17, "recover-after-interruption", {"kind": "review"})
    result = Pipeline(store, [policy], forge, model,
        LocalVerifier(trusted=True, artifacts=artifacts), artifacts).run_once("recovery-worker")
    assert result["state"] == "awaiting_approval"
    current = store.get_record("acme", "candidate-current", candidate_id)
    assert store.get(current["job_id"], "acme").status == "completed"
