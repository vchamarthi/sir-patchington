import pytest

from patchington.context import ContextIndex, route
from patchington.contracts import ChangedFile, ChangeSnapshot, RepoPolicy, ReviewOutput
from patchington.models import FixtureModel, ModelError


def policy():
    return RepoPolicy(tenant="acme", team="payments", installation_id=1, repository_id=2,
                      repo="acme/shop")


def snapshot(path="src/auth.py"):
    return ChangeSnapshot(tenant="acme", repository_id=2, installation_id=1, repo="acme/shop",
                          pr=1, base_sha="a"*40, head_sha="b"*40, head_ref="feature",
                          head_repo="acme/shop", files=(ChangedFile(path=path),))


def test_context_never_crosses_tenant_or_team():
    index = ContextIndex()
    index.add(snapshot(), {"src/auth.py": "def authorize(): return False"}, policy())
    assert index.retrieve(snapshot(), policy())[0]["revision"] == "b"*40
    other = policy().model_copy(update={"tenant": "elsewhere"})
    with pytest.raises(PermissionError):
        index.retrieve(snapshot(), other)


def test_context_keeps_revision_and_source_hash():
    index=ContextIndex()
    index.add(snapshot(), {"src/auth.py": "import src.users\ndef authorize(): return False"}, policy())
    item=index.retrieve(snapshot(),policy())[0]
    assert item["symbols"] == ["authorize"] and len(item["digest"])==64


def test_risk_mandatory_security_and_native_lanes():
    assert "security" in route(snapshot(),policy())["lanes"]
    assert "native" in route(snapshot("src/fft.cpp"),policy())["lanes"]


def test_fixture_missing_stage_never_succeeds():
    with pytest.raises(ModelError):
        FixtureModel({}).generate("review",{},ReviewOutput)


def test_fixture_output_is_schema_validated():
    with pytest.raises(ModelError):
        FixtureModel({"review": {"unknown":"fabrication"}}).generate("review",{},ReviewOutput)


@pytest.mark.parametrize("path", [".env", "src/.env.production", "src/id_rsa",
    "src/credentials.json", "docs/client_secret.json", "src/private.key", "docs/cert.pem"])
def test_credential_named_files_never_enter_model_context(path):
    index = ContextIndex()
    index.add(snapshot(), {path: "synthetic private data", "src/auth.py": "value = 1"}, policy())
    assert [item["path"] for item in index.retrieve(snapshot(), policy())] == ["src/auth.py"]
    report = index.report(snapshot(), policy())
    assert any(item["path"] == path for item in report["omissions"])
    assert "synthetic private data" not in str(report)


@pytest.mark.parametrize("content", [
    "-----BEGIN PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----",
    "token = 'ghp_abcdefghijklmnopqrstuvwxyz1234567890'",
    'API_KEY = "synthetic_long_secret_123456"',  # gitleaks:allow synthetic detector fixture
    '{"api_key": "synthetic_long_secret_123456"}',  # gitleaks:allow synthetic detector fixture
    "aws = 'AKIAABCDEFGHIJKLMNOP'",  # gitleaks:allow synthetic detector fixture
])
def test_literal_credential_signatures_are_omitted_without_echoing_values(content):
    index = ContextIndex()
    index.add(snapshot(), {"src/auth.py": content}, policy())
    assert index.retrieve(snapshot(), policy()) == []
    report = index.report(snapshot(), policy())
    assert report["omissions"][0]["reason"] == "credential_signature"
    assert content not in str(report)


def test_read_policy_is_distinct_from_patch_paths_and_reports_graph_coverage():
    p = policy().model_copy(update={"context_paths": ("src/", "docs/"),
                                   "context_exclusions": ("docs/private/",)})
    index = ContextIndex()
    index.add(snapshot(), {"src/auth.py": "def authorize(): return False",
        "docs/requirements.md": "Business requirement", "docs/private/design.md": "private",
        "deploy/runtime.yaml": "not model readable", "tests/test_auth.py": "value = 1"}, p)
    items = index.retrieve(snapshot(), p)
    assert {item["path"] for item in items} == {"src/auth.py", "docs/requirements.md"}
    report = index.report(snapshot(), p)
    assert report["graph_coverage"]["python-ast-imports-and-symbols"] == 1
    assert report["graph_coverage"]["lexical-only"] == 1
    assert len(report["omissions"]) == 3


def test_cross_team_context_is_empty_and_related_repositories_require_explicit_authority():
    index = ContextIndex()
    primary, p = snapshot(), policy()
    index.add(primary, {"src/auth.py": "value = 1"}, p)
    other_team = p.model_copy(update={"team": "platform"})
    assert index.retrieve(primary, other_team) == []
    related_policy = p.model_copy(update={"repository_id": 3, "repo": "acme/shared"})
    related = primary.model_copy(update={"repository_id": 3, "repo": "acme/shared",
                                         "head_repo": "acme/shared", "head_sha": "c" * 40})
    index.add(related, {"src/shared.py": "value = 2"}, related_policy)
    with pytest.raises(PermissionError):
        index.retrieve_related(primary, p, ((related, related_policy),), ())
    result = index.retrieve_related(primary, p, ((related, related_policy),), (3,))
    assert {item["repository_id"] for item in result} == {2, 3}
    assert {item["revision"] for item in result} == {"b" * 40, "c" * 40}
    cross = related_policy.model_copy(update={"team": "platform"})
    with pytest.raises(PermissionError):
        index.retrieve_related(primary, p, ((related, cross),), (3,))


def test_retrieval_budget_omissions_are_explicit_and_related_results_share_one_budget():
    index = ContextIndex()
    index.add(snapshot(), {"src/a.py": "a" * 20, "src/b.py": "b" * 20}, policy())
    result = index.retrieve(snapshot(), policy(), budget=20)
    assert sum(len(item["content"].encode()) for item in result) == 20
    assert any(item["reason"] == "retrieval_budget" for item in index.report(snapshot(), policy())["omissions"])


def test_ordinary_token_handling_code_is_retained_and_results_cannot_mutate_the_index():
    index = ContextIndex()
    source = "def token_prefix(): return 'ghp_'"
    index.add(snapshot(), {"src/auth.py": source}, policy())
    items = index.retrieve(snapshot(), policy())
    assert items[0]["content"] == source
    items[0]["symbols"].append("caller mutation")
    assert "caller mutation" not in index.retrieve(snapshot(), policy())[0]["symbols"]


def test_model_snapshot_withholds_excluded_and_secret_patches_and_comment_text():
    from patchington.context import model_snapshot
    from patchington.contracts import ReviewThread
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    source = snapshot().model_copy(update={
        "title": "Issue " + secret, "body": "Owner requirement is explicit",
        "files": (ChangedFile(path="src/auth.py", patch="@@ -1 +1 @@\n-safe\n+safe"),
                  ChangedFile(path="src/.env", patch="SYNTHETIC_PRIVATE_VALUE"),
                  ChangedFile(path="src/other.py", patch="token = '" + secret + "'")),
        "threads": (ReviewThread(id="T1", author="owner", body="secret is " + secret),
                    ReviewThread(id="T2", author="owner", body="Follow the owner requirement"))})
    result = model_snapshot(source, policy())
    assert [file["path"] for file in result["files"]] == ["src/auth.py"]
    assert result["threads"][1]["body"] == "Follow the owner requirement"
    assert secret not in str(result) and "SYNTHETIC_PRIVATE_VALUE" not in str(result)
    assert len(result["context_omissions"]) == 4
    assert result["snapshot_identity"] == source.identity
    assert source.title.endswith(secret)


def test_model_snapshot_comment_text_is_bounded_and_private_key_payload_is_wholly_withheld():
    from patchington.context import model_snapshot
    from patchington.contracts import ReviewThread
    private = "-----BEGIN RSA PRIVATE KEY-----\nPRIVATE_KEY_BODY\n-----END RSA PRIVATE KEY-----"
    source = snapshot().model_copy(update={"body": private, "threads": (
        ReviewThread(id="T1", author="owner", body="A" * 25_000),)})
    result = model_snapshot(source, policy())
    assert "PRIVATE_KEY_BODY" not in str(result)
    assert len(result["threads"][0]["body"].encode()) <= 20_000
    assert any(item["reason"] == "text_budget" for item in result["context_omissions"])


def test_credential_in_filename_is_not_echoed_into_context_or_omission_metadata():
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    index = ContextIndex()
    index.add(snapshot(), {"src/" + secret + ".py": "value = 1"}, policy())
    assert index.retrieve(snapshot(), policy()) == []
    assert secret not in str(index.report(snapshot(), policy()))


def test_snapshot_identity_fields_survive_exhausted_comment_text_budget():
    from patchington.context import model_snapshot
    from patchington.contracts import ReviewThread
    source = snapshot().model_copy(update={"threads": tuple(
        ReviewThread(id=f"T{number}", author="owner", body="A" * 20_000)
        for number in range(6))})
    result = model_snapshot(source, policy())
    assert result["head_sha"] == source.head_sha
    assert result["base_sha"] == source.base_sha
    assert result["repo"] == source.repo
