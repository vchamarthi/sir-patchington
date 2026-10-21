"""Role-scoped service commands; live generation requires explicit worker enablement."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import make_url

from patchington.api import create_app
from patchington.artifacts import ArtifactStore
from patchington.config import RuntimeConfig, database_url, load_config, validate_runtime
from patchington.contracts import digest_json
from patchington.execution import DockerVerifier
from patchington.github import AppTokenProvider, GitHubClient, SplitTokenProvider
from patchington.models import ClaudeCLI, ModelError
from patchington.pipeline import Pipeline
from patchington.publisher import Publisher
from patchington.store import Store


class CLIUsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CLIUsageError("invalid command arguments")


class _Disabled:
    def generate(self, *args, **kwargs):
        raise ModelError("this role cannot generate model output")

    def verify(self, *args, **kwargs):
        raise PermissionError("this role cannot execute repository code")


def _emit(value: dict) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


def _parser():
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "worker", "publisher", "migrate", "doctor", "reconcile"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, required=True)
        if name == "serve":
            sub.add_argument("--host", default="127.0.0.1")
            sub.add_argument("--port", type=int, default=8000)
        if name in {"worker", "publisher"}:
            sub.add_argument("--once", action="store_true", help="process one queue cycle and exit")
        if name == "doctor":
            sub.add_argument(
                "--role", choices=("serve", "worker", "publisher", "migrate"), default="worker"
            )
    demo = commands.add_parser("demo", help="offline fixture loop with actual Docker and local Git")
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--image", default=None)
    return parser


def _store() -> Store:
    url = database_url()
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database not in {None, "", ":memory:"}:
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Store(url)


def _role_boundary(role: str) -> None:
    forbidden = {
        "serve": (
            "PATCHINGTON_READ_APP_KEY",
            "PATCHINGTON_WRITE_APP_KEY",
            "PATCHINGTON_CLAUDE_CREDENTIALS_FILE",
        ),
        "worker": (
            "PATCHINGTON_WRITE_APP_KEY",
            "PATCHINGTON_APPROVAL_SECRET",
            "PATCHINGTON_WEBHOOK_SECRET",
        ),
        "publisher": ("PATCHINGTON_WEBHOOK_SECRET", "PATCHINGTON_CLAUDE_CREDENTIALS_FILE"),
        "reconcile": (
            "PATCHINGTON_WRITE_APP_KEY",
            "PATCHINGTON_APPROVAL_SECRET",
            "PATCHINGTON_WEBHOOK_SECRET",
            "PATCHINGTON_CLAUDE_CREDENTIALS_FILE",
        ),
        "migrate": (
            "PATCHINGTON_READ_APP_KEY",
            "PATCHINGTON_WRITE_APP_KEY",
            "PATCHINGTON_APPROVAL_SECRET",
            "PATCHINGTON_WEBHOOK_SECRET",
            "PATCHINGTON_CLAUDE_CREDENTIALS_FILE",
        ),
    }.get(role, ())
    if any(os.environ.get(name) for name in forbidden):
        raise ValueError("credentials cross a role authority boundary")
    if role != "worker" and os.environ.get("PATCHINGTON_ALLOW_LIVE_MODEL") == "1":
        raise ValueError("live model authority belongs only to workers")
    if role != "worker" and any(
        os.environ.get(key)
        for key in (
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
        )
    ):
        raise ValueError("model credentials belong only to workers")


def _app_provider(prefix: str):
    app_id = int(os.environ.get(f"PATCHINGTON_{prefix}_APP_ID", "0"))
    key = os.environ.get(f"PATCHINGTON_{prefix}_APP_KEY", "")
    if app_id <= 0 or not key or len(key.encode()) > 65536:
        raise ValueError("required scoped GitHub App credentials are missing")
    return AppTokenProvider(app_id, key)


def _close_clients(clients):
    for client in clients:
        transport = getattr(client, "_http", None)
        if transport is not None:
            transport.close()


def _result_metadata(result: dict) -> dict:
    values = {}
    for name in (
        "state",
        "error",
        "candidate_id",
        "evidence_digest",
        "head_sha",
        "verification_passed",
    ):
        value = result.get(name)
        if name == "verification_passed":
            if type(value) is bool:
                values[name] = value
        elif isinstance(value, str):
            pattern = (
                r"[a-f0-9]{64}"
                if name in {"candidate_id", "evidence_digest"}
                else r"[a-f0-9]{40}"
                if name == "head_sha"
                else r"[A-Za-z][A-Za-z0-9_]{0,99}"
            )
            if re.fullmatch(pattern, value):
                values[name] = value
    return values


def _run_queues(
    pipeline: Pipeline, role: str, once: bool, *, reconcile=None, reconcile_interval: int = 300
) -> int:
    stop = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda signum, frame: stop.set())
    owner = role + "-" + uuid.uuid4().hex
    queues = ("publish",) if role == "publisher" else ("review", "observe")
    backoff = 0.5
    next_reconcile = 0.0
    try:
        while not stop.is_set():
            worked = failed = False
            if reconcile is not None and time.monotonic() >= next_reconcile:
                try:
                    report = reconcile()
                    _emit({"reconciliation": report})
                except Exception as exc:
                    _emit({"reconciliation": {"status": "incomplete", "error": type(exc).__name__}})
                next_reconcile = time.monotonic() + reconcile_interval
            for queue in queues:
                if stop.is_set():
                    break
                try:
                    result = pipeline.run_once(owner, queue=queue)
                except Exception as exc:
                    result = {"state": "incomplete", "error": type(exc).__name__}
                if result is not None:
                    worked = True
                    failed = failed or result.get("state") == "incomplete"
                    _emit({"queue": queue, "result": _result_metadata(result)})
            if once:
                if not worked:
                    _emit({"status": "idle", "role": role})
                return 1 if failed else 0
            backoff = min(30, backoff * 2) if not worked or failed else 0.5
            stop.wait(backoff)
        _emit({"status": "stopped", "role": role})
        return 0
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _doctor(config: RuntimeConfig, role: str) -> tuple[dict, int]:
    report = {
        "role": role,
        "enrolled_repositories": len(config.repositories),
        "credentials": {},
        "model": {"status": "not_required"},
    }
    ready = True
    try:
        validate_runtime(config, role)
        _role_boundary(role)
        report["configuration"] = {"status": "valid"}
    except Exception as exc:
        report["configuration"] = {"status": "invalid", "error": type(exc).__name__}
        ready = False
    store = None
    try:
        store = _store()
        with store.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        report["database"] = {"status": "connected", "schema": "not_checked"}
    except Exception as exc:
        report["database"] = {"status": "unavailable", "error": type(exc).__name__}
        ready = False
    finally:
        if store is not None:
            store.close()
    try:
        artifacts = ArtifactStore(Path(config.artifacts_path))
        digest = artifacts.put(b"patchington-doctor-storage-check-v1")
        if artifacts.get(digest) != b"patchington-doctor-storage-check-v1":
            raise ValueError("artifact readback mismatch")
        report["artifacts"] = {"status": "read_write_verified"}
    except Exception as exc:
        report["artifacts"] = {"status": "unavailable", "error": type(exc).__name__}
        ready = False
    for prefix in ("READ", "WRITE"):
        required = role in {"worker", "publisher"} if prefix == "READ" else role == "publisher"
        configured = bool(
            os.environ.get(f"PATCHINGTON_{prefix}_APP_KEY")
            and os.environ.get(f"PATCHINGTON_{prefix}_APP_ID", "").isdigit()
            and int(os.environ.get(f"PATCHINGTON_{prefix}_APP_ID", "0")) > 0
        )
        report["credentials"][prefix.lower() + "_app"] = (
            "configured_unqualified" if configured else "missing" if required else "not_required"
        )
        if required:
            ready = False  # Presence is not live authentication qualification.
    report["docker"] = {"status": "not_required"}
    if role == "worker":
        report["model"] = {
            "status": "disabled"
            if config.model == "disabled"
            else "fixture_only"
            if config.model == "fixture"
            else "live_unqualified",
            "explicit_live_enablement": os.environ.get("PATCHINGTON_ALLOW_LIVE_MODEL") == "1",
            "executable_available": shutil.which("claude") is not None,
            "authentication": "not_checked",
            "generation": "not_executed",
        }
        ready = False
        try:
            verifier = DockerVerifier(config.execution_image)
            daemon = verifier._docker(
                ["version", "--format", "{{json .Server.Version}}"], timeout=15
            )
            environment, violations = verifier._environment()
            available = (
                daemon.code == 0 and not daemon.timed_out and not daemon.overflow and not violations
            )
            report["docker"] = {
                "status": "available" if available else "unavailable",
                "environment": environment,
                "execution": "not_executed",
            }
            if not available:
                ready = False
        except Exception as exc:
            report["docker"] = {"status": "unavailable", "error": type(exc).__name__}
            ready = False
    report["status"] = "local_checks_passed" if ready else "unqualified"
    report["readiness"] = "not_certified"
    report["note"] = "No GitHub authentication or model generation was attempted."
    return report, 0 if ready else 1


def _reconcile(store, policies, forge) -> dict:
    ids, failures, discovered = [], [], 0
    for policy in policies:
        try:
            prs = forge.list_open_prs(policy)
        except Exception as exc:
            failures.append(
                {
                    "tenant": policy.tenant,
                    "repo": policy.repo,
                    "stage": "discovery",
                    "error": type(exc).__name__,
                }
            )
            continue
        discovered += len(prs)
        for pr in prs:
            try:
                snapshot = forge.snapshot(policy, pr)
                if (
                    snapshot.pr != pr
                    or snapshot.tenant != policy.tenant
                    or snapshot.repo != policy.repo
                    or snapshot.repository_id != policy.repository_id
                    or snapshot.installation_id != policy.installation_id
                ):
                    raise PermissionError("reconciliation snapshot escaped repository enrollment")
                identity = digest_json({"policy": policy.digest, "snapshot": snapshot.identity})
                ids.append(
                    store.enqueue(
                        policy.tenant,
                        policy.repo,
                        pr,
                        "reconcile-" + identity,
                        {"kind": "review", "source": "reconcile"},
                        max_attempts=policy.max_attempts,
                    )
                )
            except Exception as exc:
                failure = {
                    "tenant": policy.tenant,
                    "repo": policy.repo,
                    "stage": "snapshot_or_enqueue",
                    "error": type(exc).__name__,
                }
                if type(pr) is int and pr > 0:
                    failure["pr"] = pr
                failures.append(failure)
    return {
        "status": "partial_failure" if failures else "reconciled",
        "discovered": discovered,
        "job_ids": ids,
        "failures": failures,
    }


def _dispatch(args, config: RuntimeConfig) -> int:
    if args.command == "doctor":
        report, code = _doctor(config, args.role)
        _emit(report)
        return code
    validate_runtime(config, args.command)
    _role_boundary(args.command)
    if args.command == "worker" and (
        config.model != "claude-cli" or os.environ.get("PATCHINGTON_ALLOW_LIVE_MODEL") != "1"
    ):
        raise ValueError("live jobs require explicitly enabled Claude CLI workers")
    store = _store()
    clients = []
    try:
        store.initialize()
        if args.command == "migrate":
            _emit({"status": "migrated", "schema": "v1"})
            return 0
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise ValueError("invalid service port")
            app = create_app(
                store,
                list(config.repositories),
                list(config.principals),
                webhook_secret=os.environ["PATCHINGTON_WEBHOOK_SECRET"].encode(),
                approval_secret=os.environ["PATCHINGTON_APPROVAL_SECRET"].encode(),
            )
            uvicorn.run(app, host=args.host, port=args.port, access_log=False, log_level="warning")
            _emit({"status": "stopped", "role": "serve"})
            return 0
        if args.command == "reconcile" and not config.repositories:
            _emit({"status": "reconciled", "discovered": 0, "job_ids": []})
            return 0
        read = _app_provider("READ")
        clients.append(read)
        tokens = read
        if args.command == "publisher":
            write = _app_provider("WRITE")
            clients.append(write)
            if read.app_id == write.app_id:
                raise ValueError("publication requires a separate write App identity")
            mapping = {
                (p.installation_id, p.repository_id): p.write_installation_id
                for p in config.repositories
                if p.write_installation_id is not None
            }
            if any(
                (p.publish_enabled or p.post_reviews)
                and (p.installation_id, p.repository_id) not in mapping
                for p in config.repositories
            ):
                raise ValueError("publication installation mapping is incomplete")
            tokens = SplitTokenProvider(read, write, mapping)
        forge = GitHubClient(tokens)
        clients.append(forge)
        if args.command == "reconcile":
            report = _reconcile(store, config.repositories, forge)
            _emit(report)
            return 1 if report["failures"] else 0
        artifacts = ArtifactStore(Path(config.artifacts_path))
        model = ClaudeCLI(allow_live=True) if args.command == "worker" else _Disabled()
        verifier = (
            DockerVerifier(config.execution_image, artifacts=artifacts)
            if args.command == "worker"
            else _Disabled()
        )
        publisher = (
            Publisher(forge, artifacts, os.environ["PATCHINGTON_APPROVAL_SECRET"].encode())
            if args.command == "publisher"
            else None
        )
        pipeline = Pipeline(
            store,
            config.repositories,
            forge,
            model,
            verifier,
            artifacts,
            publisher=publisher,
            lease_seconds=config.lease_seconds,
            tenant_concurrency=config.tenant_concurrency,
        )
        return _run_queues(
            pipeline,
            args.command,
            args.once,
            reconcile=(lambda: _reconcile(store, config.repositories, forge))
            if args.command == "worker" and not args.once
            else None,
            reconcile_interval=config.reconcile_interval_seconds,
        )
    finally:
        _close_clients(clients)
        store.close()


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "demo":
            from patchington.demo import run_demo

            kwargs = {"image": args.image} if args.image else {}
            receipt = run_demo(args.output, **kwargs)
            _emit(
                {
                    "status": receipt["status"],
                    "receipt": str(args.output.absolute() / "receipt.json"),
                }
            )
            return 0
        return _dispatch(args, load_config(args.config))
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        _emit({"status": "error", "error": type(exc).__name__})
        return 2 if isinstance(exc, CLIUsageError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
