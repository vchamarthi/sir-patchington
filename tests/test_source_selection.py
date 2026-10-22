import subprocess

import pytest

from patchington.contracts import RepoPolicy
from patchington.gitwork import GitWorkspace, WorkspaceError


def repository(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for path, content in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture",
                    "-c", "user.email=f@example.com", "commit", "-qm", "fixture"], check=True)
    revision = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
    return GitWorkspace(repo), revision


def test_source_slice_acquires_imports_tests_and_ancestor_guidelines_before_siblings(tmp_path):
    files = {
        "src/pkg/worker.py": "from common.validation import validate\nfrom .rules import RULE\n",
        "src/common/validation.py": "def validate(value):\n    return value > 0\n",
        "src/pkg/rules.py": "RULE = 1\n",
        "tests/pkg/test_worker.py": "def test_worker():\n    assert True\n",
        "test/pkg/worker_test.py": "def test_contract():\n    assert True\n",
        "AGENTS.md": "Require input validation.\n",
        "src/pkg/AGENTS.md": "Preserve the worker contract.\n",
        "src/review-guidelines.md": "Check authorization.\n",
        "src/pkg/a_unrelated.py": "#" * 1000,
        "other/unrelated.py": "NEVER_RELATED = True\n",
    }
    workspace, revision = repository(tmp_path, files)
    relevant = set(files) - {"src/pkg/a_unrelated.py", "other/unrelated.py"}
    budget = sum(len(files[path].encode()) for path in relevant)
    result = workspace.source_slice(revision, ("src/pkg/worker.py",), budget)
    assert set(result) == relevant
    assert list(result)[0] == "src/pkg/worker.py"
    assert workspace.source_slice_omissions["src/pkg/a_unrelated.py"] == "byte_budget"
    assert workspace.source_slice_coverage["dependency_files"] == 2
    assert workspace.source_slice_coverage["matching_test_files"] == 2
    assert workspace.source_slice_coverage["guideline_files"] == 3
    assert workspace.source_slice(revision, ("src/pkg/worker.py",), budget) == result


def test_source_slice_discovers_safe_python_callers_and_tests_outside_changed_directory(tmp_path):
    files = {'src/pay/fees.py': 'def fee(x):\n    return x + 1\n',
             'src/checkout/cart.py': 'from pay.fees import fee\ndef cart(x):\n    return fee(x)\n',
             'tests/test_fee_contract.py': 'from pay.fees import fee\ndef test_fee():\n    assert fee(0) == 1\n',
             'private/excluded.py': 'not authorized\n'}
    workspace, revision = repository(tmp_path, files)
    p = RepoPolicy(tenant='acme', team='dev', installation_id=1, repository_id=2,
                   repo='acme/repo')
    result = workspace.source_slice(revision, ('src/pay/fees.py',), policy=p)
    assert set(result) == set(files) - {'private/excluded.py'}
    assert workspace.source_slice_coverage['python_discovery_files'] == 2


def test_policy_filtered_paths_do_not_consume_acquisition_slots_or_budget(tmp_path, monkeypatch):
    workspace, revision = repository(tmp_path, {
        "src/main.py": "from common.helper import VALUE\n",
        "src/.env": "not-a-real-credential\n" * 100,
        "src/private.py": "#" * 1000,
        "src/common/helper.py": "VALUE = 1\n",
    })
    policy = RepoPolicy(tenant="acme", team="dev", installation_id=1, repository_id=2,
                        repo="acme/repo", context_exclusions=("src/private.py",))
    monkeypatch.setattr(workspace, "MAX_CONTEXT_FILES", 2)
    result = workspace.source_slice(revision, ("src/.env", "src/private.py", "src/main.py"),
                                    100, policy=policy)
    assert set(result) == {"src/main.py", "src/common/helper.py"}
    assert workspace.source_slice_omissions["src/.env"] == "credential_filename"
    assert workspace.source_slice_omissions["src/private.py"] == "context_exclusion"


def test_credential_content_does_not_consume_selected_byte_budget(tmp_path):
    secret_example = 'API_KEY = "' + "sk-" + "a" * 24 + '"\n'
    files = {"src/main.py": "from common.helper import VALUE\n",
             "src/secret_example.py": secret_example,
             "src/common/helper.py": "VALUE = 1\n"}
    workspace, revision = repository(tmp_path, files)
    policy = RepoPolicy(tenant="acme", team="dev", installation_id=1, repository_id=2,
                        repo="acme/repo")
    budget = len(files["src/main.py"].encode()) + len(files["src/common/helper.py"].encode())
    result = workspace.source_slice(revision, ("src/secret_example.py", "src/main.py"),
                                    budget, policy=policy)
    assert set(result) == {"src/main.py", "src/common/helper.py"}
    assert workspace.source_slice_omissions["src/secret_example.py"] == "credential_signature"
    assert workspace.source_slice_coverage["selected_bytes"] == budget


def test_source_slice_acquisition_is_pinned_and_reads_bounded_candidates(tmp_path, monkeypatch):
    files = {"src/main.py": "from deps import d0, d1, d2, d3, d4, d5\n"}
    files.update({f"src/deps/d{number}.py": f"VALUE = {number}\n" for number in range(6)})
    files.update({f"unrelated/data{number}.py": "NOT_READ = True\n" for number in range(20)})
    workspace, revision = repository(tmp_path, files)
    (workspace.path / "src/main.py").write_text("DIRTY = True\n")
    monkeypatch.setattr(workspace, "MAX_CONTEXT_FILES", 3)
    original_git, blob_reads = workspace._git, []

    def tracked_git(args, **kwargs):
        if args[:2] == ["cat-file", "blob"]:
            blob_reads.append(args[-1])
        return original_git(args, **kwargs)

    monkeypatch.setattr(workspace, "_git", tracked_git)
    result = workspace.source_slice(revision, ("src/main.py",), budget=1000)
    assert result["src/main.py"] == files["src/main.py"]
    assert len(result) == len(blob_reads) == 3
    assert any(reason == "file_limit" for reason in workspace.source_slice_omissions.values())
    assert all("unrelated/" not in path for path in result)


def test_source_slice_caps_aggregate_budget_and_reports_effective_bound(tmp_path, monkeypatch):
    workspace, revision = repository(tmp_path, {
        "src/main.py": "from deps.helper import VALUE\n" + "#" * 21,
        "src/deps/helper.py": "VALUE = 1\n" + "#" * 30,
    })
    monkeypatch.setattr(workspace, "MAX_BLOB_BYTES", 80)
    result = workspace.source_slice(revision, ("src/main.py",), budget=3_000_000)
    assert sum(len(content.encode()) for content in result.values()) <= 80
    assert workspace.source_slice_coverage["requested_byte_budget"] == 3_000_000
    assert workspace.source_slice_coverage["effective_byte_budget"] == 80
    assert workspace.source_slice_coverage["budget_capped"] == 1


def test_source_slice_masks_credential_looking_omission_filenames(tmp_path):
    credential_name = "src/ghp_" + "a" * 30 + ".py"
    workspace, revision = repository(tmp_path, {"src/main.py": "VALUE = 1\n",
                                              credential_name: "not-a-real-secret\n"})
    workspace.source_slice(revision, (credential_name, "src/main.py"))
    assert credential_name not in str(workspace.source_slice_omissions)
    assert "credential_filename" in workspace.source_slice_omissions.values()


def test_discarded_content_reads_have_an_absolute_byte_bound(tmp_path, monkeypatch):
    example = 'API_KEY = "' + "sk-" + "a" * 24 + '"\n'
    files = {f"src/example{number}.py": example for number in range(10)}
    files["src/main.py"] = "VALUE = 1\n"
    workspace, revision = repository(tmp_path, files)
    monkeypatch.setattr(workspace, "MAX_SOURCE_BYTES", 100)
    original_git, inspected_sizes = workspace._git, []

    def tracked_git(args, **kwargs):
        output = original_git(args, **kwargs)
        if args[:2] == ["cat-file", "blob"]:
            inspected_sizes.append(len(output))
        return output

    monkeypatch.setattr(workspace, "_git", tracked_git)
    workspace.source_slice(revision, tuple(files), budget=100)
    assert sum(inspected_sizes) <= workspace.MAX_SOURCE_BYTES
    assert "inspection_byte_budget" in workspace.source_slice_omissions.values()
    assert workspace.source_slice_coverage["inspected_bytes"] == sum(inspected_sizes)


@pytest.mark.parametrize("budget", [True, False, 1.5, "100"])
def test_source_slice_refuses_non_integer_budgets(tmp_path, budget):
    workspace, revision = repository(tmp_path, {"src/main.py": "VALUE = 1\n"})
    with pytest.raises(WorkspaceError, match="invalid bounded"):
        workspace.source_slice(revision, ("src/main.py",), budget=budget)
