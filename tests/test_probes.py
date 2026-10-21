import subprocess
import sys

import pytest
from test_gitwork import git

from patchington.artifacts import ArtifactStore
from patchington.contracts import ChangeSnapshot, RepoPolicy
from patchington.gitwork import GitWorkspace


def repository(tmp_path, source):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src/fees.py").write_text(source)
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=f", "-c", "user.email=f@e", "commit", "-qm", "base")
    head = git(repo, "rev-parse", "HEAD")
    snapshot = ChangeSnapshot(tenant="acme", repository_id=2, installation_id=1, repo="acme/repo",
                              pr=1, base_sha=head, head_sha=head, head_ref="fix", head_repo="acme/repo")
    policy = RepoPolicy(tenant="acme", team="dev", installation_id=1, repository_id=2, repo="acme/repo")
    return repo, GitWorkspace(repo), snapshot, policy


def run_probe(repo, check):
    return subprocess.run(list(check.argv), cwd=repo, capture_output=True, timeout=10)


def test_external_probe_supervises_success_and_early_exit(tmp_path):
    from patchington.probes import delivery_fee_probe

    repo, _, _, _ = repository(tmp_path, "def delivery_fee(x):\n    return 5 if x < 50 else 0\n")
    check = delivery_fee_probe(python=sys.executable)
    good = run_probe(repo, check)
    assert good.returncode == 0, good.stdout
    assert b"PATCHINGTON_PROBE_EXECUTED=3/3" in good.stdout
    (repo / "src/fees.py").write_text("import os\nos._exit(0)\n")
    attack = run_probe(repo, check)
    assert attack.returncode != 0
    assert b"PATCHINGTON_PROBE_EXECUTED=3/3" not in attack.stdout


@pytest.mark.parametrize("source", [
    "print('forged extra output')\ndef delivery_fee(x):\n    return 5 if x < 50 else 0\n",
    "def delivery_fee(x):\n    return 100\n",
    "import os\nos.write(1, b'{malformed}')\nos._exit(0)\n",
    "import time\ntime.sleep(5)\n",
    "print('x' * 100000)\ndef delivery_fee(x):\n    return 0\n",
])
def test_external_probe_rejects_extra_malformed_wrong_timed_out_or_large_output(tmp_path, source):
    from patchington.probes import delivery_fee_probe

    repo, _, _, _ = repository(tmp_path, source)
    check = delivery_fee_probe(python=sys.executable, case_timeout=1, output_limit=1024)
    assert run_probe(repo, check).returncode != 0


def test_probe_validates_identifiers_closed_cases_and_expected_exceptions(tmp_path):
    from patchington.probes import function_probe

    for module, function in (("src.fees;import os", "f"), ("src.fees", "f()"), ("../x", "f")):
        with pytest.raises(ValueError):
            function_probe("probe", module, function, ({"args": [1], "expected": 2},))
    for cases in ((), ({"args": [], "expected": 0, "unknown": True},),
                  ({"args": [], "expected": float('nan')},),
                  ({"args": [], "expected": 0, "raises": "ValueError"},)):
        with pytest.raises(ValueError):
            function_probe("probe", "src.fees", "delivery_fee", cases)
    repo, _, _, _ = repository(tmp_path, "def delivery_fee(x):\n    raise ValueError('bad')\n")
    check = function_probe("raises", "src.fees", "delivery_fee", (
        {"args": [1], "raises": "ValueError"},), python=sys.executable)
    assert run_probe(repo, check).returncode == 0


@pytest.mark.docker
def test_actual_docker_observer_rejects_exit_zero_candidate_and_accepts_fix(tmp_path):
    from patchington.execution import DockerVerifier
    from patchington.probes import delivery_fee_probe

    repo, workspace, snapshot, policy = repository(
        tmp_path, "def delivery_fee(x):\n    return 5 if x <= 50 else 0\n")
    good_patch = "diff --git a/src/fees.py b/src/fees.py\n--- a/src/fees.py\n+++ b/src/fees.py\n@@ -1,2 +1,2 @@\n def delivery_fee(x):\n-    return 5 if x <= 50 else 0\n+    return 5 if x < 50 else 0\n"
    bad_patch = "diff --git a/src/fees.py b/src/fees.py\n--- a/src/fees.py\n+++ b/src/fees.py\n@@ -1,2 +1,2 @@\n-def delivery_fee(x):\n-    return 5 if x <= 50 else 0\n+import os\n+os._exit(0)\n"
    verifier = DockerVerifier("python:3.12-slim", artifacts=ArtifactStore(tmp_path / "evidence"))
    check = delivery_fee_probe()
    attack = verifier.verify(workspace, workspace.candidate(bad_patch, snapshot, policy), (check,))
    assert not attack.passed
    assert attack.checks[0].candidate_exit == 1
    result = verifier.verify(workspace, workspace.candidate(good_patch, snapshot, policy), (check,))
    assert result.passed, result.model_dump()
    assert result.checks[0].baseline_exit == 1
    assert result.checks[0].candidate_exit == 0
    assert b"PATCHINGTON_PROBE_EXECUTED=3/3" in verifier.artifacts.get(
        result.checks[0].candidate_log_digest)


def test_external_probe_never_writes_bytecode_in_trusted_local_checkout(tmp_path):
    from patchington.execution import LocalVerifier
    from patchington.probes import delivery_fee_probe

    _, workspace, snapshot, policy = repository(
        tmp_path, "def delivery_fee(x):\n    return 5 if x <= 50 else 0\n")
    patch = "diff --git a/src/fees.py b/src/fees.py\n--- a/src/fees.py\n+++ b/src/fees.py\n@@ -1,2 +1,2 @@\n def delivery_fee(x):\n-    return 5 if x <= 50 else 0\n+    return 5 if x < 50 else 0\n"
    candidate = workspace.candidate(patch, snapshot, policy)
    result = LocalVerifier(trusted=True).verify(workspace, candidate, (
        delivery_fee_probe(python=sys.executable),))
    assert result.passed, result.model_dump()
    assert result.checks[0].baseline_source_unchanged
    assert result.checks[0].candidate_source_unchanged
