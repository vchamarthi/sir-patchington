import json

import pytest

from patchington.contracts import ChangeSnapshot


@pytest.fixture
def environment(tmp_path, monkeypatch):
    for name in ("PATCHINGTON_READ_APP_ID", "PATCHINGTON_READ_APP_KEY", "PATCHINGTON_WRITE_APP_ID",
                 "PATCHINGTON_WRITE_APP_KEY", "PATCHINGTON_WEBHOOK_SECRET", "PATCHINGTON_APPROVAL_SECRET",
                 "PATCHINGTON_ALLOW_LIVE_MODEL", "PATCHINGTON_CLAUDE_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATCHINGTON_DATABASE_URL", "sqlite:///" + str(tmp_path / "state.db"))
    config = tmp_path / "config.toml"
    config.write_text('mode="development"\nmodel="disabled"\nartifacts_path=' +
                      json.dumps(str(tmp_path / "artifacts")) + '\n')
    return config


def test_migrate_initializes_real_durable_state_and_errors_are_type_only(environment, capsys):
    from patchington.cli import main

    assert main(["migrate", "--config", str(environment)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "migrated"
    assert (environment.parent / "state.db").exists()
    assert main(["migrate", "--config", "/missing/private-secret-config"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error"] == "FileNotFoundError"
    assert "private-secret-config" not in output


def test_serve_empty_development_enrollment_has_no_app_or_model_clients(environment, monkeypatch, capsys):
    from patchington import cli

    monkeypatch.setenv("PATCHINGTON_WEBHOOK_SECRET", "w" * 32)
    monkeypatch.setenv("PATCHINGTON_APPROVAL_SECRET", "a" * 32)
    captured = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: captured.append((app, kwargs)))
    monkeypatch.setattr(cli, "AppTokenProvider", lambda *args, **kwargs: pytest.fail("API cannot load App credentials"))
    assert cli.main(["serve", "--config", str(environment), "--host", "127.0.0.1", "--port", "8123"]) == 0
    assert captured[0][1]["port"] == 8123
    assert "/livez" in {route.path for route in captured[0][0].routes}
    assert json.loads(capsys.readouterr().out)["status"] == "stopped"


def test_disabled_worker_and_role_credential_leakage_fail_closed(environment, monkeypatch, capsys):
    from patchington.cli import main

    assert main(["worker", "--config", str(environment), "--once"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "ValueError"
    monkeypatch.setenv("PATCHINGTON_WRITE_APP_KEY", "private-must-never-be-printed")
    assert main(["worker", "--config", str(environment), "--once"]) == 1
    assert "private-must-never-be-printed" not in capsys.readouterr().out


def test_doctor_reports_unqualified_live_state_without_auth_or_model_calls(environment, monkeypatch, capsys):
    from patchington import cli

    class Docker:
        def __init__(self, *args, **kwargs):
            pass

        def _environment(self):
            return "docker:fixture-inspection-only", []

        def _docker(self, *args, **kwargs):
            from patchington.gitwork import ProcessResult
            return ProcessResult(0, b'"29.5.2"')

    monkeypatch.setattr(cli, "DockerVerifier", Docker)
    monkeypatch.setattr(cli, "AppTokenProvider", lambda *args, **kwargs: pytest.fail("doctor must not authenticate"))
    monkeypatch.setattr(cli, "ClaudeCLI", lambda *args, **kwargs: pytest.fail("doctor must not run a model"))
    assert cli.main(["doctor", "--config", str(environment)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "unqualified"
    assert report["credentials"]["read_app"] == "missing"
    assert report["model"]["status"] == "disabled"
    assert report["database"]["status"] == "connected"
    assert report["docker"]["status"] == "available"


def enroll(environment, model="claude-cli"):
    environment.write_text(environment.read_text().replace('model="disabled"', f'model="{model}"') + '''
[[repositories]]
tenant="demo"
team="dev"
installation_id=1
write_installation_id=3
repository_id=2
repo="demo/repo"
''')


def fake_providers(cli, monkeypatch):
    class Provider:
        def __init__(self, app_id, key):
            self.app_id = app_id

        def __call__(self, *args, **kwargs):
            pytest.fail("empty queue must not authenticate")

    monkeypatch.setattr(cli, "AppTokenProvider", Provider)
    monkeypatch.setenv("PATCHINGTON_READ_APP_ID", "1")
    monkeypatch.setenv("PATCHINGTON_READ_APP_KEY", "private-read-fixture")


def test_worker_once_serves_review_and_observe_without_publisher_authority(environment, monkeypatch, capsys):
    from patchington import cli

    enroll(environment)
    fake_providers(cli, monkeypatch)
    monkeypatch.setenv("PATCHINGTON_ALLOW_LIVE_MODEL", "1")
    calls, construction = [], []
    monkeypatch.setattr(cli, "ClaudeCLI", lambda **kwargs: construction.append(kwargs) or object())

    class Pipeline:
        def __init__(self, *args, **kwargs):
            assert kwargs.get("publisher") is None

        def run_once(self, owner, queue="review"):
            calls.append(queue)
            return None

    monkeypatch.setattr(cli, "Pipeline", Pipeline)
    assert cli.main(["worker", "--config", str(environment), "--once"]) == 0
    assert calls == ["review", "observe"]
    assert construction == [{"allow_live": True}]
    assert json.loads(capsys.readouterr().out)["status"] == "idle"


def test_publisher_once_has_split_credentials_and_no_model_or_execution(environment, monkeypatch, capsys):
    from patchington import cli

    enroll(environment, model="disabled")
    fake_providers(cli, monkeypatch)
    monkeypatch.setenv("PATCHINGTON_WRITE_APP_ID", "2")
    monkeypatch.setenv("PATCHINGTON_WRITE_APP_KEY", "private-write-fixture")
    monkeypatch.setenv("PATCHINGTON_APPROVAL_SECRET", "a" * 32)
    monkeypatch.setattr(cli, "ClaudeCLI", lambda **kwargs: pytest.fail("publisher cannot run a model"))
    monkeypatch.setattr(cli, "DockerVerifier", lambda *args, **kwargs: pytest.fail("publisher cannot execute source"))
    calls = []

    class Pipeline:
        def __init__(self, *args, **kwargs):
            assert kwargs["publisher"] is not None

        def run_once(self, owner, queue="review"):
            calls.append(queue)
            return None

    monkeypatch.setattr(cli, "Pipeline", Pipeline)
    assert cli.main(["publisher", "--config", str(environment), "--once"]) == 0
    assert calls == ["publish"]
    assert json.loads(capsys.readouterr().out)["status"] == "idle"


def test_reconcile_uses_authoritative_revision_and_deduplicates(environment, monkeypatch, capsys):
    from patchington import cli
    from patchington.store import Store

    enroll(environment, model="disabled")
    fake_providers(cli, monkeypatch)
    snapshot = ChangeSnapshot(tenant="demo", repository_id=2, installation_id=1, repo="demo/repo", pr=17,
                              base_sha="a" * 40, head_sha="b" * 40, head_ref="feature", head_repo="demo/repo")

    class Forge:
        def __init__(self, *args):
            pass

        def list_open_prs(self, policy):
            return [17]

        def snapshot(self, policy, pr):
            return snapshot

    monkeypatch.setattr(cli, "GitHubClient", Forge)
    for _ in range(2):
        assert cli.main(["reconcile", "--config", str(environment)]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "reconciled"
    store = Store("sqlite:///" + str(environment.parent / "state.db"))
    try:
        assert len(store.list_jobs("demo")) == 1
    finally:
        store.close()


def test_demo_dispatch_needs_no_config_and_usage_failure_is_json(monkeypatch, tmp_path, capsys):
    from patchington import cli, demo

    monkeypatch.setattr(demo, "run_demo", lambda output, **kwargs: {"status": "completed"})
    assert cli.main(["demo", "--output", str(tmp_path / "demo")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert cli.main(["worker"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "CLIUsageError"


def test_once_failure_is_nonzero_and_does_not_print_exception_text(capsys):
    from patchington.cli import _run_queues

    class Failing:
        def run_once(self, owner, queue="review"):
            if queue == "review":
                raise RuntimeError("private provider credential value")
            return None

    assert _run_queues(Failing(), "worker", once=True) == 1
    output = capsys.readouterr().out
    assert "private provider" not in output
    assert json.loads(output)["result"]["error"] == "RuntimeError"


def test_worker_signal_finishes_current_call_then_stops_and_restores_handlers(monkeypatch, capsys):
    from patchington import cli

    installed, restored, calls = {}, [], []
    def original(*args):
        return None
    monkeypatch.setattr(cli.signal, "getsignal", lambda sig: original)

    def set_handler(sig, handler):
        if handler is original:
            restored.append(sig)
        else:
            installed[sig] = handler

    monkeypatch.setattr(cli.signal, "signal", set_handler)

    class Stopping:
        def run_once(self, owner, queue="review"):
            calls.append(queue)
            installed[cli.signal.SIGTERM](cli.signal.SIGTERM, None)
            return None

    assert cli._run_queues(Stopping(), "worker", once=False) == 0
    assert calls == ["review"]
    assert set(restored) == {cli.signal.SIGINT, cli.signal.SIGTERM}
    assert json.loads(capsys.readouterr().out)["status"] == "stopped"


def test_queue_logs_only_safe_metadata_not_model_or_remote_values(capsys):
    from patchington.cli import _run_queues

    class Sensitive:
        def run_once(self, owner, queue="review"):
            return {"state": "review_complete", "findings": [{"claim": "PRIVATE SOURCE CLAIM"}],
                    "remote": {"token": "PRIVATE REMOTE VALUE"}, "verification_passed": False}

    assert _run_queues(Sensitive(), "publisher", once=True) == 0
    output = capsys.readouterr().out
    assert "PRIVATE" not in output
    assert json.loads(output)["result"] == {"state": "review_complete", "verification_passed": False}


def test_doctor_local_checks_are_not_a_ready_to_serve_claim(environment, capsys):
    from patchington.cli import main

    assert main(["doctor", "--config", str(environment), "--role", "migrate"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "local_checks_passed"
    assert report["readiness"] == "not_certified"


def test_reconcile_continues_after_one_pr_failure(environment, monkeypatch, capsys):
    from patchington import cli

    enroll(environment, model="disabled")
    fake_providers(cli, monkeypatch)
    snapshot = ChangeSnapshot(tenant="demo", repository_id=2, installation_id=1, repo="demo/repo", pr=18,
                              base_sha="a" * 40, head_sha="b" * 40, head_ref="feature", head_repo="demo/repo")

    class Forge:
        def __init__(self, *args):
            pass

        def list_open_prs(self, policy):
            return [17, 18]

        def snapshot(self, policy, pr):
            if pr == 17:
                raise RuntimeError("PRIVATE FAILURE BODY")
            return snapshot

    monkeypatch.setattr(cli, "GitHubClient", Forge)
    assert cli.main(["reconcile", "--config", str(environment)]) == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["status"] == "partial_failure"
    assert len(report["job_ids"]) == 1
    assert report["failures"][0]["pr"] == 17
    assert report["failures"][0]["error"] == "RuntimeError"
    assert "PRIVATE" not in output


@pytest.mark.parametrize("command,expected", [("migrate", 0), ("reconcile", 0),
                                               ("serve", 1), ("worker", 1), ("publisher", 1)])
def test_actual_cli_subprocess_commands_need_no_live_credentials_for_safe_paths(
        tmp_path, command, expected):
    import os
    import subprocess
    import sys

    config = tmp_path / "empty-development.toml"
    config.write_text('mode="development"\nmodel="disabled"\nartifacts_path=' +
                      json.dumps(str(tmp_path / "artifacts")) + '\n')
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "LANG": "C.UTF-8",
           "PATCHINGTON_DATABASE_URL": "sqlite:///" + str(tmp_path / "state.db")}
    args = [sys.executable, "-m", "patchington.cli", command, "--config", str(config)]
    if command in {"worker", "publisher"}:
        args.append("--once")
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == expected, result.stdout + result.stderr
    assert result.stderr == ""
    record = json.loads(result.stdout)
    assert record["status"] in {"migrated", "reconciled", "error"}
    assert "Traceback" not in result.stdout


def test_reconcile_discovery_failure_does_not_skip_other_enrolled_team(environment, monkeypatch, capsys):
    from patchington import cli

    enroll(environment, model="disabled")
    environment.write_text(environment.read_text() + '''
[[repositories]]
tenant="second"
team="other"
installation_id=4
repository_id=5
repo="demo/second"
''')
    fake_providers(cli, monkeypatch)

    class Forge:
        def __init__(self, *args):
            pass

        def list_open_prs(self, policy):
            if policy.repository_id == 2:
                raise ConnectionError("PRIVATE DISCOVERY ERROR")
            return [17]

        def snapshot(self, policy, pr):
            return ChangeSnapshot(tenant=policy.tenant, repository_id=policy.repository_id,
                installation_id=policy.installation_id, repo=policy.repo, pr=pr,
                base_sha="a" * 40, head_sha="b" * 40, head_ref="feature", head_repo=policy.repo)

    monkeypatch.setattr(cli, "GitHubClient", Forge)
    assert cli.main(["reconcile", "--config", str(environment)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "partial_failure"
    assert len(report["job_ids"]) == 1
    assert report["failures"][0]["stage"] == "discovery"
    assert report["failures"][0]["error"] == "ConnectionError"
