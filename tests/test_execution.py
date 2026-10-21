import json
import sys

import pytest
from test_gitwork import PATCH, fixture_repo

from patchington.artifacts import ArtifactStore
from patchington.contracts import CheckSpec


def prepared(tmp_path):
    from patchington.gitwork import GitWorkspace

    repo, policy, snapshot = fixture_repo(tmp_path)
    workspace = GitWorkspace(repo)
    return workspace, workspace.candidate(PATCH, snapshot, policy)


def test_local_requires_explicit_trust_and_never_claims_a_sandbox(tmp_path):
    from patchington.execution import LocalVerifier

    with pytest.raises(ValueError, match="trusted"):
        LocalVerifier()
    workspace, candidate = prepared(tmp_path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    check = CheckSpec(name="independent", argv=(sys.executable, "-c",
                      "from src.calc import VALUE; assert VALUE == 2"), regression=True)
    result = LocalVerifier(trusted=True, artifacts=artifacts).verify(workspace, candidate, (check,))
    assert result.passed
    assert result.checks[0].baseline_exit == 1
    assert result.checks[0].candidate_exit == 0
    assert "unsandboxed" in result.environment
    evidence = artifacts.get_json(result.evidence_digest)
    assert evidence["candidate_id"] == candidate.id
    assert evidence["checks"][0]["baseline_exit"] == 1
    assert artifacts.get(result.checks[0].baseline_log_digest)


def test_missing_checks_no_regression_and_mutation_cannot_pass(tmp_path):
    from patchington.execution import LocalVerifier

    workspace, candidate = prepared(tmp_path)
    verifier = LocalVerifier(trusted=True)
    assert not verifier.verify(workspace, candidate, ()).passed
    optional = CheckSpec(name="optional", argv=(sys.executable, "-c", "pass"), required=False)
    assert not verifier.verify(workspace, candidate, (optional,)).passed
    always_green = CheckSpec(name="fake-regression", argv=(sys.executable, "-c", "pass"), regression=True)
    assert not verifier.verify(workspace, candidate, (always_green,)).passed
    mutate = CheckSpec(name="mutate", argv=(sys.executable, "-c",
                       "from pathlib import Path; Path('src/calc.py').write_text('forged')"), regression=True)
    mutated = verifier.verify(workspace, candidate, (mutate,))
    assert not mutated.passed
    assert not mutated.checks[0].candidate_source_unchanged
    no_tests = CheckSpec(name="empty", argv=(sys.executable, "-c", "print('no tests ran in 0.01s')"), regression=True)
    assert not verifier.verify(workspace, candidate, (no_tests,)).passed


def test_timeout_and_log_caps_fail_closed(tmp_path):
    from patchington.execution import LocalVerifier

    workspace, candidate = prepared(tmp_path)
    verifier = LocalVerifier(trusted=True, timeout=1, log_limit=1024)
    slow = CheckSpec(name="slow", argv=(sys.executable, "-c", "import time; time.sleep(10)"), regression=True)
    result = verifier.verify(workspace, candidate, (slow,))
    assert not result.passed and result.checks[0].timed_out
    loud = CheckSpec(name="loud", argv=(sys.executable, "-c", "print('x' * 1000000)"), regression=True)
    result = verifier.verify(workspace, candidate, (loud,))
    assert not result.passed
    assert len(verifier.artifacts.get(result.checks[0].candidate_log_digest)) <= 1024


def test_changed_acceptance_oracle_is_rejected(tmp_path):
    from patchington.execution import LocalVerifier
    from patchington.gitwork import GitWorkspace

    repo, policy, snapshot = fixture_repo(tmp_path)
    patch = "diff --git a/tests/test_x.py b/tests/test_x.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/test_x.py\n@@ -0,0 +1 @@\n+assert True\n"
    policy = policy.model_copy(update={"allowed_paths": ("src/", "tests/")})
    workspace = GitWorkspace(repo)
    candidate = workspace.candidate(patch, snapshot, policy)
    result = LocalVerifier(trusted=True).verify(workspace, candidate, (
        CheckSpec(name="checks", argv=(sys.executable, "-c", "pass")),))
    assert not result.passed
    assert any("acceptance" in item for item in result.violations)


@pytest.mark.docker
def test_actual_docker_isolation_and_baseline_regression_evidence(tmp_path, monkeypatch):
    from patchington.execution import DockerVerifier

    workspace, candidate = prepared(tmp_path)
    monkeypatch.setenv("PATCHINGTON_TEST_SECRET", "must-not-enter-container")
    script = """import os, socket
from pathlib import Path
from src.calc import VALUE
assert os.getuid() != 0
assert 'PATCHINGTON_TEST_SECRET' not in os.environ
assert not Path('/var/run/docker.sock').exists()
try:
    Path('/root-write-test').write_text('bad')
except OSError:
    pass
else:
    raise AssertionError('root writable')
sock = socket.socket(); sock.settimeout(0.3)
try:
    sock.connect(('1.1.1.1', 80))
except OSError:
    pass
else:
    raise AssertionError('network escaped')
print('isolated UID', os.getuid())
assert VALUE == 2
"""
    artifacts = ArtifactStore(tmp_path / "artifacts")
    verifier = DockerVerifier("python:3.12-slim", artifacts=artifacts)
    result = verifier.verify(workspace, candidate, (
        CheckSpec(name="isolated-regression", argv=("python", "-c", script), regression=True),))
    assert result.passed, result.model_dump()
    assert result.checks[0].baseline_exit == 1
    assert result.checks[0].candidate_exit == 0
    assert "sha256:" in result.environment
    assert b"isolated UID" in artifacts.get(result.checks[0].candidate_log_digest)
    evidence = artifacts.get_json(result.evidence_digest)
    assert json.loads(json.dumps(evidence))["environment"] == result.environment


@pytest.mark.docker
def test_actual_docker_uses_daemon_managed_source_volumes_without_host_mounts(tmp_path):
    from patchington.execution import DockerVerifier

    workspace, candidate = prepared(tmp_path)
    verifier = DockerVerifier("python:3.12-slim")
    calls = []
    original = verifier._docker

    def recorded(argv, **kwargs):
        calls.append(argv)
        return original(argv, **kwargs)

    verifier._docker = recorded
    check = CheckSpec(name="remote-compatible", regression=True, argv=("python", "-c",
                      "from src.calc import VALUE; assert VALUE == 2"))
    result = verifier.verify(workspace, candidate, (check,))
    assert result.passed, result.model_dump()
    assert not any("type=bind" in arg for call in calls for arg in call)
    assert any(call[:2] == ["volume", "create"] for call in calls)
    assert any(call[:1] == ["cp"] for call in calls)
    assert any("type=volume" in arg and "readonly" in arg for call in calls for arg in call)
    volumes = [call[-1] for call in calls if call[:2] == ["volume", "create"]]
    assert volumes
    for volume in volumes:
        assert any(call[:2] == ["volume", "rm"] and volume in call for call in calls)


@pytest.mark.docker
def test_actual_docker_verifies_text_fix_in_repository_with_binary_assets(tmp_path):
    from test_gitwork import committed

    from patchington.execution import DockerVerifier
    from patchington.gitwork import GitWorkspace

    repo, policy, snapshot = fixture_repo(tmp_path)
    (repo / "logo.png").write_bytes(b"\0\xffPNG")
    snapshot = committed(repo, snapshot)
    workspace = GitWorkspace(repo)
    candidate = workspace.candidate(PATCH, snapshot, policy)
    check = CheckSpec(name="binary-preserved", regression=True, argv=("python", "-c",
                      "from pathlib import Path; from src.calc import VALUE; "
                      "assert Path('logo.png').read_bytes() == bytes([0,255,80,78,71]); assert VALUE == 2"))
    result = DockerVerifier("python:3.12-slim").verify(workspace, candidate, (check,))
    assert result.passed, result.model_dump()
