import shutil
import subprocess
import sys

from patchington.artifacts import ArtifactStore
from patchington.contracts import ChangedFile, ChangeSnapshot, RepoPolicy
from patchington.execution import LocalVerifier
from patchington.gitwork import GitWorkspace
from patchington.models import FixtureModel
from patchington.pipeline import Pipeline
from patchington.probes import delivery_fee_probe
from patchington.store import Store

PATCH = """diff --git a/src/fees.py b/src/fees.py
--- a/src/fees.py
+++ b/src/fees.py
@@ -1,2 +1,2 @@
 def delivery_fee(subtotal):
-    return 0 if subtotal > 50 else 5
+    return 0 if subtotal >= 50 else 5
"""
FINDING = {
    "path": "src/fees.py",
    "line": 2,
    "severity": "high",
    "category": "correctness",
    "claim": "Delivery fee excludes the fifty boundary",
    "trigger": "subtotal=50",
    "expected": "0",
    "reason": "The trusted delivery contract says free at fifty",
}


def fixture(tmp_path, *, checks=True):
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "src").mkdir()

    def git(*args):
        return (
            subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@localhost",
                    *args,
                ]
            )
            .decode()
            .strip()
        )

    git("init", "-b", "main")
    (repo / "src/fees.py").write_text(
        "def delivery_fee(subtotal):\n    return 0 if subtotal >= 50 else 5\n"
    )
    git("add", ".")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-b", "feature")
    (repo / "src/fees.py").write_text(
        "def delivery_fee(subtotal):\n    return 0 if subtotal > 50 else 5\n"
    )
    git("add", ".")
    git("commit", "-m", "boundary defect")
    head = git("rev-parse", "HEAD")
    policy = RepoPolicy(
        tenant="acme",
        team="payments",
        installation_id=1,
        repository_id=2,
        repo="acme/shop",
        checks=(delivery_fee_probe(python=sys.executable),) if checks else (),
        approvers=("alice",),
        requirements=("Delivery is free at subtotal 50 or above; otherwise costs 5.",),
    )
    snapshot = ChangeSnapshot(
        tenant="acme",
        repository_id=2,
        installation_id=1,
        repo="acme/shop",
        pr=17,
        base_sha=base,
        head_sha=head,
        head_ref="feature",
        head_repo="acme/shop",
        files=(ChangedFile(path="src/fees.py", patch=PATCH),),
    )

    class Forge:
        def snapshot(self, p, pr):
            return snapshot

        def fetch(self, p, s, destination):
            shutil.copytree(repo, destination)
            return GitWorkspace(destination)

        def post_review(self, *args):
            raise AssertionError("publication default must be disabled")

    model = FixtureModel(
        {
            "review": {"findings": [FINDING]},
            "refute": supported_judgments,
            "repair": {"patch": PATCH, "rationale": "Restore the specified boundary"},
        }
    )
    artifacts = ArtifactStore(tmp_path / "artifacts")
    store = Store(f"sqlite:///{tmp_path}/state.db")
    store.initialize()
    return store, policy, Forge(), model, artifacts


def supported_judgments(payload):
    """Deliberately scripted source-grounded response; not real model reasoning."""
    judgments = []
    for proposal in payload["proposals"]:
        source = next(item for item in payload["context"] if item["path"] == proposal["path"])
        line = proposal["line"]
        judgments.append({
            "fingerprint": proposal["fingerprint"], "status": "supported",
            "rationale": "Source excludes fifty despite the trusted inclusive delivery contract.",
            "counterargument": "Exclusive comparison would be valid only for a different contract.",
            "citations": [{"path": source["path"], "source_digest": source["digest"],
                           "start_line": line, "end_line": line,
                           "quote": source["content"].splitlines()[line - 1]}],
        })
    return {"judgments": judgments}


def test_end_to_end_offline_repair_requires_real_independent_evidence(tmp_path):
    store, policy, forge, model, artifacts = fixture(tmp_path)
    job = store.enqueue("acme", "acme/shop", 17, "delivery-1", {"kind": "review"})
    engine = Pipeline(
        store, [policy], forge, model, LocalVerifier(trusted=True, artifacts=artifacts), artifacts
    )
    result = engine.run_once("worker-1")
    assert result["state"] == "awaiting_approval" and result["verification_passed"]
    completed = store.get(job, "acme")
    assert completed.status == "completed"
    record = store.get_record("acme", "candidate", result["candidate_id"])
    assert record["verification"]["checks"][0]["baseline_exit"] != 0
    assert record["verification"]["checks"][0]["candidate_exit"] == 0
    assert model.calls == ["review", "refute", "repair"]


def test_missing_acceptance_policy_never_reports_verified(tmp_path):
    store, policy, forge, model, artifacts = fixture(tmp_path, checks=False)
    store.enqueue("acme", "acme/shop", 17, "delivery-1", {"kind": "review"})
    result = Pipeline(
        store, [policy], forge, model, LocalVerifier(trusted=True, artifacts=artifacts), artifacts
    ).run_once("w")
    assert result["state"] == "needs_owner" and not result["verification_passed"]


def test_empty_refutation_cannot_approve_a_finding(tmp_path):
    store, policy, forge, model, artifacts = fixture(tmp_path)
    model.responses["refute"] = {"judgments": []}
    store.enqueue("acme", "acme/shop", 17, "delivery-1", {"kind": "review"})
    result = Pipeline(
        store, [policy], forge, model, LocalVerifier(trusted=True, artifacts=artifacts), artifacts
    ).run_once("w")
    assert result["state"] == "needs_owner" and "repair" not in model.calls


def test_same_generation_from_another_delivery_reuses_immutable_candidate(tmp_path):
    store, policy, forge, model, artifacts = fixture(tmp_path)
    engine = Pipeline(
        store, [policy], forge, model, LocalVerifier(trusted=True, artifacts=artifacts), artifacts
    )
    store.enqueue("acme", policy.repo, 17, "first", {})
    first = engine.run_once("worker")
    calls = list(model.calls)
    store.enqueue("acme", policy.repo, 17, "second", {})
    second = engine.run_once("worker")
    assert first["state"] == second["state"] == "awaiting_approval"
    assert first["candidate_id"] == second["candidate_id"]
    assert second["reused_generation"] and model.calls == calls


def test_model_metadata_is_filtered_and_missing_owner_context_blocks_repair(tmp_path):
    store, policy, forge, model, artifacts = fixture(tmp_path)
    snapshot = forge.snapshot(policy, 17).model_copy(
        update={"body": 'password="a_sensitive_example_password"'}
    )
    forge.snapshot = lambda *args: snapshot
    captured = []

    def review(payload):
        captured.append(payload)
        return {"findings": [FINDING]}

    model.responses["review"] = review
    store.enqueue("acme", policy.repo, 17, "first", {})
    result = Pipeline(
        store, [policy], forge, model, LocalVerifier(trusted=True, artifacts=artifacts), artifacts
    ).run_once("worker")
    assert result["state"] == "needs_owner" and "repair" not in model.calls
    assert captured[0]["snapshot"]["body"] == "[withheld: credential signature]"
    assert captured[0]["snapshot"]["context_omissions"]


def test_transient_transport_retries_but_authority_errors_stop(tmp_path):
    from patchington.github import GitHubError, GitHubTransientError

    store, policy, forge, model, artifacts = fixture(tmp_path)
    engine = Pipeline(store, [policy], forge, model, None, artifacts)

    def transient(*args):
        raise GitHubTransientError("temporary network outage")

    forge.snapshot = transient
    first = store.enqueue("acme", policy.repo, 17, "transient", {})
    assert engine.run_once("worker")["error"] == "GitHubTransientError"
    assert store.get(first, "acme").status == "queued"

    def denied(*args):
        raise GitHubError("repository authority mismatch")

    forge.snapshot = denied
    second = store.enqueue("acme", policy.repo, 18, "authority", {})
    assert engine.run_once("worker")["error"] == "GitHubError"
    assert store.get(second, "acme").status == "dead"
