import os
import subprocess

import pytest

from patchington.contracts import ChangeSnapshot, RepoPolicy


def git(path, *args, data=None):
    return subprocess.run(["git", "-C", str(path), *args], input=data, capture_output=True,
                          check=True).stdout.decode().strip()


def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "src").mkdir()
    (repo / "src/calc.py").write_text("VALUE = 1\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=fixture", "-c", "user.email=f@example.com", "commit", "-qm", "base")
    head = git(repo, "rev-parse", "HEAD")
    policy = RepoPolicy(tenant="acme", team="dev", installation_id=1, repository_id=2,
                        repo="acme/repo")
    snapshot = ChangeSnapshot(tenant="acme", installation_id=1, repository_id=2, repo="acme/repo",
                              pr=1, base_sha=head, head_sha=head, head_ref="fix", head_repo="acme/repo")
    return repo, policy, snapshot


PATCH = """diff --git a/src/calc.py b/src/calc.py
--- a/src/calc.py
+++ b/src/calc.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""


def test_candidate_is_pinned_deterministic_and_does_not_touch_checkout(tmp_path):
    from patchington.gitwork import GitWorkspace

    repo, policy, snapshot = fixture_repo(tmp_path)
    (repo / "src/calc.py").write_text("DIRTY = True\n")
    workspace = GitWorkspace(repo)
    candidate = workspace.candidate(PATCH, snapshot, policy)
    assert candidate.parent_sha == snapshot.head_sha
    assert candidate.timestamp == 946684800
    assert workspace.candidate(PATCH, snapshot, policy) == candidate
    assert git(repo, "rev-parse", candidate.commit_sha + "^1") == snapshot.head_sha
    assert workspace.snapshot(candidate.commit_sha)["src/calc.py"] == "VALUE = 2\n"
    assert (repo / "src/calc.py").read_text() == "DIRTY = True\n"
    destination = tmp_path / "checkout"
    workspace.checkout(candidate.commit_sha, destination)
    assert (destination / "src/calc.py").read_text() == "VALUE = 2\n"
    assert not (destination / ".git").exists()


@pytest.mark.parametrize("bad", [
    PATCH.replace("src/calc.py", "../escape"),
    PATCH.replace("src/calc.py", ".github/workflows/test.yml"),
    PATCH.replace("@@ -1 +1 @@", "old mode 100644\nnew mode 100755\n@@ -1 +1 @@"),
    "diff --git a/src/x b/src/x\nnew file mode 120000\n--- /dev/null\n+++ b/src/x\n@@ -0,0 +1 @@\n+/etc/passwd\n",
    "GIT binary patch\nliteral 12\nabc\n",
])
def test_candidate_refuses_unsafe_or_unsupported_patches(tmp_path, bad):
    from patchington.gitwork import GitWorkspace, WorkspaceError

    repo, policy, snapshot = fixture_repo(tmp_path)
    with pytest.raises(WorkspaceError):
        GitWorkspace(repo).candidate(bad, snapshot, policy)


def test_snapshot_refuses_symlinks_binary_submodules_and_oversize(tmp_path):
    from patchington.gitwork import GitWorkspace, WorkspaceError

    repo, _, snapshot = fixture_repo(tmp_path)
    workspace = GitWorkspace(repo)
    with pytest.raises(WorkspaceError):
        workspace.snapshot("HEAD")
    os.symlink("/etc/passwd", repo / "src/link")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=f", "-c", "user.email=f@e", "commit", "-qm", "link")
    with pytest.raises(WorkspaceError, match="mode|symlink"):
        workspace.snapshot(git(repo, "rev-parse", "HEAD"))
    git(repo, "reset", "--hard", snapshot.head_sha)
    (repo / "src/binary").write_bytes(b"\0\xff")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=f", "-c", "user.email=f@e", "commit", "-qm", "binary")
    with pytest.raises(WorkspaceError, match="binary|UTF"):
        workspace.snapshot(git(repo, "rev-parse", "HEAD"))


def test_candidate_does_not_run_hooks_filters_or_global_configuration(tmp_path, monkeypatch):
    from patchington.gitwork import GitWorkspace

    repo, policy, snapshot = fixture_repo(tmp_path)
    marker = tmp_path / "executed"
    hooks = repo / ".git/hooks"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    git(repo, "config", "filter.evil.clean", f"touch {marker}")
    git(repo, "config", "core.attributesFile", str(tmp_path / "attributes"))
    (tmp_path / "attributes").write_text("* filter=evil\n")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    candidate = GitWorkspace(repo).candidate(PATCH, snapshot, policy)
    assert candidate.tree_sha
    assert not marker.exists()


def test_source_submodule_and_file_byte_bounds_fail_closed(tmp_path, monkeypatch):
    from patchington.gitwork import GitWorkspace, WorkspaceError

    repo, _, snapshot = fixture_repo(tmp_path)
    git(repo, "update-index", "--add", "--cacheinfo", "160000", snapshot.head_sha, "src/vendor")
    git(repo, "-c", "user.name=f", "-c", "user.email=f@e", "commit", "-qm", "submodule")
    with pytest.raises(WorkspaceError, match="submodule"):
        GitWorkspace(repo).snapshot(git(repo, "rev-parse", "HEAD"))
    workspace = GitWorkspace(repo)
    monkeypatch.setattr(workspace, "MAX_SOURCE_BYTES", 3)
    with pytest.raises(WorkspaceError, match="byte"):
        workspace.snapshot(snapshot.head_sha)
    monkeypatch.setattr(workspace, "MAX_SOURCE_BYTES", 20_000_000)
    monkeypatch.setattr(workspace, "MAX_BLOB_BYTES", 3)
    with pytest.raises(WorkspaceError, match="bounded Git"):
        workspace.snapshot(snapshot.head_sha)


def test_candidate_refuses_identity_policy_and_patch_size_mismatches(tmp_path):
    from patchington.gitwork import GitWorkspace, WorkspaceError

    repo, policy, snapshot = fixture_repo(tmp_path)
    workspace = GitWorkspace(repo)
    wrong = policy.model_copy(update={"repository_id": 999})
    with pytest.raises(WorkspaceError, match="authorized policy"):
        workspace.candidate(PATCH, snapshot, wrong)
    small = policy.model_copy(update={"max_patch_bytes": 4})
    with pytest.raises(WorkspaceError, match="oversized"):
        workspace.candidate(PATCH, snapshot, small)
    for patch in ("", PATCH.replace("VALUE = 2", "VALUE = 1")):
        with pytest.raises(WorkspaceError):
            workspace.candidate(patch, snapshot, policy)


def committed(repo, snapshot):
    git(repo, "add", ".")
    git(repo, "-c", "user.name=f", "-c", "user.email=f@e", "commit", "-qm", "assets")
    head = git(repo, "rev-parse", "HEAD")
    return snapshot.model_copy(update={"head_sha": head, "base_sha": head})


def test_candidate_preserves_unrelated_binary_and_checkout_preserves_bytes(tmp_path):
    from patchington.gitwork import GitWorkspace, WorkspaceError

    repo, policy, snapshot = fixture_repo(tmp_path)
    (repo / "assets").mkdir()
    binary = bytes(range(256)) * 10
    (repo / "assets/logo.png").write_bytes(binary)
    snapshot = committed(repo, snapshot)
    workspace = GitWorkspace(repo)
    with pytest.raises(WorkspaceError):
        workspace.snapshot(snapshot.head_sha)
    candidate = workspace.candidate(PATCH, snapshot, policy)
    assert git(repo, "rev-parse", snapshot.head_sha + ":assets/logo.png") == git(
        repo, "rev-parse", candidate.commit_sha + ":assets/logo.png")
    destination = tmp_path / "binary-checkout"
    workspace.checkout(candidate.commit_sha, destination)
    assert (destination / "assets/logo.png").read_bytes() == binary
    assert (destination / "src/calc.py").read_text() == "VALUE = 2\n"


def test_source_slice_prioritizes_requested_then_nearby_with_explicit_omissions(tmp_path):
    from patchington.gitwork import GitWorkspace

    repo, _, snapshot = fixture_repo(tmp_path)
    (repo / "src/helper.py").write_text("HELP = 1\n")
    (repo / "src/large.py").write_text("#" * 1000)
    (repo / "src/bad.py").write_bytes(b"\xff\xfe")
    (repo / "src/logo.png").write_bytes(b"\0asset")
    (repo / "other").mkdir()
    (repo / "other/unrelated.py").write_text("UNRELATED = True\n")
    snapshot = committed(repo, snapshot)
    workspace = GitWorkspace(repo)
    result = workspace.source_slice(snapshot.head_sha, ("src/calc.py", "src/logo.png"), budget=30)
    assert list(result)[:1] == ["src/calc.py"]
    assert result["src/helper.py"] == "HELP = 1\n"
    assert "other/unrelated.py" not in result
    assert "src/large.py" not in result
    assert "src/bad.py" not in result
    assert "src/logo.png" not in result
    assert sum(len(value.encode()) for value in result.values()) <= 30
    assert workspace.source_slice_omissions["src/logo.png"] == "nontext"
    assert workspace.source_slice_omissions["src/large.py"] == "byte_budget"
    tiny = workspace.source_slice(snapshot.head_sha, ("src/calc.py",), budget=2)
    assert tiny == {}


def test_checkout_has_distinct_bounded_binary_admission(tmp_path, monkeypatch):
    from patchington.gitwork import GitWorkspace, WorkspaceError

    repo, _, snapshot = fixture_repo(tmp_path)
    (repo / "asset.bin").write_bytes(b"\0" * 100)
    snapshot = committed(repo, snapshot)
    workspace = GitWorkspace(repo)
    monkeypatch.setattr(workspace, "MAX_CHECKOUT_BYTES", 50)
    with pytest.raises(WorkspaceError, match="checkout.*byte"):
        workspace.checkout(snapshot.head_sha, tmp_path / "bounded")


def test_context_and_candidate_admit_monorepo_above_legacy_snapshot_limit(tmp_path):
    from patchington.gitwork import GitWorkspace, WorkspaceError

    repo, policy, snapshot = fixture_repo(tmp_path)
    digest = git(repo, "hash-object", "-w", "--stdin", data=b"\0asset")
    rows = "".join(f"100644 {digest}\tassets/item-{number:05}.bin\n" for number in range(5001))
    git(repo, "update-index", "--index-info", data=rows.encode())
    git(repo, "-c", "user.name=f", "-c", "user.email=f@e", "commit", "-qm", "monorepo")
    head = git(repo, "rev-parse", "HEAD")
    snapshot = snapshot.model_copy(update={"head_sha": head, "base_sha": head})
    workspace = GitWorkspace(repo)
    with pytest.raises(WorkspaceError, match="strict snapshot file count"):
        workspace.snapshot(head)
    assert workspace.source_slice(head, ("src/calc.py",)) == {"src/calc.py": "VALUE = 1\n"}
    candidate = workspace.candidate(PATCH, snapshot, policy)
    assert len(workspace._entries(candidate.commit_sha)) == 5002
    destination = tmp_path / "monorepo-checkout"
    workspace.checkout(candidate.commit_sha, destination)
    assert (destination / "assets/item-05000.bin").read_bytes() == b"\0asset"
