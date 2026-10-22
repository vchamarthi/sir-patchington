"""Pinned Git object operations; repository programs, hooks and filters never execute."""
from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from patchington.contracts import Candidate, ChangeSnapshot, RepoPolicy, safe_path


class WorkspaceError(ValueError):
    """Unsafe or unsupported source/candidate."""


@dataclass(frozen=True)
class ProcessResult:
    code: int | None
    output: bytes
    timed_out: bool = False
    overflow: bool = False


def bounded_run(argv: list[str], *, cwd: Path | None = None, env: dict | None = None,
                data: bytes = b"", timeout: int = 30, limit: int = 20_000_000) -> ProcessResult:
    """Drain bounded combined output; terminate the entire process group on a limit."""
    with tempfile.TemporaryFile() as source:
        source.write(data)
        source.seek(0)
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=source,  # noqa: S603 - trusted argv; no shell
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        except OSError as exc:
            return ProcessResult(None, str(exc).encode()[:limit])
        output = bytearray()
        deadline = time.monotonic() + timeout
        timed_out = overflow = False
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fd, min(65536, limit + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > limit:
                        overflow = True
                        break
                if overflow:
                    break
        if timed_out or overflow:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # macOS can reject a group signal after a short-lived child exits.
                if process.poll() is None:
                    process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        process.stdout.close()
        return ProcessResult(process.returncode, bytes(output[:limit]), timed_out, overflow)


def git_environment() -> dict[str, str]:
    # Construct, never copy: no auth tokens, askpass, inherited Git configuration or loaders.
    return {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": "/nonexistent",
            "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ATTR_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0"}


class GitWorkspace:
    MAX_FILES = 100_000
    MAX_SNAPSHOT_FILES = 5000
    MAX_CHECKOUT_BYTES = 200_000_000
    MAX_CONTEXT_FILES = 200
    MAX_SOURCE_BYTES = 20_000_000
    MAX_BLOB_BYTES = 2_000_000
    TIMESTAMP = 946684800

    def __init__(self, path: Path):
        self.path = Path(path).resolve(strict=True)
        self._git(["rev-parse", "--git-dir"])
        self.source_slice_omissions: dict[str, str] = {}
        self.source_slice_coverage: dict[str, int] = {}

    def _git(self, args: list[str], *, data: bytes = b"", env: dict | None = None,
             limit: int = MAX_SOURCE_BYTES) -> bytes:
        result = bounded_run(self._git_command(args), env=env or git_environment(),
                             data=data, limit=limit)
        if result.code != 0 or result.timed_out or result.overflow:
            raise WorkspaceError("bounded Git operation failed: " + result.output[:500].decode(
                "utf-8", errors="replace"))
        return result.output

    def _git_command(self, args: list[str]) -> list[str]:
        return ["git", "--no-pager", "-c", "core.hooksPath=/dev/null",
                "-c", "core.attributesFile=/dev/null", "-c", "commit.gpgSign=false",
                "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                "-c", "diff.external=", "-C", str(self.path), *args]

    @staticmethod
    def _revision(revision: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise WorkspaceError("only an exact commit SHA is accepted")
        return revision

    def _entries(self, revision: str) -> dict[str, tuple[str, str]]:
        raw = self._git(["ls-tree", "-rz", "--full-tree", self._revision(revision)],
                        limit=64_000_000)
        entries = {}
        for row in raw.split(b"\0"):
            if not row:
                continue
            try:
                metadata, name = row.split(b"\t", 1)
                mode, kind, digest = metadata.decode("ascii").split()
                path = safe_path(name.decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise WorkspaceError("unsafe Git tree path") from exc
            if mode not in {"100644", "100755"} or kind != "blob":
                raise WorkspaceError("symlink, submodule or unsupported mode refused")
            if path in entries or not re.fullmatch(r"[a-f0-9]{40}", digest):
                raise WorkspaceError("duplicate path or invalid Git object identity")
            entries[path] = (mode, digest)
            if len(entries) > self.MAX_FILES:
                raise WorkspaceError("source file count exceeds bound")
        return entries

    def snapshot(self, revision: str) -> dict[str, str]:
        entries = self._entries(revision)
        if len(entries) > self.MAX_SNAPSHOT_FILES:
            raise WorkspaceError("strict snapshot file count exceeds bound")
        result = {}
        total = 0
        for path, (_, digest) in entries.items():
            data = self._git(["cat-file", "blob", digest], limit=self.MAX_BLOB_BYTES)
            total += len(data)
            if total > self.MAX_SOURCE_BYTES:
                raise WorkspaceError("source byte limit exceeded")
            if b"\0" in data:
                raise WorkspaceError("binary source refused")
            try:
                result[path] = data.decode("utf-8")
            except UnicodeError as exc:
                raise WorkspaceError("source must be UTF-8") from exc
        return result

    def _sizes(self, digests: list[str]) -> dict[str, int]:
        unique = list(dict.fromkeys(digests))
        if not unique:
            return {}
        raw = self._git(["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                        data=("\n".join(unique) + "\n").encode(), limit=len(unique) * 128)
        sizes = {}
        try:
            rows = raw.decode("ascii").splitlines()
            if len(rows) != len(unique):
                raise ValueError("missing blob metadata")
            for expected, line in zip(unique, rows, strict=True):
                digest, kind, size = line.split()
                if digest != expected or kind != "blob" or int(size) < 0:
                    raise ValueError("invalid blob metadata")
                sizes[digest] = int(size)
        except (ValueError, UnicodeError) as exc:
            raise WorkspaceError("invalid Git blob size metadata") from exc
        return sizes

    def source_slice(self, revision: str, paths: tuple[str, ...],
                     budget: int = 100_000, *, policy: RepoPolicy | None = None) -> dict[str, str]:
        """Read bounded pinned context: changed files, direct imports, tests and guides.

        Discovery inspects tree names and only the selected changed-file contents;
        it neither scans all source contents nor follows dependencies recursively.
        Omissions and counts describe this slice, not complete repository coverage.
        """
        from patchington.context import (
            _ancestor_guidance,
            _credential_path,
            _read_reason,
            _related_tests,
            _signature,
            python_dependencies,
        )

        if (not isinstance(paths, tuple) or len(paths) > 500 or isinstance(budget, bool)
                or not isinstance(budget, int) or not 1 <= budget <= self.MAX_SOURCE_BYTES
                or (policy is not None and not isinstance(policy, RepoPolicy))):
            raise WorkspaceError("invalid bounded source slice request")
        try:
            requested = list(dict.fromkeys(safe_path(path) for path in paths))
        except ValueError as exc:
            raise WorkspaceError("unsafe source slice path") from exc
        entries = self._entries(revision)
        effective_budget = min(budget, self.MAX_BLOB_BYTES,
                               policy.max_source_bytes if policy else self.MAX_SOURCE_BYTES)
        parents = {str(PurePosixPath(path).parent) for path in requested}
        extensions = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java",
                      ".c", ".h", ".cpp", ".hpp", ".rb", ".kt", ".swift", ".sql", ".vue",
                      ".svelte", ".toml", ".yaml", ".yml", ".json", ".md"}
        binary_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
                             ".gz", ".tar", ".woff", ".woff2", ".ttf", ".mp4", ".mp3", ".bin",
                             ".so", ".dll", ".exe", ".pyc", ".sqlite", ".db"}
        available = set(entries)
        omissions: dict[str, str] = {}
        result: dict[str, str] = {}
        considered: dict[str, str] = {}
        used = inspected = inspected_bytes = 0

        def acquire(candidates: list[str], category: str) -> None:
            nonlocal used, inspected, inspected_bytes
            eligible = []
            for path in candidates:
                if path in considered:
                    continue
                considered[path] = category
                reason = (_read_reason(path, policy) if policy else
                          "credential_filename" if _credential_path(path) or _signature(path)
                          else None)
                if reason:
                    omissions[path] = reason
                elif path not in entries:
                    omissions[path] = "missing"
                elif PurePosixPath(path).suffix.lower() in binary_extensions:
                    omissions[path] = "nontext"
                else:
                    eligible.append(path)
            sizes = self._sizes([entries[path][1] for path in eligible])
            for path in eligible:
                size = sizes[entries[path][1]]
                if size > min(self.MAX_BLOB_BYTES, effective_budget - used):
                    omissions[path] = "byte_budget"
                    continue
                if inspected >= self.MAX_CONTEXT_FILES:
                    omissions[path] = "file_limit"
                    continue
                if size > self.MAX_SOURCE_BYTES - inspected_bytes:
                    omissions[path] = "inspection_byte_budget"
                    continue
                data = self._git(["cat-file", "blob", entries[path][1]], limit=max(1, size))
                inspected += 1
                inspected_bytes += len(data)
                if b"\0" in data:
                    omissions[path] = "nontext"
                    continue
                try:
                    text = data.decode("utf-8")
                except UnicodeError:
                    omissions[path] = "invalid_utf8"
                    continue
                if _signature(text):
                    omissions[path] = "credential_signature"
                    continue
                result[path] = text
                used += len(data)

        acquire(requested, "requested")
        dependencies: set[str] = set()
        matching_tests: set[str] = set()
        for path in requested:
            source_path = PurePosixPath(path)
            if path in result and source_path.suffix == ".py":
                dependencies.update(python_dependencies(path, result[path], available))
                matching_tests.update(_related_tests(path))
        requested_set = set(requested)
        ancestor_directories = {str(parent) for path in requested
                                for parent in PurePosixPath(path).parents}
        guidelines = {path for path in available
                      if str(PurePosixPath(path).parent) in ancestor_directories
                      and _ancestor_guidance(path, requested_set)}
        if requested:
            guidelines.add(".github/copilot-instructions.md")
        acquire(sorted(dependencies), "dependency")
        acquire(sorted(matching_tests & available), "matching_test")
        acquire(sorted(guidelines & available), "guideline")
        nearby = [path for path in sorted(entries) if path not in considered
                  and str(PurePosixPath(path).parent) in parents
                  and PurePosixPath(path).suffix.lower() in extensions]
        acquire(nearby, "nearby")
        self.source_slice_omissions = {
            f"[withheld filename {number}]" if _signature(path) else path: reason
            for number, (path, reason) in enumerate(omissions.items(), start=1)}
        self.source_slice_coverage = {"tree_files": len(entries), "requested_files": len(requested),
                                      "selected_files": len(result), "selected_bytes": used,
                                      "inspected_files": inspected,
                                      "inspected_bytes": inspected_bytes,
                                      "inspection_byte_budget": self.MAX_SOURCE_BYTES,
                                      "candidate_files": len(considered),
                                      "requested_byte_budget": budget,
                                      "effective_byte_budget": effective_budget,
                                      "budget_capped": int(effective_budget < budget),
                                      "python_import_scan_files": sum(path in result
                                          and path.endswith(".py") for path in requested),
                                      "python_import_scan_omissions": sum(path not in result
                                          and path.endswith(".py") for path in requested),
                                      "dependency_files": sum(considered[p] == "dependency"
                                                              for p in result),
                                      "matching_test_files": sum(considered[p] == "matching_test"
                                                                 for p in result),
                                      "guideline_files": sum(considered[p] == "guideline"
                                                             for p in result),
                                      "nearby_file_limit_omissions": sum(
                                          omissions.get(p) == "file_limit" for p in nearby)}
        return result

    def checkout(self, revision: str, destination: Path) -> None:
        """Materialize exact bounded regular blobs; binary assets remain byte-exact."""
        entries = self._entries(revision)
        sizes = self._sizes([entry[1] for entry in entries.values()])
        total = sum(sizes[entry[1]] for entry in entries.values())
        if total > self.MAX_CHECKOUT_BYTES:
            raise WorkspaceError("checkout source byte limit exceeded")
        destination = Path(destination)
        if destination.exists() and (destination.is_symlink() or any(destination.iterdir())):
            raise WorkspaceError("checkout destination must be an empty real directory")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not entries:
            return
        # A single batch avoids one Git process per file and keeps raw assets on disk.
        with tempfile.TemporaryFile() as commands, tempfile.TemporaryFile() as blobs, \
                tempfile.TemporaryFile() as errors:
            commands.write(("\n".join(entry[1] for entry in entries.values()) + "\n").encode())
            commands.seek(0)
            process = subprocess.Popen(self._git_command(["cat-file", "--batch"]),  # noqa: S603 - fixed Git operation
                                       stdin=commands, stdout=blobs, stderr=errors,
                                       env=git_environment(), start_new_session=True)
            deadline = time.monotonic() + 60
            output_limit = total + len(entries) * 128
            failed = False
            while process.poll() is None:
                if (time.monotonic() > deadline or os.fstat(blobs.fileno()).st_size > output_limit
                        or os.fstat(errors.fileno()).st_size > 4096):
                    failed = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        if process.poll() is None:
                            process.kill()
                    break
                time.sleep(0.01)
            process.wait(timeout=2)
            if (failed or process.returncode != 0 or
                    os.fstat(blobs.fileno()).st_size > output_limit or
                    os.fstat(errors.fileno()).st_size > 4096):
                raise WorkspaceError("bounded checkout blob transfer failed")
            blobs.seek(0)
            for path, (mode, digest) in entries.items():
                size = sizes[digest]
                if blobs.readline(128) != f"{digest} blob {size}\n".encode():
                    raise WorkspaceError("checkout blob identity or size mismatch")
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    remaining = size
                    while remaining:
                        chunk = blobs.read(min(65536, remaining))
                        if not chunk:
                            raise WorkspaceError("truncated checkout blob")
                        stream.write(chunk)
                        remaining -= len(chunk)
                if blobs.read(1) != b"\n":
                    raise WorkspaceError("invalid checkout blob boundary")
                target.chmod(0o755 if mode == "100755" else 0o644)
            if blobs.read(1):
                raise WorkspaceError("unexpected checkout blob output")

    def candidate(self, patch: str, snapshot: ChangeSnapshot, policy: RepoPolicy) -> Candidate:
        if (snapshot.tenant != policy.tenant or snapshot.repository_id != policy.repository_id
                or snapshot.installation_id != policy.installation_id or snapshot.repo != policy.repo):
            raise WorkspaceError("snapshot differs from authorized policy")
        if not patch or len(patch.encode()) > policy.max_patch_bytes or "\0" in patch:
            raise WorkspaceError("empty, binary or oversized patch")
        if any(re.match(r"^(old mode |new mode |new file mode (?!100644)|"
                        r"deleted file mode (?!100644)|GIT binary patch|Binary files |"
                        r"rename (from|to) |copy (from|to) )", line)
               for line in patch.splitlines()):
            raise WorkspaceError("binary, rename, copy or mode changes refused")
        before = self._entries(snapshot.head_sha)
        objects = self._git(["rev-parse", "--git-path", "objects"]).decode().strip()
        objects_path = (self.path / objects).resolve() if not Path(objects).is_absolute() else Path(objects)
        with tempfile.TemporaryDirectory(prefix="patchington-candidate-") as name:
            private = Path(name)
            env = git_environment()
            init = bounded_run(["git", "init", "--bare", "--quiet", "--template=", str(private)], env=env)
            if init.code != 0:
                raise WorkspaceError("cannot initialize isolated object workspace")
            isolated = GitWorkspace(private)
            env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(objects_path)
            isolated._git(["read-tree", snapshot.head_sha], env=env)
            isolated._git(["apply", "--cached", "--whitespace=nowarn", "-"],
                          data=patch.encode(), env=env)
            tree = isolated._git(["write-tree"], env=env).decode().strip()
            # Read via the isolated configuration while resolving the source object alternate.
            (private / "objects/info/alternates").write_text(str(objects_path) + "\n")
            after = isolated._entries(tree)
            changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
            if not changed or len(changed) > policy.max_files:
                raise WorkspaceError("empty candidate or too many changed files")
            if any(not policy.permits_path(p) for p in changed):
                raise WorkspaceError("candidate modifies protected or disallowed paths")
            if any(before[p][0] != after[p][0]
                   for p in before.keys() & after.keys()):
                raise WorkspaceError("mode changes refused")
            changed_bytes = 0
            for entries, reader in ((before, self), (after, isolated)):
                for path in changed & entries.keys():
                    data = reader._git(["cat-file", "blob", entries[path][1]], limit=self.MAX_BLOB_BYTES)
                    if b"\0" in data:
                        raise WorkspaceError("changed binary source refused")
                    try:
                        data.decode("utf-8")
                    except UnicodeError as exc:
                        raise WorkspaceError("changed source must be UTF-8") from exc
                    changed_bytes += len(data)
            if changed_bytes > policy.max_source_bytes:
                raise WorkspaceError("changed source exceeds policy byte bound")
            env.update({"GIT_AUTHOR_NAME": "Sir Patchington", "GIT_COMMITTER_NAME": "Sir Patchington",
                        "GIT_AUTHOR_EMAIL": "patchington@localhost",
                        "GIT_COMMITTER_EMAIL": "patchington@localhost",
                        "GIT_AUTHOR_DATE": f"{self.TIMESTAMP} +0000",
                        "GIT_COMMITTER_DATE": f"{self.TIMESTAMP} +0000"})
            commit = isolated._git(["commit-tree", tree, "-p", snapshot.head_sha],
                                   data=b"Sir Patchington verified candidate\n", env=env).decode().strip()
            # Import only newly written raw objects. No index, refs, hooks or filters are touched.
            for object_path in (private / "objects").glob("[0-9a-f][0-9a-f]/*"):
                oid = object_path.parent.name + object_path.name
                kind = isolated._git(["cat-file", "-t", oid], env=env).decode().strip()
                raw = isolated._git(["cat-file", kind, oid], env=env)
                imported = self._git(["hash-object", "-w", "-t", kind, "--stdin"], data=raw).decode().strip()
                if imported != oid:
                    raise WorkspaceError("Git object identity mismatch")
        return Candidate(snapshot=snapshot, patch=patch, parent_sha=snapshot.head_sha,
                         tree_sha=tree, commit_sha=commit, timestamp=self.TIMESTAMP,
                         policy_digest=policy.digest)
