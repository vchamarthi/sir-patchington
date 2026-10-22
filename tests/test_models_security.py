"""Mocked process-boundary tests: no Claude process/auth/provider is invoked."""

import json

import pytest

from patchington.contracts import ReviewOutput
from patchington.gitwork import ProcessResult
from patchington.models import ClaudeCLI, ModelError


def adapter(monkeypatch):
    for key in tuple(__import__("os").environ):
        if key.startswith(("ANTHROPIC_", "OPENAI_", "AWS_", "GOOGLE_")):
            monkeypatch.delenv(key)
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
    monkeypatch.setattr("patchington.models.shutil.which", lambda _path: "/mock/claude")
    monkeypatch.setattr("patchington.models.bounded_run", lambda argv, **kwargs:
        ProcessResult(0, b"2.1.306 (Claude Code)\n") if argv[-1] == "--version" else
        pytest.fail("unmocked model invocation"))
    return ClaudeCLI(allow_live=True)


def response(**updates):
    result = {"modelUsage": {"claude-sonnet-5-5": {"inputTokens": 1}},
              "is_error": False, "structured_output": {"findings": [], "limitations": []}}
    result.update(updates)
    return ProcessResult(0, json.dumps(result).encode())


def test_cli_is_disabled_without_explicit_live_enablement(monkeypatch):
    monkeypatch.setattr("patchington.models.shutil.which",
                        lambda _path: pytest.fail("disabled mode must not inspect executable"))
    with pytest.raises(ModelError, match="disabled"):
        ClaudeCLI()


@pytest.mark.parametrize("phase", ["version", "generation"])
def test_cli_disables_client_updates_for_every_subprocess(monkeypatch, phase):
    model = adapter(monkeypatch)
    monkeypatch.setenv("DISABLE_AUTOUPDATER", "0")
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        return (ProcessResult(0, b"2.1.306 (Claude Code)\n")
                if argv[-1] == "--version" else response())

    monkeypatch.setattr("patchington.models.bounded_run", run)
    if phase == "version":
        ClaudeCLI(allow_live=True)
    else:
        model.generate("review", {}, ReviewOutput)
    assert len(calls) == 1
    assert calls[0]["env"].get("DISABLE_AUTOUPDATER") == "1"


def test_generation_preserves_account_identity_without_provider_or_service_secrets(monkeypatch):
    model = adapter(monkeypatch)
    identity = {"PATH": "/synthetic/bin", "HOME": "/synthetic/home", "LANG": "C.UTF-8",
                "TMPDIR": "/synthetic/tmp", "USER": "synthetic-user", "LOGNAME": "synthetic-user"}
    for key, value in identity.items():
        monkeypatch.setenv(key, value)
    for key in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "PATCHINGTON_APPROVAL_SECRET", "LD_PRELOAD"):
        monkeypatch.setenv(key, "synthetic-secret")
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        return response()

    monkeypatch.setattr("patchington.models.bounded_run", run)
    model.generate("review", {}, ReviewOutput)
    assert len(calls) == 1
    assert calls[0]["env"] == {**identity, "DISABLE_AUTOUPDATER": "1"}


def test_cli_uses_tool_free_safe_mode_with_no_inherited_service_secrets(monkeypatch):
    model = adapter(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-github-secret")
    monkeypatch.setenv("PATCHINGTON_APPROVAL_SECRET", "synthetic-approval-secret")
    monkeypatch.setenv("LD_PRELOAD", "/synthetic/loader")
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return response()
    monkeypatch.setattr("patchington.models.bounded_run", run)
    assert model.generate("review", {"source": "value = 1"}, ReviewOutput).findings == ()
    argv, options = calls[0]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--safe-mode" in argv and "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--settings") + 1] == '{"disableAllHooks":true}'
    assert argv[argv.index("--max-turns") + 1] == "2"
    assert "--disable-slash-commands" in argv and "--no-session-persistence" in argv
    assert options["limit"] == 2_000_000 and options["timeout"] == 180
    assert not {"GITHUB_TOKEN", "PATCHINGTON_APPROVAL_SECRET", "LD_PRELOAD"} & options["env"].keys()
    assert options["cwd"] is not None


@pytest.mark.parametrize("result", [
    ProcessResult(0, b"{"), ProcessResult(1, b"sensitive process stderr"),
    ProcessResult(0, b"[]"), ProcessResult(0, b"null"),
    ProcessResult(0, b"{}", overflow=True), ProcessResult(0, b"{}", timed_out=True),
    response(modelUsage={}), response(modelUsage={"wrong-model": {}}),
    response(modelUsage=["claude-sonnet-5-5"]),
    response(is_error=True), response(structured_output={"unexpected": "not accepted"}),
])
def test_cli_rejects_failed_bounded_or_wrong_model_outputs(monkeypatch, result):
    model = adapter(monkeypatch)
    monkeypatch.setattr("patchington.models.bounded_run", lambda *args, **kwargs: result)
    with pytest.raises(ModelError) as error:
        model.generate("review", {}, ReviewOutput)
    assert "sensitive process stderr" not in str(error.value)


def test_cli_rejects_provider_routing_and_oversized_prompts_without_a_process(monkeypatch):
    model = adapter(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    with pytest.raises(ModelError, match="routing"):
        ClaudeCLI(allow_live=True)
    monkeypatch.setattr("patchington.models.bounded_run",
                        lambda *args, **kwargs: pytest.fail("oversized prompt invoked process"))
    with pytest.raises(ModelError, match="budget"):
        model.generate("review", {"source": "a" * 500_001}, ReviewOutput)


def test_startup_checks_only_the_exact_offline_client_version_with_minimal_environment(monkeypatch):
    from patchington.models import CLIENT_VERSION
    model = adapter(monkeypatch)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return ProcessResult(0, (CLIENT_VERSION + " (Claude Code)\n").encode())
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-secret")
    monkeypatch.setenv("PATCHINGTON_APPROVAL_SECRET", "synthetic-secret")
    monkeypatch.setattr("patchington.models.bounded_run", run)
    model = ClaudeCLI(allow_live=True)
    assert model.executable == "/mock/claude"
    argv, options = calls[0]
    assert argv == ["/mock/claude", "--version"]
    assert options["timeout"] == 15 and options["limit"] == 65536
    assert options.get("data", b"") == b""
    assert options["env"]["HOME"] == str(options["cwd"])
    assert not {"GITHUB_TOKEN", "PATCHINGTON_APPROVAL_SECRET"} & options["env"].keys()


@pytest.mark.parametrize("result", [ProcessResult(0, b""), ProcessResult(0, b"true"),
    ProcessResult(0, b"2.1.305 (Claude Code)\n"), ProcessResult(1, b"2.1.306 (Claude Code)"),
    ProcessResult(0, b"2.1.306 (Claude Code)", overflow=True),
    ProcessResult(0, b"2.1.306 (Claude Code)", timed_out=True)])
def test_startup_refuses_wrong_empty_or_failed_client_version(monkeypatch, result):
    adapter(monkeypatch)
    monkeypatch.setattr("patchington.models.shutil.which", lambda _path: "/usr/bin/true")
    monkeypatch.setattr("patchington.models.bounded_run", lambda *args, **kwargs: result)
    with pytest.raises(ModelError, match="version"):
        ClaudeCLI(executable="/usr/bin/true", allow_live=True)
