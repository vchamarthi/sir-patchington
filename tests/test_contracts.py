import pytest
from pydantic import ValidationError

from patchington.contracts import CheckSpec, Finding, RepoPolicy, digest_json


def test_policy_is_fail_closed_and_immutable():
    p = RepoPolicy(
        tenant="acme", team="payments", installation_id=1, repository_id=2, repo="acme/shop"
    )
    assert not p.publish_enabled and not p.post_reviews and p.max_attempts == 2
    with pytest.raises(ValidationError):
        p.publish_enabled = True


def test_policy_cannot_enable_publication_without_approvers_and_checks():
    with pytest.raises(ValidationError):
        RepoPolicy(
            tenant="acme",
            team="payments",
            installation_id=1,
            repository_id=2,
            repo="acme/shop",
            publish_enabled=True,
        )


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", ".git/config", "a/../../x", "a\\b"])
def test_findings_reject_unsafe_paths(path):
    with pytest.raises(ValidationError):
        Finding(
            path=path,
            line=1,
            severity="high",
            category="correctness",
            claim="Boundary bug",
            trigger="x=50",
            expected="0",
            reason="new comparison excludes 50",
        )


def test_no_shell_command_string():
    with pytest.raises(ValidationError):
        CheckSpec(name="tests", argv="pytest; curl example.com")


def test_canonical_digest_is_order_independent():
    assert digest_json({"a": 1, "b": 2}) == digest_json({"b": 2, "a": 1})
