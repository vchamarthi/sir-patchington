"""Permission- and revision-scoped retrieval with explicit graph coverage."""
from __future__ import annotations

import ast
import copy
import hashlib
import re
from pathlib import PurePosixPath

from .contracts import ChangeSnapshot, RepoPolicy, safe_path

_CREDENTIAL_SIGNATURES = (
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_.-]{20,}")),
    ("provider_key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}")),
    ("google_key", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}", re.IGNORECASE)),
    ("jwt_token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("literal_secret", re.compile(
        r"\b(?:api[_-]?key|client[_-]?secret|secret[_-]?key|secret|password|access[_-]?token)"
        r"['\"]?\s*[:=]\s*['\"][^'\"\s]{12,}['\"]",
        re.IGNORECASE)),
)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(path.startswith(pattern) if pattern.endswith("/") else path == pattern
               for pattern in patterns)


def _credential_path(path: str) -> bool:
    parts = PurePosixPath(path.lower()).parts
    name = parts[-1]
    return (any(part.startswith(".env") or part in {".aws", ".ssh"} for part in parts)
            or name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".npmrc", ".pypirc",
                        ".netrc", "_netrc", ".git-credentials", "credentials", "secrets"}
            or name.endswith((".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"))
            or name.startswith(("credentials.", "client_secret", "service-account", "secrets.")))


def _signature(content: str) -> str | None:
    return next((kind for kind, pattern in _CREDENTIAL_SIGNATURES if pattern.search(content)), None)


def _read_reason(path: str, policy: RepoPolicy) -> str | None:
    if _credential_path(path) or _signature(path):
        return "credential_filename"
    if not _matches(path, policy.context_paths):
        return "read_policy"
    if _matches(path, policy.context_exclusions):
        return "context_exclusion"
    return None


def python_dependencies(path: str, content: str, available_paths: set[str]) -> set[str]:
    """Resolve static Python imports to existing repository paths without executing code.

    Absolute imports support repository-root and conventional ``src/`` layouts.
    Relative imports stay inside the importing package's source root. An imported
    name can be either a symbol or a submodule, so ``from pkg import child`` adds
    both the package and an existing child module. Basename searches, runtime
    import hooks, dynamic imports and nonstandard source roots are not inferred.
    """
    source = PurePosixPath(path)
    if (not path.endswith(".py") or source.is_absolute()
            or ".." in source.parts):
        return set()
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return set()

    def existing(module_path: str, *, absolute: bool) -> set[str]:
        roots = ("", "src/") if absolute else ("",)
        return {candidate for root in roots
                for candidate in (f"{root}{module_path}.py", f"{root}{module_path}/__init__.py")
                if candidate in available_paths}

    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                dependencies.update(existing(name.name.replace(".", "/"), absolute=True))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parent = source.parent.parts
                source_root = int(bool(parent and parent[0] == "src"
                                       and "src/__init__.py" not in available_paths))
                if node.level > len(parent) - source_root:
                    continue
                base = parent[:len(parent) - node.level + 1]
                module_path = "/".join((*base, *((node.module or "").split("."))))
                module_path = module_path.rstrip("/")
            else:
                module_path = (node.module or "").replace(".", "/")
            if not module_path:
                continue
            dependencies.update(existing(module_path, absolute=not node.level))
            for name in node.names:
                if name.name != "*":
                    dependencies.update(existing(f"{module_path}/{name.name}",
                                                 absolute=not node.level))
    return dependencies - {path}


def _related_tests(path: str) -> set[str]:
    """Use package-relative test paths; equal basenames alone are not evidence."""
    source = PurePosixPath(path)
    if source.suffix != ".py":
        return set()
    relative = PurePosixPath(*source.parts[1:]) if source.parts[0] == "src" else source
    parent, stem = relative.parent, relative.stem
    return {str(PurePosixPath(root) / parent / name)
            for root in ("test", "tests")
            for name in (f"test_{stem}.py", f"{stem}_test.py")} | {
                str(source.parent / f"test_{stem}.py"),
                str(source.parent / f"{stem}_test.py")}


def _ancestor_guidance(path: str, changed: set[str]) -> bool:
    source = PurePosixPath(path)
    if path == ".github/copilot-instructions.md":
        return bool(changed)
    if source.name.lower() not in {
            "agents.md", "claude.md", "contributing.md", "review_guidelines.md",
            "review-guidelines.md", "coding_guidelines.md", "coding-guidelines.md"}:
        return False
    return any(source.parent in PurePosixPath(changed_path).parents for changed_path in changed)


def model_snapshot(snapshot: ChangeSnapshot, policy: RepoPolicy) -> dict:
    """Bound and filter untrusted metadata before model transport; no complete-DLP claim.

    An entire text field is withheld when a credential signature is found, so a
    PEM body cannot remain after stripping only its header. Raw snapshot identity
    stays available for provenance, while raw content stays outside this payload.
    """
    ContextIndex._scope(snapshot, policy)
    raw = snapshot.model_dump(mode="json")
    omissions: list[dict] = []
    remaining = min(policy.max_source_bytes, 100_000)

    def sanitize(value, field: str):
        nonlocal remaining
        if isinstance(value, str):
            signature = _signature(value)
            if signature:
                omissions.append({"field": field, "reason": "credential_signature",
                                  "signature_kind": signature})
                return "[withheld: credential signature]"
            encoded = value.encode()
            limit = min(20_000, remaining)
            remaining -= min(limit, len(encoded))
            if len(encoded) > limit:
                omissions.append({"field": field, "reason": "text_budget"})
                return encoded[:limit].decode("utf-8", errors="ignore")
            return value
        if isinstance(value, list):
            return [sanitize(item, f"{field}[{index}]") for index, item in enumerate(value)]
        if isinstance(value, dict):
            return {key: sanitize(item, f"{field}.{key}") for key, item in value.items()}
        return value

    # Owner text takes priority over patch excerpts in the shared text budget.
    result = {key: sanitize(raw[key], key) for key in ("title", "body", "threads")}
    for key, value in raw.items():
        if key not in {"title", "body", "threads", "files"}:
            result[key] = sanitize(value, key) if isinstance(value, str) and _signature(value) else value
    result["files"] = []
    for index, file in enumerate(raw["files"]):
        path = file["path"]
        reason = _read_reason(path, policy)
        signature = _signature(file["patch"])
        if reason or signature:
            omissions.append({"path": "[withheld filename]" if _signature(path) else path,
                              "reason": reason or "credential_signature"})
            continue
        result["files"].append(sanitize(file, f"files[{index}]"))
    result["context_omissions"] = omissions
    result["snapshot_identity"] = snapshot.identity
    return result


class ContextIndex:
    def __init__(self) -> None:
        self._items: dict[tuple, list[dict]] = {}
        self._reports: dict[tuple, dict] = {}

    @staticmethod
    def _scope(snapshot: ChangeSnapshot, policy: RepoPolicy) -> tuple:
        if (snapshot.tenant != policy.tenant or snapshot.repo != policy.repo
                or snapshot.repository_id != policy.repository_id
                or snapshot.installation_id != policy.installation_id):
            raise PermissionError("snapshot is outside the trusted repository policy")
        return (policy.tenant, policy.team, policy.installation_id, policy.repository_id,
                policy.repo, snapshot.head_sha, policy.digest)

    def add(self, snapshot: ChangeSnapshot, files: dict[str, str], policy: RepoPolicy) -> None:
        key = self._scope(snapshot, policy)
        if sum(len(v.encode()) for v in files.values()) > policy.max_source_bytes:
            raise ValueError("source context exceeds budget")
        items, omissions = [], []
        graph_coverage: dict[str, int] = {}
        for path, content in sorted(files.items()):
            safe_path(path)
            reason = _read_reason(path, policy)
            if reason:
                omissions.append({"path": "[withheld filename]" if _signature(path) else path,
                                  "reason": reason})
                continue
            signature = _signature(content)
            if signature:
                omissions.append({"path": path, "reason": "credential_signature",
                                  "signature_kind": signature})
                continue
            symbols, imports, coverage = [], [], "lexical-only"
            if path.endswith(".py"):
                try:
                    tree = ast.parse(content)
                    symbols = [n.name for n in ast.walk(tree)
                               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                    imports = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                               for a in n.names]
                    imports += [n.module for n in ast.walk(tree)
                                if isinstance(n, ast.ImportFrom) and n.module]
                    coverage = "python-ast-imports-and-symbols"
                except SyntaxError:
                    coverage = "python-parse-failed"
            graph_coverage[coverage] = graph_coverage.get(coverage, 0) + 1
            items.append({"tenant": policy.tenant, "team": policy.team,
                          "repository_id": policy.repository_id,
                          "repo": snapshot.repo, "revision": snapshot.head_sha, "path": path,
                          "digest": hashlib.sha256(content.encode()).hexdigest(),
                          "content": content, "symbols": symbols, "imports": imports,
                          "coverage": coverage})
        available_paths = {item["path"] for item in items}
        for item in items:
            item["dependencies"] = sorted(python_dependencies(
                item["path"], item["content"], available_paths))
        self._items[key] = items
        self._reports[key] = {"repo": policy.repo, "revision": snapshot.head_sha,
            "repository_id": policy.repository_id, "graph_coverage": graph_coverage,
            "input_files": len(files), "indexed_files": len(items), "retrieved_files": 0,
            "omissions": omissions, "limitations": [
                "Python coverage is static imports/symbols; other languages are lexical only.",
                "Import edges cover repository-root/src layouts, not dynamic imports or runtime hooks.",
                "Provided documents are lower-priority fallback context; relevance is not guaranteed.",
                "When no changed file is indexed, sparse context falls back to the provided safe files.",
                "Credential pattern filtering can omit benign examples and cannot detect every secret."]}

    def retrieve(self, snapshot: ChangeSnapshot, policy: RepoPolicy,
                 budget: int = 100_000) -> list[dict]:
        if isinstance(budget, bool) or not isinstance(budget, int) or not 0 <= budget <= policy.max_source_bytes:
            raise ValueError("retrieval budget must fit the repository source bound")
        key = self._scope(snapshot, policy)
        items = self._items.get(key, [])
        changed = {f.path for f in snapshot.files}
        indexed_changed = [item for item in items if item["path"] in changed]
        direct_imports = {path for item in indexed_changed for path in item["dependencies"]}
        test_paths = {test for path in changed for test in _related_tests(path)}
        priorities = {"changed_file": 0, "direct_import": 1, "direct_importer": 2,
                      "relevant_test": 3, "ancestor_guidance": 4, "provided_document": 5,
                      "sparse_context": 6}

        def reasons(item: dict) -> list[str]:
            path = item["path"]
            selected = []
            if path in changed:
                selected.append("changed_file")
            if path in direct_imports:
                selected.append("direct_import")
            if set(item["dependencies"]) & changed:
                selected.append("direct_importer")
            if path in test_paths:
                selected.append("relevant_test")
            if _ancestor_guidance(path, changed):
                selected.append("ancestor_guidance")
            if not selected and PurePosixPath(path).suffix.lower() in {".md", ".rst", ".txt"}:
                selected.append("provided_document")
            if not selected and not indexed_changed:
                selected.append("sparse_context")
            return selected

        candidates = [(item, reasons(item)) for item in items]
        ranked = sorted(candidates, key=lambda pair: (
            min((priorities[reason] for reason in pair[1]), default=len(priorities)),
            pair[0]["path"]))
        result, used, omissions = [], 0, []
        for item, selection_reasons in ranked:
            if not selection_reasons:
                omissions.append({"path": item["path"], "reason": "not_relevant"})
                continue
            size = len(item["content"].encode())
            if used + size <= budget:
                selected = copy.deepcopy(item)
                selected["selection_reasons"] = selection_reasons
                result.append(selected)
                used += size
            else:
                omissions.append({"path": item["path"], "reason": "retrieval_budget"})
        if key in self._reports:
            report = self._reports[key]
            report["omissions"] = [item for item in report["omissions"]
                                   if item["reason"] not in {"retrieval_budget", "not_relevant"}]
            report["omissions"].extend(omissions)
            report["retrieved_files"] = len(result)
            report["retrieved_bytes"] = used
            report["selections"] = [{"path": item["path"], "reasons": item["selection_reasons"]}
                                    for item in result]
        return result

    def report(self, snapshot: ChangeSnapshot, policy: RepoPolicy) -> dict:
        return copy.deepcopy(self._reports.get(self._scope(snapshot, policy), {
            "repo": policy.repo, "revision": snapshot.head_sha,
            "repository_id": policy.repository_id, "graph_coverage": {}, "indexed_files": 0,
            "retrieved_files": 0, "omissions": [], "limitations": ["No context was indexed."]}))

    def retrieve_related(self, snapshot: ChangeSnapshot, policy: RepoPolicy,
                         related: tuple[tuple[ChangeSnapshot, RepoPolicy], ...],
                         authorized_repositories: tuple[int, ...],
                         budget: int = 100_000) -> list[dict]:
        """Join already indexed sources using trusted explicit repository authority.

        No repository is fetched implicitly. Cross-tenant/team retrieval is refused
        even when an ID is supplied; an authorized same-team source is required.
        """
        self._scope(snapshot, policy)
        if isinstance(budget, bool) or not isinstance(budget, int) or not 0 <= budget <= policy.max_source_bytes:
            raise ValueError("related retrieval budget must fit the repository source bound")
        if any(isinstance(repo, bool) or not isinstance(repo, int) or repo <= 0
               for repo in authorized_repositories):
            raise PermissionError("invalid related repository authority")
        pairs = [(snapshot, policy)]
        seen = {(snapshot.repository_id, snapshot.head_sha)}
        for source, source_policy in related:
            self._scope(source, source_policy)
            if (source_policy.tenant != policy.tenant or source_policy.team != policy.team
                    or source_policy.repository_id not in authorized_repositories):
                raise PermissionError("related repository is outside explicit tenant/team authority")
            identity = source.repository_id, source.head_sha
            if identity not in seen:
                seen.add(identity)
                pairs.append((source, source_policy))
        result, remaining = [], budget
        for source, source_policy in pairs:
            rows = self.retrieve(source, source_policy, min(remaining, source_policy.max_source_bytes))
            result.extend(rows)
            remaining -= sum(len(row["content"].encode()) for row in rows)
        return result


def route(snapshot: ChangeSnapshot, policy: RepoPolicy) -> dict:
    ContextIndex._scope(snapshot, policy)
    lanes, reasons = set(policy.mandatory_lanes), []
    for changed in snapshot.files:
        path = changed.path.lower()
        if any(word in path for word in ("auth", "token", "permission", "crypto", "secret")):
            lanes.add("security")
            reasons.append(f"security-sensitive path: {changed.path}")
        if path.endswith((".c", ".cpp", ".h", ".hpp", ".pyx", ".cu", ".rs")):
            lanes.add("native")
            reasons.append(f"native boundary: {changed.path}")
        if any(word in path for word in ("schema", "migrat", "api", "interface")):
            lanes.add("compatibility")
            reasons.append(f"contract boundary: {changed.path}")
    return {"lanes": sorted(lanes), "reasons": reasons,
            "limitations": ["Non-Python graphs are lexical only; device/platform lanes need explicit checks."]}
