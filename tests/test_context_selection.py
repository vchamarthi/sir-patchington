import pytest

from patchington import context
from patchington.contracts import ChangedFile, ChangeSnapshot, RepoPolicy


def policy(**updates):
    return RepoPolicy(tenant="acme", team="payments", installation_id=1, repository_id=2,
                      repo="acme/shop").model_copy(update=updates)


def snapshot(*paths):
    return ChangeSnapshot(
        tenant="acme", repository_id=2, installation_id=1, repo="acme/shop", pr=1,
        base_sha="a" * 40, head_sha="b" * 40, head_ref="feature", head_repo="acme/shop",
        files=tuple(ChangedFile(path=path) for path in paths))


def select(files, *changed, budget=100_000, repo_policy=None):
    index = context.ContextIndex()
    source, configured = snapshot(*changed), repo_policy or policy()
    index.add(source, files, configured)
    return index.retrieve(source, configured, budget), index.report(source, configured)


@pytest.mark.parametrize(("path", "source", "expected"), [
    ("src/shop/service.py", "import shop.models\nfrom shop import helpers", {
        "src/shop/models.py", "src/shop/__init__.py", "src/shop/helpers.py"}),
    ("src/shop/service.py", "from .models import Model\nfrom . import helpers", {
        "src/shop/models.py", "src/shop/__init__.py", "src/shop/helpers.py"}),
    ("src/shop/nested/service.py", "from ..models import Model", {"src/shop/models.py"}),
    ("worker.py", "from shop.models import Model", {"src/shop/models.py"}),
    ("src/shop/service.py", "import other.models", {"other/models.py"}),
    ("src/shop/service.py", "import importlib\nimportlib.import_module('shop.helpers')", set()),
    ("src/shop/service.py", "from ... import outside", set()),
    ("src/shop/service.py", "this is broken Python!", set()),
])
def test_static_python_dependencies_resolve_actual_modules_without_basename_guessing(
        path, source, expected):
    available = {"src/shop/service.py", "src/shop/models.py", "src/shop/__init__.py",
                 "src/shop/helpers.py", "other/models.py", "docs/models.py"}
    assert context.python_dependencies(path, source, available) == expected


def test_context_prioritizes_direct_imports_and_unchanged_callers_over_irrelevant_siblings():
    files = {
        "src/shop/service.py": "from .models import Model\nvalue = Model()\n",
        "src/shop/models.py": "class Model: pass\n",
        "src/worker.py": "from shop.service import value\n",
        "src/aaa_unrelated.py": "x = 0\n" * 8,
        "src/shop/sibling.py": "y = 2\n",
    }
    rows, report = select(files, "src/shop/service.py")
    assert {row["path"] for row in rows} == {
        "src/shop/service.py", "src/shop/models.py", "src/worker.py"}
    reasons = {row["path"]: row["selection_reasons"] for row in rows}
    assert "changed_file" in reasons["src/shop/service.py"]
    assert "direct_import" in reasons["src/shop/models.py"]
    assert "direct_importer" in reasons["src/worker.py"]
    assert {item["path"] for item in report["omissions"]
            if item["reason"] == "not_relevant"} == {
                "src/aaa_unrelated.py", "src/shop/sibling.py"}


def test_unrelated_alphabetical_files_cannot_starve_imported_context_budget():
    changed, dependency = "from shop.z_dependency import value\n", "value = 3\n"
    files = {"src/shop/service.py": changed, "src/shop/z_dependency.py": dependency,
             "src/aaa_unrelated.py": "x = 0\n"}
    rows, report = select(files, "src/shop/service.py", budget=len((changed + dependency).encode()))
    assert [row["path"] for row in rows] == [
        "src/shop/service.py", "src/shop/z_dependency.py"]
    assert {"path": "src/aaa_unrelated.py", "reason": "not_relevant"} in report["omissions"]


def test_named_tests_match_module_path_and_do_not_collide_on_shared_basename():
    rows, report = select({
        "src/shop/service.py": "value = 1\n",
        "tests/shop/test_service.py": "def test_service(): pass\n",
        "tests/other/test_service.py": "def test_service(): pass\n",
        "tests/test_service.py": "def test_service(): pass\n",
        "tests/integration/test_checkout.py": "from shop.service import value\n",
    }, "src/shop/service.py")
    paths = {row["path"] for row in rows}
    assert "tests/shop/test_service.py" in paths
    assert "tests/integration/test_checkout.py" in paths
    assert "tests/other/test_service.py" not in paths
    assert "tests/test_service.py" not in paths
    assert any(item["reason"] == "not_relevant" for item in report["omissions"])


def test_named_tests_support_singular_test_directory():
    rows, _ = select({"src/shop/service.py": "value = 1\n",
                      "test/shop/service_test.py": "def test_service(): pass\n"},
                     "src/shop/service.py", repo_policy=policy(context_paths=("src/", "test/")))
    assert {row["path"] for row in rows} == {
        "src/shop/service.py", "test/shop/service_test.py"}


def test_ancestor_guides_precede_unrelated_provided_document_fallback():
    changed = "value = 1\n"
    files = {"src/shop/service.py": changed, "AGENTS.md": "root guide\n",
             "src/AGENTS.md": "source guide\n", "src/shop/CONTRIBUTING.md": "shop guide\n",
             "docs/aaa_overview.md": "large unrelated overview\n" * 4}
    p = policy(context_paths=("src/", "docs/", "AGENTS.md"))
    rows, report = select(files, "src/shop/service.py", budget=len(changed.encode()) + 40,
                          repo_policy=p)
    assert {row["path"] for row in rows} == {
        "src/shop/service.py", "AGENTS.md", "src/AGENTS.md", "src/shop/CONTRIBUTING.md"}
    assert all("ancestor_guidance" in row["selection_reasons"] for row in rows[1:])
    assert {"path": "docs/aaa_overview.md", "reason": "retrieval_budget"} in report["omissions"]


def test_explicit_documents_and_sparse_context_have_a_documented_fallback_reason():
    rows, report = select({"docs/requirements.md": "requirement", "src/shared.py": "value = 1"},
                          "src/missing.py")
    assert {row["path"] for row in rows} == {"docs/requirements.md", "src/shared.py"}
    assert "provided_document" in next(
        row for row in rows if row["path"].endswith(".md"))["selection_reasons"]
    assert "sparse_context" in next(
        row for row in rows if row["path"].endswith(".py"))["selection_reasons"]
    assert any("sparse" in limit.lower() for limit in report["limitations"])


def test_selection_omissions_reset_between_retrievals_and_preserve_secret_exclusions():
    files = {"src/shop/service.py": "from . import helpers\n",
             "src/shop/helpers.py": "value = 1\n", "src/shop/unrelated.py": "value = 2\n",
             "src/shop/secrets.py": "SYNTHETIC_PRIVATE_VALUE"}
    p, source, index = policy(), snapshot("src/shop/service.py"), context.ContextIndex()
    index.add(source, files, p)
    index.retrieve(source, p, budget=0)
    rows = index.retrieve(source, p)
    report = index.report(source, p)
    assert {row["path"] for row in rows} == {"src/shop/service.py", "src/shop/helpers.py"}
    assert not any(item["reason"] == "retrieval_budget" for item in report["omissions"])
    assert len([item for item in report["omissions"] if item["reason"] == "not_relevant"]) == 1
    assert "SYNTHETIC_PRIVATE_VALUE" not in str(rows) + str(report)


def test_selection_keeps_whole_utf8_files_inside_byte_budget():
    source = "message = 'é'\n"
    rows, report = select({"src/shop/service.py": source}, "src/shop/service.py",
                          budget=len(source.encode()) - 1)
    assert rows == []
    assert report["retrieved_bytes"] == 0
    assert {"path": "src/shop/service.py", "reason": "retrieval_budget"} in report["omissions"]


def test_changed_implementation_precedes_changed_tests_and_large_qualification_documents():
    files = {"docs/aaa_qualification.json": "q" * 70_000,
             "src/runtime.py": "# implementation\n" + "x = 1\n" * 4_999,
             "tests/aaa_runtime.py": "# fixture test\n" + "x = 2\n" * 4_999}
    rows, report = select(files, *files, budget=100_000)
    assert rows[0]["path"] == "src/runtime.py"
    assert {"path": "docs/aaa_qualification.json", "reason": "retrieval_budget"} in report["omissions"]
    assert {row["path"] for row in rows} == {"src/runtime.py", "tests/aaa_runtime.py"}


def test_model_context_adds_explicit_line_numbers_without_mutating_canonical_evidence():
    import copy
    import hashlib

    rows, _ = select({"src/service.py": "first = 1\n\nlast = 'é'\n"}, "src/service.py")
    before = copy.deepcopy(rows)
    viewed = context.model_context(rows)
    assert viewed[0]["numbered_content"] == "1: first = 1\n2: \n3: last = 'é'"
    assert viewed[0]["content"] == rows[0]["content"]
    assert viewed[0]["digest"] == hashlib.sha256(rows[0]["content"].encode()).hexdigest()
    viewed[0]["symbols"].append("model callback mutation")
    assert rows == before
