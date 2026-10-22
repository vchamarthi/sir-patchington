"""Immutable boundary objects; no model output grants authority."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")]
Repo = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")]


def canonical_json(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def safe_path(value: str) -> str:
    p = PurePosixPath(value)
    if (
        not value
        or len(value) > 500
        or p.is_absolute()
        or "\\" in value
        or any(part in {"..", ".git", ""} for part in value.split("/"))
        or any(ord(c) < 32 for c in value)
        or str(p) != value
    ):
        raise ValueError("unsafe repository path")
    return value


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CheckSpec(Frozen):
    name: Name
    argv: tuple[str, ...]
    required: bool = True
    regression: bool = False
    timeout: int = Field(default=60, ge=1, le=600)
    lane: Name = "tests"

    @field_validator("argv")
    @classmethod
    def command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) > 100 or any(not v or "\x00" in v for v in value):
            raise ValueError("a bounded explicit argv is required")
        if value[0].rsplit("/", 1)[-1] in {"sh", "bash", "zsh", "curl", "wget", "sudo"}:
            raise ValueError("shell and network utility entrypoints are disallowed")
        return value


class RepoPolicy(Frozen):
    tenant: Name
    team: Name
    installation_id: int = Field(gt=0)
    repository_id: int = Field(gt=0)
    write_installation_id: int | None = Field(default=None, gt=0)
    repo: Repo
    default_branch: str = "main"
    allow_forks: bool = False
    allow_drafts: bool = False
    repair_enabled: bool = True
    publish_enabled: bool = False
    post_reviews: bool = False
    approvers: tuple[str, ...] = ()
    max_attempts: int = Field(default=2, ge=1, le=5)
    max_files: int = Field(default=100, ge=1, le=500)
    max_source_bytes: int = Field(default=2_000_000, ge=1024, le=20_000_000)
    max_patch_bytes: int = Field(default=100_000, ge=1, le=1_000_000)
    allowed_paths: tuple[str, ...] = ("src/",)
    denied_paths: tuple[str, ...] = (".git", ".github/", ".env", "deploy/", "Dockerfile")
    context_paths: tuple[str, ...] = ("src/", "tests/", "docs/")
    context_exclusions: tuple[str, ...] = ()
    checks: tuple[CheckSpec, ...] = ()
    requirements: tuple[str, ...] = ()
    mandatory_lanes: tuple[str, ...] = ("correctness",)
    max_findings: int = Field(default=20, ge=1, le=100)
    required_github_checks: tuple[str, ...] = ()
    required_github_check_app_ids: tuple[int, ...] = ()
    ci_poll_attempts: int = Field(default=30, ge=2, le=100)

    @model_validator(mode="after")
    def safe_publication(self) -> RepoPolicy:
        if self.publish_enabled and (
            not self.approvers or not self.checks or not any(c.required for c in self.checks)
        ):
            raise ValueError("publishing requires approvers and mandatory checks")
        if len({c.name for c in self.checks}) != len(self.checks):
            raise ValueError("check names must be unique")
        if any(value <= 0 for value in self.required_github_check_app_ids):
            raise ValueError("CI producer App IDs must be positive")
        if len(set(self.required_github_checks)) != len(self.required_github_checks):
            raise ValueError("required GitHub check names must be unique")
        for path in (
            self.allowed_paths + self.denied_paths + self.context_paths + self.context_exclusions
        ):
            if path != ".git":
                safe_path(path.rstrip("/"))
        return self

    @property
    def digest(self) -> str:
        return digest_json(self)

    def permits_path(self, path: str) -> bool:
        safe_path(path)
        if any(p.startswith(".env") or p.endswith((".key", ".pem")) for p in path.split("/")):
            return False
        return any(
            path.startswith(p) if p.endswith("/") else path == p for p in self.allowed_paths
        ) and not any(
            path.startswith(p) if p.endswith("/") else path == p for p in self.denied_paths
        )


class ChangedFile(Frozen):
    path: str
    status: str = "modified"
    patch: str = ""
    previous_path: str | None = None

    _path = field_validator("path")(safe_path)


class ReviewThread(Frozen):
    id: str
    body: str
    author: str
    path: str | None = None
    line: int | None = None
    resolved: bool = False
    outdated: bool = False
    updated_at: str = ""


class ChangeSnapshot(Frozen):
    tenant: Name
    repository_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    repo: Repo
    pr: int = Field(gt=0)
    base_sha: SHA
    head_sha: SHA
    base_ref: str = "main"
    head_ref: str
    head_repo: Repo
    is_fork: bool = False
    title: str = ""
    body: str = ""
    files: tuple[ChangedFile, ...] = ()
    threads: tuple[ReviewThread, ...] = ()

    @field_validator("head_ref", "base_ref")
    @classmethod
    def ref(cls, value: str) -> str:
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,199}", value)
            or ".." in value
            or "//" in value
            or value.endswith(("/", ".", ".lock"))
        ):
            raise ValueError("invalid branch ref")
        return value

    @property
    def feedback_digest(self) -> str:
        return digest_json([t.model_dump(mode="json") for t in self.threads])

    @property
    def identity(self) -> str:
        return digest_json(self)


class Finding(Frozen):
    path: str
    line: int = Field(ge=1)
    severity: Literal["critical", "high", "medium", "low"]
    category: str
    claim: str = Field(min_length=5, max_length=4000)
    trigger: str = Field(min_length=1, max_length=2000)
    expected: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=4000)
    evidence: tuple[str, ...] = ()
    status: Literal["proposed", "supported", "refuted", "needs_evidence", "requires_owner"] = (
        "proposed"
    )

    _path = field_validator("path")(safe_path)

    @property
    def fingerprint(self) -> str:
        return digest_json(
            {
                "path": self.path,
                "line": self.line,
                "category": self.category,
                "claim": self.claim.strip(),
                "trigger": self.trigger,
                "expected": self.expected,
            }
        )


class ReviewOutput(Frozen):
    findings: tuple[Finding, ...] = Field(default=(), max_length=100)
    limitations: tuple[str, ...] = ()


class SourceCitation(Frozen):
    path: str
    source_digest: Digest
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=4000)

    _path = field_validator("path")(safe_path)

    @model_validator(mode="after")
    def bounded_range(self) -> SourceCitation:
        if self.end_line < self.start_line or self.end_line - self.start_line >= 80:
            raise ValueError("citation requires an ordered bounded source range")
        if not self.quote.strip():
            raise ValueError("citation must contain source text")
        return self


class FindingJudgment(Frozen):
    fingerprint: Digest
    status: Literal["supported", "refuted", "needs_evidence", "requires_owner"]
    rationale: str = Field(min_length=5, max_length=4000)
    counterargument: str = Field(min_length=5, max_length=4000)
    citations: tuple[SourceCitation, ...] = Field(default=(), max_length=8)


class ReviewVerification(Frozen):
    judgments: tuple[FindingJudgment, ...] = Field(default=(), max_length=100)
    limitations: tuple[str, ...] = ()


class RepairOutput(Frozen):
    patch: str = Field(max_length=1_000_000)
    rationale: str = Field(min_length=1, max_length=4000)


class Candidate(Frozen):
    snapshot: ChangeSnapshot
    patch: str
    parent_sha: SHA
    tree_sha: SHA
    commit_sha: SHA
    timestamp: int = Field(ge=0)
    policy_digest: Digest

    @model_validator(mode="after")
    def parent_is_tested_head(self) -> Candidate:
        if self.parent_sha != self.snapshot.head_sha:
            raise ValueError("candidate parent differs from captured head")
        return self

    @property
    def id(self) -> str:
        return digest_json(self)


class CheckResult(Frozen):
    name: str
    baseline_exit: int | None
    candidate_exit: int | None
    baseline_log_digest: Digest
    candidate_log_digest: Digest
    baseline_source_unchanged: bool = True
    candidate_source_unchanged: bool = True
    timed_out: bool = False
    skipped: bool = False
    passed: bool
    detail: str = ""


class Verification(Frozen):
    candidate_id: Digest
    passed: bool
    checks: tuple[CheckResult, ...]
    evidence_digest: Digest
    environment: str
    violations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def reject_false_green(self) -> Verification:
        if self.passed and (
            not self.checks
            or self.violations
            or any(
                not c.passed
                or c.skipped
                or c.timed_out
                or c.candidate_exit != 0
                or not c.candidate_source_unchanged
                or not c.baseline_source_unchanged
                for c in self.checks
            )
        ):
            raise ValueError("verification cannot pass absent or invalid evidence")
        return self


class Approval(Frozen):
    candidate_id: Digest
    evidence_digest: Digest
    policy_digest: Digest
    expected_head: SHA
    tested_base: SHA
    actor: str
    expires_at: int
    nonce: str
    signature: Digest
