"""Exact-artifact approval and reconstruction before restricted publication."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from patchington.artifacts import ArtifactStore
from patchington.contracts import Approval, Candidate, RepoPolicy, Verification, canonical_json
from patchington.github import GitHubClient, GitHubError, GitHubTransientError, run_git
from patchington.gitwork import GitWorkspace


class PublishError(ValueError):
    """Publication refused or requires authoritative reconciliation."""


class PublishTransientError(PublishError):
    """A bounded retry can safely reconcile an uncertain remote publication outcome."""


def _secret(secret: bytes) -> None:
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("Approval secret must contain at least 32 bytes")


def _signature(payload: dict, secret: bytes) -> str:
    return hmac.new(secret, b"sir-patchington-approval-v1\x00" + canonical_json(payload),
                    hashlib.sha256).hexdigest()


def sign_approval(candidate: Candidate, verification: Verification, actor: str,
                  secret: bytes, expires_at: int) -> Approval:
    """Actor identity MUST come from authenticated RBAC, never a model or request body."""
    _secret(secret)
    if not actor or len(actor) > 200 or any(ord(c) < 32 for c in actor):
        raise ValueError("Authenticated actor is required")
    now = int(time.time())
    if isinstance(expires_at, bool) or not now < expires_at <= now + 86400:
        raise ValueError("Approval expiry must be in the next 24 hours")
    if verification.candidate_id != candidate.id or not verification.passed:
        raise ValueError("Cannot approve absent or mismatched passing verification")
    payload = {"candidate_id": candidate.id, "evidence_digest": verification.evidence_digest,
               "policy_digest": candidate.policy_digest, "expected_head": candidate.parent_sha,
               "tested_base": candidate.snapshot.base_sha, "actor": actor,
               "expires_at": expires_at, "nonce": secrets.token_hex(16)}
    return Approval(**payload, signature=_signature(payload, secret))


class Publisher:
    def __init__(self, forge: GitHubClient, artifacts: ArtifactStore, approval_secret: bytes):
        _secret(approval_secret)
        self.forge = forge
        self.artifacts = artifacts
        self._secret = approval_secret

    def _validate(self, policy: RepoPolicy, candidate: Candidate,
                  verification: Verification, approval: Approval) -> None:
        try:
            for model in (policy, candidate, verification, approval):
                type(model).model_validate_json(model.model_dump_json())
        except (TypeError, ValueError, AttributeError) as exc:
            raise PublishError("Publication boundary contract validation failed") from exc
        if not policy.publish_enabled:
            raise PublishError("Publication is disabled")
        payload = approval.model_dump(exclude={"signature"})
        if not hmac.compare_digest(approval.signature, _signature(payload, self._secret)):
            raise PublishError("Invalid approval signature")
        if approval.expires_at <= int(time.time()):
            raise PublishError("Approval has expired")
        if approval.actor not in policy.approvers:
            raise PublishError("Actor is not an authorized repository approver")
        if (approval.candidate_id != candidate.id or approval.evidence_digest != verification.evidence_digest
                or approval.policy_digest != policy.digest or candidate.policy_digest != policy.digest
                or approval.expected_head != candidate.parent_sha
                or candidate.parent_sha != candidate.snapshot.head_sha
                or approval.tested_base != candidate.snapshot.base_sha
                or verification.candidate_id != candidate.id):
            raise PublishError("Approval, candidate, policy or verification identity mismatch")
        snapshot = candidate.snapshot
        if (snapshot.tenant != policy.tenant or snapshot.repository_id != policy.repository_id
                or snapshot.installation_id != policy.installation_id or snapshot.repo != policy.repo):
            raise PublishError("Candidate is outside repository scope")
        # Publishing into another owner's fork cannot be authorized by the base-repo policy.
        if snapshot.is_fork or snapshot.head_repo != policy.repo:
            raise PublishError("Fork publication is unsupported")
        if snapshot.head_ref == snapshot.base_ref or snapshot.head_ref == policy.default_branch:
            raise PublishError("Publication cannot target the protected base/default branch")
        if not verification.environment.startswith("docker:"):
            raise PublishError("Publication requires isolated container verification")
        if not verification.passed or verification.violations:
            raise PublishError("Verification did not pass")
        configured = {check.name: check for check in policy.checks}
        actual = {check.name: check for check in verification.checks}
        if len(actual) != len(verification.checks) or set(actual) != set(configured):
            raise PublishError("Verification does not cover the configured checks exactly")
        for name, spec in configured.items():
            result = actual[name]
            if (not result.passed or result.skipped or result.timed_out
                    or result.candidate_exit != 0 or result.baseline_exit is None
                    or not result.baseline_source_unchanged or not result.candidate_source_unchanged
                    or (spec.regression and result.baseline_exit == 0)
                    or (not spec.regression and result.baseline_exit != 0)):
                raise PublishError("Configured check evidence is not valid")

    def publish(self, policy: RepoPolicy, candidate: Candidate, verification: Verification,
                approval: Approval, workspace: GitWorkspace,
                *, guard: Callable[[], None] | None = None) -> dict:
        self._validate(policy, candidate, verification, approval)
        if guard:
            guard()
        # workspace belongs to the worker and is deliberately never inspected or used.
        expected = {"candidate_id": candidate.id,
                    "checks": [c.model_dump(mode="json") for c in verification.checks],
                    "environment": verification.environment,
                    "violations": list(verification.violations)}
        try:
            if self.artifacts.get_json(verification.evidence_digest) != expected:
                raise PublishError("Stored verification evidence differs from the approved result")
            for result in verification.checks:
                self.artifacts.get(result.baseline_log_digest)
                self.artifacts.get(result.candidate_log_digest)
        except (OSError, ValueError) as exc:
            raise PublishError("Verification artifacts are absent, corrupt or mismatched") from exc
        current = self.forge.snapshot(policy, candidate.snapshot.pr)
        original = candidate.snapshot
        already_written = current.head_sha == candidate.commit_sha
        if already_written:
            if (current.base_sha != original.base_sha or current.base_ref != original.base_ref
                    or current.head_ref != original.head_ref or current.head_repo != original.head_repo):
                raise PublishError("Published candidate now has a different PR base or branch identity")
        elif current.identity != original.identity:
            raise PublishError("PR changed since the approved candidate was captured")
        with tempfile.TemporaryDirectory(prefix="patchington-publisher-") as temp:
            clean = self.forge.fetch(policy, original, Path(temp) / "repository")
            rebuilt = clean.candidate(candidate.patch, original, policy)
            if rebuilt.id != candidate.id:
                raise PublishError("Clean reconstruction differs from the approved candidate")
            tree = run_git(clean.path, ["rev-parse", candidate.commit_sha + "^{tree}"])
            parents = run_git(clean.path, ["rev-list", "--parents", "-n", "1", candidate.commit_sha])
            if tree != candidate.tree_sha or parents.split() != [candidate.commit_sha, candidate.parent_sha]:
                raise PublishError("Reconstructed Git object identity mismatch")
            # Approval may expire while fetching or constructing. Recheck at the write boundary.
            self._validate(policy, candidate, verification, approval)
            if guard:
                guard()
            def write_guard() -> None:
                self._validate(policy, candidate, verification, approval)
                if guard:
                    guard()
            try:
                return self.forge.publish_candidate(policy, original, candidate, clean,
                                                    reconcile_only=already_written,
                                                    guard=write_guard)
            except GitHubTransientError as exc:
                raise PublishTransientError(str(exc)) from exc
            except GitHubError as exc:
                raise PublishError(str(exc)) from exc
