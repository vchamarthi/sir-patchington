"""Independent baseline/candidate evidence from bounded trusted checks."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path

from patchington.artifacts import ArtifactStore
from patchington.contracts import Candidate, CheckResult, CheckSpec, Verification
from patchington.gitwork import GitWorkspace, ProcessResult, WorkspaceError, bounded_run

_FIXED_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",  # noqa: S108 - private container tmpfs
              "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
              "PYTEST_ADDOPTS": "-p no:cacheprovider", "PYTHONHASHSEED": "0"}
_ORACLE_NAMES = {"conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "setup.py",
                 "tox.ini", "noxfile.py", "makefile", "dockerfile", "package.json",
                 "package-lock.json", "uv.lock", "poetry.lock", "requirements.txt",
                 ".coveragerc", "cargo.toml", "cargo.lock", "go.mod", "go.sum"}


def _inventory(root: Path) -> dict[str, tuple[str, int]]:
    result = {}
    total = 0
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        relative = str(path.relative_to(root))
        if stat.S_ISLNK(info.st_mode):
            result[relative] = ("symlink", info.st_mode)
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
            if total > GitWorkspace.MAX_CHECKOUT_BYTES or len(result) >= GitWorkspace.MAX_FILES:
                raise WorkspaceError("mutated source inventory exceeds bounds")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
            result[relative] = (digest.hexdigest(), stat.S_IMODE(info.st_mode))
        elif not stat.S_ISDIR(info.st_mode):
            result[relative] = ("special", info.st_mode)
    return result


def _empty_or_skipped(output: bytes) -> bool:
    text = output.decode("utf-8", errors="replace")
    if re.search(r"no tests ran|collected 0 items|Ran 0 tests", text, re.I):
        return True
    return bool(re.search(r"\b\d+ skipped\b", text)
                and not re.search(r"\b\d+ (passed|failed|errors?)\b", text))


class _Verifier:
    def __init__(self, *, timeout: int = 120, artifacts: ArtifactStore | None = None,
                 log_limit: int = 1_000_000):
        if not 1 <= timeout <= 600 or not 1024 <= log_limit <= 4_000_000:
            raise ValueError("invalid execution bounds")
        self.timeout = timeout
        self.log_limit = log_limit
        self._temporary_artifacts = None
        if artifacts is None:
            self._temporary_artifacts = tempfile.TemporaryDirectory(prefix="patchington-evidence-")
            artifacts = ArtifactStore(Path(self._temporary_artifacts.name))
        self.artifacts = artifacts

    def _environment(self) -> tuple[str, list[str]]:
        raise NotImplementedError

    def _execute(self, source: Path, check: CheckSpec) -> ProcessResult:
        raise NotImplementedError

    def _once(self, workspace: GitWorkspace, revision: str, check: CheckSpec):
        with tempfile.TemporaryDirectory(prefix="patchington-check-",
                                         dir=getattr(self, "scratch_root", None)) as name:
            source = Path(name) / "source"
            workspace.checkout(revision, source)
            before = _inventory(source)
            result = self._execute(source, check)
            try:
                unchanged = before == _inventory(source)
            except (OSError, WorkspaceError):
                unchanged = False
            return result, unchanged

    def verify(self, workspace: GitWorkspace, candidate: Candidate,
               checks: tuple[CheckSpec, ...]) -> Verification:
        environment, violations = self._environment()
        if not checks or not any(c.required for c in checks):
            violations.append("no required acceptance checks configured")
        if not any(c.required and c.regression for c in checks):
            violations.append("no required change-specific baseline regression check")
        if len({c.name for c in checks}) != len(checks):
            violations.append("duplicate acceptance check names")
        try:
            raw = workspace._git(["cat-file", "commit", candidate.commit_sha]).decode()
            header = raw.split("\n\n", 1)[0].splitlines()
            if (f"tree {candidate.tree_sha}" not in header
                    or [line for line in header if line.startswith("parent ")]
                    != [f"parent {candidate.parent_sha}"]):
                violations.append("candidate Git commit differs from frozen parent/tree")
            before = workspace._entries(candidate.parent_sha)
            after = workspace._entries(candidate.commit_sha)
            changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
            for path in changed:
                pieces = path.lower().split("/")
                basename = pieces[-1]
                if (any(part in {"tests", "test", ".github", "ci", ".gitlab"} for part in pieces)
                        or basename in _ORACLE_NAMES or basename.startswith("test_")
                        or basename.endswith(("_test.py", ".lock"))):
                    violations.append("candidate changes an acceptance input: " + path)
        except (WorkspaceError, UnicodeError):
            violations.append("candidate source or commit identity unavailable")
        results = []
        for check in checks:
            if violations:
                empty = self.artifacts.put(b"")
                results.append(CheckResult(name=check.name, baseline_exit=None, candidate_exit=None,
                                           baseline_log_digest=empty, candidate_log_digest=empty,
                                           skipped=True, passed=False,
                                           detail="verification preflight refused execution"))
                continue
            baseline, baseline_unchanged = self._once(workspace, candidate.parent_sha, check)
            repaired, candidate_unchanged = self._once(workspace, candidate.commit_sha, check)
            errors = []
            timed_out = baseline.timed_out or repaired.timed_out
            unavailable = baseline.code in {None, 125, 126, 127} or repaired.code in {None, 125, 126, 127}
            empty = _empty_or_skipped(baseline.output) or _empty_or_skipped(repaired.output)
            if timed_out:
                errors.append("check exceeded execution deadline")
            if baseline.overflow or repaired.overflow:
                errors.append("check exceeded log bound")
            if unavailable:
                errors.append("check unavailable or did not execute")
            if empty:
                errors.append("no executed tests or all tests skipped")
            if not baseline_unchanged or not candidate_unchanged:
                errors.append("check mutated source inventory")
            if repaired.code != 0:
                errors.append("candidate check failed")
            if check.regression and baseline.code == 0:
                errors.append("regression check did not fail original head")
            results.append(CheckResult(
                name=check.name, baseline_exit=baseline.code, candidate_exit=repaired.code,
                baseline_log_digest=self.artifacts.put(baseline.output),
                candidate_log_digest=self.artifacts.put(repaired.output),
                baseline_source_unchanged=baseline_unchanged,
                candidate_source_unchanged=candidate_unchanged, timed_out=timed_out,
                skipped=unavailable or empty, passed=not errors, detail="; ".join(errors)))
        payload = {"candidate_id": candidate.id,
                   "checks": [r.model_dump(mode="json") for r in results],
                   "environment": environment, "violations": violations}
        digest = self.artifacts.put_json(payload)
        return Verification(candidate_id=candidate.id,
                            passed=bool(results) and not violations and all(r.passed for r in results),
                            checks=tuple(results), evidence_digest=digest,
                            environment=environment, violations=tuple(violations))


class LocalVerifier(_Verifier):
    """Only for explicitly trusted fixtures. This grants no isolation from the host."""

    def __init__(self, *, trusted: bool = False, timeout: int = 120,
                 artifacts: ArtifactStore | None = None, log_limit: int = 1_000_000):
        if trusted is not True:
            raise ValueError("LocalVerifier requires explicitly trusted fixtures")
        super().__init__(timeout=timeout, artifacts=artifacts, log_limit=log_limit)

    def _environment(self):
        return "local:trusted-fixture:unsandboxed", []

    def _execute(self, source, check):
        return bounded_run(list(check.argv), cwd=source, env=dict(_FIXED_ENV),
                           timeout=min(self.timeout, check.timeout), limit=self.log_limit)


class DockerVerifier(_Verifier):
    """Docker boundary: read-only source/root, nonroot, networkless, bounded scratch."""

    def __init__(self, image: str, timeout: int = 120, *, artifacts: ArtifactStore | None = None,
                 log_limit: int = 1_000_000):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,249}", image):
            raise ValueError("invalid trusted image reference")
        super().__init__(timeout=timeout, artifacts=artifacts, log_limit=log_limit)
        self.image = image
        self._image_id = None

    @staticmethod
    def _host_environment():
        # Docker daemon context is host-side only. No host environment reaches source execution.
        allowed = {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                   "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
        return {key: value for key, value in os.environ.items() if key in allowed}

    def _docker(self, argv: list[str], *, timeout=30, limit=1_000_000):
        return bounded_run(["docker", *argv], env=self._host_environment(), timeout=timeout, limit=limit)

    def _environment(self):
        result = self._docker(["image", "inspect", self.image], limit=1_000_000)
        if result.code != 0 or result.timed_out or result.overflow:
            return "docker:unavailable", ["trusted Docker image unavailable; provision it explicitly"]
        try:
            metadata = json.loads(result.output)[0]
            image_id = metadata["Id"]
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
                raise ValueError("invalid Docker image identity")
            self._image_id = image_id
            digests = metadata.get("RepoDigests") or []
            digest = next((d for d in digests if re.fullmatch(
                r"[a-zA-Z0-9_./:-]+@sha256:[a-f0-9]{64}", d)), image_id)
        except (ValueError, KeyError, IndexError, TypeError):
            return "docker:unavailable", ["Docker image identity could not be verified"]
        return (f"docker:{digest};id={image_id};network=none;uid=65532;root=ro;source=ro;"
                "memory=256m;cpus=1;pids=64;tmpfs=64m;staging=daemon-volume"), []

    def _execute(self, source: Path, check: CheckSpec) -> ProcessResult:
        source.chmod(0o755)
        identity = uuid.uuid4().hex
        name = "patchington-check-" + identity
        staging = "patchington-stage-" + identity
        volume = "patchington-source-" + identity
        shared = ["--network=none", "--user=65532:65532", "--read-only", "--cap-drop=ALL",
                  "--security-opt=no-new-privileges:true", "--memory=256m", "--memory-swap=256m",
                  "--cpus=1", "--pids-limit=64", "--ulimit=nofile=256:256",
                  "--ulimit=core=0:0", "--log-driver=none", "--ipc=none"]
        operations = [
            ["volume", "create", "--label", "patchington.ephemeral=true", volume],
            ["create", "--name", staging, *shared, "--mount",
             f"type=volume,src={volume},dst=/work,volume-nocopy", "--entrypoint", "python",
             self._image_id, "-I", "-S", "-c", "pass"],
            # The staging container is never started; only the daemon copies bounded regular files.
            ["cp", str(source) + "/.", staging + ":/work"],
        ]
        result = ProcessResult(None, b"source staging did not complete")
        cleanup_errors = []
        try:
            for operation in operations:
                staged = self._docker(operation, timeout=min(30, self.timeout), limit=self.log_limit)
                if staged.code != 0 or staged.timed_out or staged.overflow:
                    result = ProcessResult(None, (b"source staging failed: " + staged.output)[:self.log_limit],
                                           timed_out=staged.timed_out, overflow=staged.overflow)
                    break
            else:
                argv = ["run", "--rm", "--pull=never", "--name", name, *shared,
                        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
                        "--mount", f"type=volume,src={volume},dst=/work,readonly,volume-nocopy",
                        "--workdir=/work"]
                for key, value in _FIXED_ENV.items():
                    argv.extend(["--env", f"{key}={value}"])
                argv.extend(["--entrypoint", check.argv[0], self._image_id, *check.argv[1:]])
                result = self._docker(argv, timeout=min(self.timeout, check.timeout), limit=self.log_limit)
        finally:
            # Explicit names make cleanup work even after a killed remote Docker CLI.
            for container in (name, staging):
                cleaned = self._docker(["rm", "--force", container], timeout=10, limit=4096)
                if (cleaned.timed_out or cleaned.overflow or
                        (cleaned.code != 0 and b"no such container" not in cleaned.output.lower())):
                    cleanup_errors.append("container cleanup unconfirmed")
            cleaned = self._docker(["volume", "rm", "--force", volume], timeout=10, limit=4096)
            if (cleaned.timed_out or cleaned.overflow or
                    (cleaned.code != 0 and b"no such volume" not in cleaned.output.lower())):
                cleanup_errors.append("source volume cleanup unconfirmed")
        if cleanup_errors:
            return ProcessResult(None, (result.output + b"\n" + "; ".join(cleanup_errors).encode())[:self.log_limit],
                                 timed_out=result.timed_out, overflow=result.overflow)
        return result
