"""Long-running CI polling stays bounded without premature rereview or false success."""

import pytest
from pydantic import ValidationError
from sqlalchemy import update
from test_pipeline_security import publication

from patchington.contracts import RepoPolicy
from patchington.pipeline import Pipeline
from patchington.store import jobs


def pending_observer(tmp_path, *, attempts=30):
    store, policy, forge, model, artifacts, candidate, _ = publication(
        tmp_path, required_github_checks=("build",), required_github_check_app_ids=(42,),
        ci_poll_attempts=attempts)
    forge.snapshot = lambda *args: candidate.snapshot.model_copy(update={"head_sha": candidate.commit_sha})
    forge.checks = lambda *args: {"head_sha": candidate.commit_sha, "statuses": [], "check_runs": [
        {"name": "build", "head_sha": candidate.commit_sha, "status": "in_progress",
         "conclusion": None, "app": {"id": 42}}]}
    job = store.enqueue("acme", policy.repo, 17, "pending-observe",
                        {"kind": "observe", "candidate_id": candidate.id},
                        max_attempts=policy.ci_poll_attempts, queue="observe")
    pipeline = Pipeline(store, [policy], forge, model, None, artifacts)
    return store, policy, pipeline, job


def forced_ready(store, job_id):
    # Avoid sleeping through production backoff; eligibility alone changes, never the lease/fence.
    with store.engine.begin() as connection:
        connection.execute(update(jobs).where(jobs.c.id == job_id).values(available_at=0))


def test_pending_ci_after_six_claims_remains_queued_without_rereview(tmp_path):
    store, policy, pipeline, job_id = pending_observer(tmp_path)
    try:
        for attempt in range(1, 7):
            forced_ready(store, job_id)
            result = pipeline.run_once("observer", queue="observe")
            assert result == {"state": "incomplete", "error": "Incomplete", "verification_passed": False}
            job = store.get(job_id, "acme")
            assert job.status == "queued"
            assert job.attempts == attempt
            assert job.max_attempts == 30
            assert not any(row.payload.get("after_publication") for row in store.list_jobs("acme"))
    finally:
        store.close()


def test_ci_polling_stops_at_owner_configured_attempt_bound(tmp_path):
    store, policy, pipeline, job_id = pending_observer(tmp_path, attempts=2)
    try:
        for _ in range(2):
            forced_ready(store, job_id)
            assert pipeline.run_once("observer", queue="observe")["state"] == "incomplete"
        job = store.get(job_id, "acme")
        assert job.status == "dead" and job.attempts == 2
        forced_ready(store, job_id)
        assert pipeline.run_once("observer", queue="observe") is None
        assert not any(row.payload.get("after_publication") for row in store.list_jobs("acme"))
    finally:
        store.close()


@pytest.mark.parametrize("attempts", [1, 101])
def test_ci_poll_attempt_policy_rejects_unbounded_or_trivial_values(attempts):
    with pytest.raises(ValidationError):
        RepoPolicy(tenant="demo", team="dev", installation_id=1, repository_id=2,
                   repo="demo/repo", ci_poll_attempts=attempts)


def test_publication_uses_the_owner_ci_poll_attempt_cap(tmp_path):
    store, policy, forge, model, artifacts, candidate, _ = publication(tmp_path, ci_poll_attempts=47)

    class Publisher:
        def publish(self, *args, guard=None, **kwargs):
            guard()
            return {"status": "published", "commit_sha": candidate.commit_sha,
                    "tree_sha": candidate.tree_sha, "requires_reverification": False}

    try:
        store.enqueue("acme", policy.repo, 17, "publish-for-ci",
                      {"kind": "publish", "candidate_id": candidate.id}, queue="publish")
        result = Pipeline(store, [policy], forge, model, None, artifacts,
                          publisher=Publisher()).run_once("publisher", queue="publish")
        assert result["state"] == "published"
        observers = [job for job in store.list_jobs("acme") if job.payload.get("kind") == "observe"]
        assert len(observers) == 1
        assert observers[0].max_attempts == 47
    finally:
        store.close()
