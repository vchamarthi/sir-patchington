"""Final-audit regression: unchanged remote after failed push is safely retryable."""

from types import SimpleNamespace

import pytest
from test_publisher import candidate, policy

from patchington.github import GitHubClient, GitHubTransientError, GitOperationError


def test_failed_push_with_confirmed_unchanged_remote_is_retryable(tmp_path):
    approved_policy = policy()
    approved = candidate(approved_policy)
    snapshot = approved.snapshot
    client = GitHubClient(lambda *args: "synthetic-token")
    client.snapshot = lambda *args: snapshot
    client._remote_refs = lambda *args: {
        "refs/heads/main": snapshot.base_sha,
        "refs/heads/repair": snapshot.head_sha,
    }

    def disconnected_push(*args, **kwargs):
        raise GitOperationError("synthetic disconnected Git transport")

    client._git = disconnected_push
    try:
        with pytest.raises(GitHubTransientError):
            client.publish_candidate(
                approved_policy, snapshot, approved, SimpleNamespace(path=tmp_path)
            )
    finally:
        client._http.close()
