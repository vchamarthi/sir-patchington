import subprocess
import time

import pytest

from patchington.contracts import (
    Candidate,
    ChangeSnapshot,
    CheckResult,
    CheckSpec,
    RepoPolicy,
    Verification,
)


def policy(enabled=True):
    return RepoPolicy(tenant="acme", team="payments", installation_id=1, repository_id=2,
        repo="acme/shop", publish_enabled=enabled, approvers=("owner",),
        checks=(CheckSpec(name="tests", argv=("python", "-m", "pytest")),))


def candidate(p=None):
    p = p or policy()
    snapshot = ChangeSnapshot(tenant="acme", repository_id=2, installation_id=1,
        repo="acme/shop", pr=7, base_sha="a" * 40, head_sha="b" * 40,
        head_ref="repair", head_repo="acme/shop")
    return Candidate(snapshot=snapshot, patch="diff", parent_sha=snapshot.head_sha,
        tree_sha="c" * 40, commit_sha="d" * 40, timestamp=946684800, policy_digest=p.digest)


def verification(c):
    return Verification(candidate_id=c.id, passed=True,
        checks=(CheckResult(name="tests", baseline_exit=0, candidate_exit=0,
            baseline_log_digest="e" * 64, candidate_log_digest="e" * 64, passed=True),),
        evidence_digest="f" * 64, environment="docker:test-image")


def test_sign_approval_binds_every_authority_field_and_uses_unique_nonces():
    from patchington.publisher import sign_approval
    c = candidate()
    v = verification(c)
    a = sign_approval(c, v, "owner", b"s" * 32, int(time.time()) + 300)
    b = sign_approval(c, v, "owner", b"s" * 32, a.expires_at)
    assert a.candidate_id == c.id and a.policy_digest == c.policy_digest
    assert a.expected_head == c.parent_sha and a.tested_base == c.snapshot.base_sha
    assert a.evidence_digest == v.evidence_digest
    assert a.nonce != b.nonce and a.signature != b.signature
    with pytest.raises(ValueError):
        sign_approval(c, v, "owner", b"short", a.expires_at)
    with pytest.raises(ValueError):
        sign_approval(c, v, "owner", b"s" * 32, int(time.time()) - 1)


@pytest.mark.parametrize("change", [{"actor": "attacker"}, {"candidate_id": "0" * 64},
    {"evidence_digest": "0" * 64}, {"expires_at": 1}, {"tested_base": "0" * 40},
    {"expected_head": "0" * 40}, {"policy_digest": "0" * 64}, {"signature": "0" * 64}])
def test_publisher_rejects_tampering_before_accessing_forge_or_workspace(change):
    from patchington.publisher import Publisher, PublishError, sign_approval
    c = candidate()
    v = verification(c)
    approval = sign_approval(c, v, "owner", b"s" * 32, int(time.time()) + 300)
    approval = approval.model_copy(update=change)
    with pytest.raises(PublishError):
        Publisher(None, None, b"s" * 32).publish(policy(), c, v, approval, None)


def test_disabled_publishing_and_local_execution_cannot_publish():
    from patchington.publisher import Publisher, PublishError, sign_approval
    p = policy(False)
    c = candidate(p)
    v = verification(c)
    approval = sign_approval(c, v, "owner", b"s" * 32, int(time.time()) + 300)
    with pytest.raises(PublishError, match="disabled"):
        Publisher(None, None, b"s" * 32).publish(p, c, v, approval, None)
    c = candidate()
    v = verification(c).model_copy(update={"environment": "local:trusted-fixture:unsandboxed"})
    approval = sign_approval(c, v, "owner", b"s" * 32, int(time.time()) + 300)
    with pytest.raises(PublishError, match="container"):
        Publisher(None, None, b"s" * 32).publish(policy(), c, v, approval, None)


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def publication(tmp_path):
    from patchington.artifacts import ArtifactStore
    from patchington.github import GitHubClient, GitHubError, run_git
    from patchington.gitwork import GitWorkspace
    from patchington.publisher import sign_approval

    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "--quiet", "--initial-branch=main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.test")
    (source / "src").mkdir()
    (source / "src/a.py").write_text("value = 1\n")
    git(source, "add", ".")
    git(source, "commit", "--quiet", "-m", "base")
    base = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "--quiet", "-b", "repair")
    (source / "src/a.py").write_text("value = 2\n")
    git(source, "commit", "--quiet", "-am", "PR")
    head = git(source, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", "--quiet", str(source), str(remote)], check=True)
    p = policy()
    snap = ChangeSnapshot(tenant="acme", repository_id=2, installation_id=1,
        repo="acme/shop", pr=7, base_sha=base, head_sha=head,
        head_ref="repair", head_repo="acme/shop")
    patch = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-value = 2\n+value = 3\n"
    work = GitWorkspace(source)
    c = work.candidate(patch, snap, p)
    store = ArtifactStore(tmp_path / "artifacts")
    log = store.put(b"checks succeeded")
    check = CheckResult(name="tests", baseline_exit=0, candidate_exit=0,
        baseline_log_digest=log, candidate_log_digest=log, passed=True)
    evidence = {"candidate_id": c.id, "checks": [check.model_dump(mode="json")],
                "environment": "docker:test-image", "violations": []}
    v = Verification(candidate_id=c.id, passed=True, checks=(check,),
        evidence_digest=store.put_json(evidence), environment="docker:test-image")
    approval = sign_approval(c, v, "owner", b"s" * 32, int(time.time()) + 300)

    class LocalForge(GitHubClient):
        """Only this test subclass admits local file transport."""
        def __init__(self):
            super().__init__(lambda *args: "mock-token")
            self.pushes = 0
            self.ambiguous = False
            self.race = False

        def _remote(self, _policy):
            return str(remote)

        def _git(self, path, args, **kwargs):
            if "push" in args:
                self.pushes += 1
                if self.race:
                    # A collaborator wins immediately before the server's atomic ref check.
                    git(remote, "update-ref", "refs/heads/repair", base, head)
                result = run_git(path, ["-c", "protocol.file.allow=always", *args], **kwargs)
                if self.ambiguous:
                    raise GitHubError("Lost successful push response")
                return result
            return run_git(path, ["-c", "protocol.file.allow=always", *args], **kwargs)

        def snapshot(self, _policy, _pr):
            return snap.model_copy(update={"head_sha": git(remote, "rev-parse", "refs/heads/repair"),
                                           "base_sha": git(remote, "rev-parse", "refs/heads/main")})

        def _get(self, _policy, path):
            if "/git/commits/" in path:
                sha = path.rsplit("/", 1)[1]
                return {"sha": sha, "tree": {"sha": git(remote, "rev-parse", sha + "^{tree}")},
                        "parents": [{"sha": parent} for parent in
                                    git(remote, "show", "-s", "--format=%P", sha).split()]}
            raise AssertionError(path)

    return p, c, v, approval, work, store, LocalForge(), remote


def test_publisher_reconstructs_clean_candidate_and_ignores_worker_git(publication):
    from patchington.publisher import Publisher
    p, c, v, approval, work, store, forge, remote = publication
    (work.path / ".git/config").write_text("this is deliberately invalid git config\n")
    result = Publisher(forge, store, b"s" * 32).publish(p, c, v, approval, work)
    assert result["status"] == "published"
    assert git(remote, "rev-parse", "repair") == c.commit_sha
    assert result["tree_sha"] == c.tree_sha and forge.pushes == 1
    result = Publisher(forge, store, b"s" * 32).publish(p, c, v, approval, work)
    assert result["status"] == "reconciled" and forge.pushes == 1


def test_publisher_reconciles_ambiguous_success_without_duplicate_writes(publication):
    from patchington.publisher import Publisher
    p, c, v, approval, work, store, forge, remote = publication
    forge.ambiguous = True
    result = Publisher(forge, store, b"s" * 32).publish(p, c, v, approval, work)
    assert result["status"] == "reconciled"
    assert git(remote, "rev-parse", "repair") == c.commit_sha and forge.pushes == 1


def test_expected_old_head_race_cannot_overwrite_collaborator(publication):
    from patchington.publisher import Publisher, PublishError
    p, c, v, approval, work, store, forge, remote = publication
    forge.race = True
    with pytest.raises(PublishError, match="head|rejected"):
        Publisher(forge, store, b"s" * 32).publish(p, c, v, approval, work)
    assert git(remote, "rev-parse", "repair") == c.snapshot.base_sha


def test_changed_base_or_corrupt_evidence_prevents_push(publication):
    from patchington.publisher import Publisher, PublishError
    p, c, v, approval, work, store, forge, remote = publication
    git(remote, "update-ref", "refs/heads/main", c.parent_sha)
    with pytest.raises(PublishError, match="changed"):
        Publisher(forge, store, b"s" * 32).publish(p, c, v, approval, work)
    assert forge.pushes == 0
    git(remote, "update-ref", "refs/heads/main", c.snapshot.base_sha)
    artifact = store._path(v.evidence_digest)
    artifact.chmod(0o600)
    artifact.write_text("corrupt")
    with pytest.raises(PublishError, match="artifacts"):
        Publisher(forge, store, b"s" * 32).publish(p, c, v, approval, work)
    assert forge.pushes == 0


def test_expiry_is_rechecked_after_clean_reconstruction(publication, monkeypatch):
    from patchington.publisher import Publisher, PublishError
    p, c, v, approval, work, store, forge, remote = publication
    now = iter([approval.expires_at - 1, approval.expires_at + 1])
    monkeypatch.setattr("patchington.publisher.time.time", lambda: next(now))
    with pytest.raises(PublishError, match="expired"):
        Publisher(forge, store, b"s" * 32).publish(p, c, v, approval, work)
    assert forge.pushes == 0


def test_pr_eligibility_is_rechecked_at_the_write_boundary(publication):
    from patchington.github import GitHubError
    from patchington.publisher import Publisher, PublishError
    p, c, v, approval, work, store, forge, remote = publication
    original = forge.snapshot
    calls = 0
    def read(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GitHubError("PR is closed")
        return original(*args)
    forge.snapshot = read
    with pytest.raises(PublishError, match="closed"):
        Publisher(forge, store, b"s" * 32).publish(p, c, v, approval, work)
    assert forge.pushes == 0


def test_remote_tree_readback_mismatch_is_not_reported_as_success(publication):
    from patchington.publisher import Publisher, PublishError
    p, c, v, approval, work, store, forge, remote = publication
    original = forge._get
    def read(*args):
        result = original(*args)
        result["tree"]["sha"] = "0" * 40
        return result
    forge._get = read
    with pytest.raises(PublishError, match="readback"):
        Publisher(forge, store, b"s" * 32).publish(p, c, v, approval, work)
    assert git(remote, "rev-parse", "repair") == c.commit_sha


def test_boundary_revalidates_crafted_models_that_bypass_pydantic_validators():
    from patchington.publisher import Publisher, PublishError, sign_approval
    c = candidate()
    original = verification(c)
    approval = sign_approval(c, original, "owner", b"s" * 32, int(time.time()) + 300)
    crafted = original.model_copy(update={"checks": (), "passed": True})
    with pytest.raises(PublishError, match="contract"):
        Publisher(None, None, b"s" * 32).publish(policy(), c, crafted, approval, None)


def test_expired_queue_lease_guard_prevents_every_remote_effect(publication):
    from patchington.publisher import Publisher
    p, c, v, approval, work, store, forge, remote = publication
    def guard():
        raise PermissionError("queue fence is no longer owned")
    with pytest.raises(PermissionError, match="fence"):
        Publisher(forge, store, b"s" * 32).publish(p, c, v, approval, work, guard=guard)
    assert forge.pushes == 0
    assert git(remote, "rev-parse", "repair") == c.parent_sha


def test_queue_guard_is_rechecked_after_forge_reads_immediately_before_push(publication):
    from patchington.publisher import Publisher
    p, c, v, approval, work, store, forge, remote = publication
    calls = 0
    def guard():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PermissionError("queue fence changed during forge reads")
    with pytest.raises(PermissionError, match="forge"):
        Publisher(forge, store, b"s" * 32).publish(p, c, v, approval, work, guard=guard)
    assert calls == 3 and forge.pushes == 0


def test_transient_remote_readback_failure_can_retry_by_reconciling_without_a_second_push(publication):
    from patchington.github import GitHubTransientError
    from patchington.publisher import Publisher, PublishTransientError
    p, c, v, approval, work, store, forge, remote = publication
    original = forge._get
    failures = 1
    def read(*args):
        nonlocal failures
        if failures:
            failures -= 1
            raise GitHubTransientError("readback temporarily unavailable")
        return original(*args)
    forge._get = read
    publisher = Publisher(forge, store, b"s" * 32)
    with pytest.raises(PublishTransientError):
        publisher.publish(p, c, v, approval, work)
    assert git(remote, "rev-parse", "repair") == c.commit_sha and forge.pushes == 1
    result = publisher.publish(p, c, v, approval, work)
    assert result["status"] == "reconciled" and forge.pushes == 1


def test_failed_push_with_confirmed_unchanged_head_retries_to_exactly_one_new_commit(publication):
    from patchington.github import GitOperationError
    from patchington.publisher import Publisher, PublishTransientError
    p, c, v, approval, work, store, forge, remote = publication
    original = forge._git
    attempts = 0
    def operation(path, args, **kwargs):
        nonlocal attempts
        if "push" in args:
            attempts += 1
            if attempts == 1:
                raise GitOperationError("connection failed before remote update")
        return original(path, args, **kwargs)
    forge._git = operation
    publisher = Publisher(forge, store, b"s" * 32)
    with pytest.raises(PublishTransientError):
        publisher.publish(p, c, v, approval, work)
    assert git(remote, "rev-parse", "repair") == c.parent_sha
    assert publisher.publish(p, c, v, approval, work)["status"] == "published"
    assert git(remote, "rev-list", "--count", f"{c.parent_sha}..repair") == "1"
    assert publisher.publish(p, c, v, approval, work)["status"] == "reconciled"
    assert attempts == 2 and forge.pushes == 1
