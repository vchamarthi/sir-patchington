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
        self._items[key] = items
        self._reports[key] = {"repo": policy.repo, "revision": snapshot.head_sha,
            "repository_id": policy.repository_id, "graph_coverage": graph_coverage,
            "input_files": len(files), "indexed_files": len(items), "retrieved_files": 0,
            "omissions": omissions, "limitations": [
                "Python coverage is static imports/symbols; other languages are lexical only.",
                "Credential pattern filtering can omit benign examples and cannot detect every secret."]}

    def retrieve(self, snapshot: ChangeSnapshot, policy: RepoPolicy,
                 budget: int = 100_000) -> list[dict]:
        if isinstance(budget, bool) or not isinstance(budget, int) or not 0 <= budget <= policy.max_source_bytes:
            raise ValueError("retrieval budget must fit the repository source bound")
        key = self._scope(snapshot, policy)
        items = self._items.get(key, [])
        changed = {f.path for f in snapshot.files}
        modules = {p.removesuffix(".py").replace("/", ".") for p in changed}
        ranked = sorted(items, key=lambda item: (
            item["path"] not in changed,
            not bool(set(item["imports"]) & modules), item["path"]))
        result, used, omissions = [], 0, []
        for item in ranked:
            size = len(item["content"].encode())
            if used + size <= budget:
                result.append(copy.deepcopy(item))
                used += size
            else:
                omissions.append({"path": item["path"], "reason": "retrieval_budget"})
        if key in self._reports:
            report = self._reports[key]
            report["omissions"] = [item for item in report["omissions"]
                                   if item["reason"] != "retrieval_budget"] + omissions
            report["retrieved_files"] = len(result)
            report["retrieved_bytes"] = used
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
