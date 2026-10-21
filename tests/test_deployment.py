"""Deployment authority boundaries and real image smoke checks."""

import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def read_yaml(name):
    return yaml.safe_load((ROOT / name).read_text())


def test_compose_uses_separate_roles_and_never_exposes_database_or_docker_socket():
    compose = read_yaml("compose.yaml")
    services = compose["services"]
    assert set(services) == {"postgres", "api", "worker", "publisher", "migrate"}
    assert "ports" not in services["postgres"]
    assert services["postgres"]["networks"] == ["state"]
    assert compose["networks"]["state"]["internal"] is True
    for role, command in {
        "api": "serve",
        "worker": "worker",
        "publisher": "publisher",
        "migrate": "migrate",
    }.items():
        service = services[role]
        assert service["command"][0] == command
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["user"] == "10001:10001"
        assert service["pids_limit"] <= 256
        assert service["stop_grace_period"] == "150s"
    serialized = json.dumps(compose)
    assert "/var/run/docker.sock" not in serialized
    assert "privileged" not in serialized
    assert services["api"]["ports"] == ["127.0.0.1:8000:8000"]


def test_compose_secret_mounts_follow_role_authority():
    services = read_yaml("compose.yaml")["services"]
    for role in ("api", "worker", "migrate"):
        assert "PATCHINGTON_WRITE_APP_KEY" not in services[role].get("environment", {})
        assert "write_app_key" not in json.dumps(services[role].get("secrets", []))
    assert "PATCHINGTON_APPROVAL_SECRET" not in services["worker"]["environment"]
    assert "PATCHINGTON_WEBHOOK_SECRET" not in services["publisher"]["environment"]
    assert "DOCKER_HOST" not in services["api"]["environment"]
    assert "DOCKER_HOST" not in services["publisher"]["environment"]
    assert services["worker"]["environment"]["DOCKER_TLS_VERIFY"] == "1"
    assert services["worker"]["environment"]["PATCHINGTON_ALLOW_LIVE_MODEL"].endswith(":-0}")
    assert "workers" in services["worker"]["profiles"]
    assert "publisher" in services["publisher"]["profiles"]


def test_production_configuration_and_model_install_are_pinned():
    config = tomllib.loads((ROOT / "deploy/config.production.toml").read_text())
    assert config["mode"] == "production"
    assert config["model"] == "claude-cli"
    assert re.fullmatch(r"[a-zA-Z0-9_./:-]+@sha256:[a-f0-9]{64}", config["execution_image"])
    assert "repositories" not in config or not config["repositories"]
    assert "principals" not in config or not config["principals"]
    package = json.loads((ROOT / "deploy/claude/package.json").read_text())
    lock = json.loads((ROOT / "deploy/claude/package-lock.json").read_text())
    version = package["dependencies"]["@anthropic-ai/claude-code"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    resolved = lock["packages"]["node_modules/@anthropic-ai/claude-code"]
    assert resolved["version"] == version and resolved["integrity"].startswith("sha512-")


def render(*extra):
    assert (ROOT / "deploy/render.py").exists()
    with tempfile.TemporaryDirectory() as temporary:
        config = Path(temporary) / "enrolled.toml"
        config.write_text(
            (ROOT / "deploy/config.production.toml").read_text()
            + """
[[repositories]]
tenant = "isolated-test"
team = "test"
installation_id = 1
repository_id = 1
repo = "isolated-fixture/never-contacted"
requirements = ["Private deployment fixture"]
[[repositories.checks]]
name = "fixture"
argv = ["python", "-c", "raise SystemExit(0)"]
regression = true
[[principals]]
actor = "user:fixture"
tenant = "isolated-test"
teams = ["test"]
roles = ["read"]
token_digest = "0000000000000000000000000000000000000000000000000000000000000000"
"""
        )
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "deploy/render.py"),
                "--config",
                str(config),
                "--image",
                "registry.example.test/team/patchington@sha256:" + "a" * 64,
                "--docker-host",
                "tcp://10.42.0.10:2376",
                "--docker-daemon-cidr",
                "10.42.0.10/32",
                "--postgres-cidr",
                "10.42.0.20/32",
                "--artifact-storage-class",
                "fixture-rwx",
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
        )


def test_rendered_kubernetes_pods_are_restricted_and_role_scoped():
    result = render()
    assert result.returncode == 0, result.stderr
    resources = json.loads(result.stdout)["items"]
    namespace = next(item for item in resources if item["kind"] == "Namespace")
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    deployments = {
        item["metadata"]["name"]: item for item in resources if item["kind"] == "Deployment"
    }
    assert set(deployments) == {"patchington-api", "patchington-worker", "patchington-publisher"}
    for deployment in deployments.values():
        pod = deployment["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["nodeSelector"] == {"kubernetes.io/arch": "amd64"}
        assert pod["terminationGracePeriodSeconds"] >= 150
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        for container in pod["containers"]:
            assert "@sha256:" in container["image"]
            assert container["securityContext"]["readOnlyRootFilesystem"] is True
            assert container["securityContext"]["allowPrivilegeEscalation"] is False
            assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
            assert container["resources"]["requests"] and container["resources"]["limits"]
        assert not any("hostPath" in volume for volume in pod.get("volumes", []))
    for role in ("api", "worker"):
        assert "patchington-write-app" not in json.dumps(deployments[f"patchington-{role}"])
    for role in ("api", "publisher"):
        assert "DOCKER_HOST" not in json.dumps(deployments[f"patchington-{role}"])
        assert "patchington-model" not in json.dumps(deployments[f"patchington-{role}"])
    assert any(item["kind"] == "ResourceQuota" for item in resources)
    policies = [item for item in resources if item["kind"] == "NetworkPolicy"]
    assert any(
        item["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
        for item in policies
    )
    daemon_policies = [item for item in policies if "2376" in json.dumps(item)]
    assert len(daemon_policies) == 1
    assert daemon_policies[0]["spec"]["podSelector"]["matchLabels"]["app"] == "patchington-worker"
    assert "10.42.0.10/32" in json.dumps(daemon_policies[0])
    assert not any(item["kind"] in {"Ingress", "Secret"} for item in resources)


@pytest.mark.parametrize(
    "extra",
    [
        ("--image", "registry.example.test/team/patchington:latest"),
        ("--docker-host", "tcp://10.42.0.10:2375"),
        ("--docker-daemon-cidr", "0.0.0.0/0"),
        ("--docker-daemon-cidr", "10.42.0.11/32"),
        ("--postgres-cidr", "0.0.0.0/0"),
        ("--artifact-storage-class", ""),
        ("--config", str(ROOT / "deploy/config.production.toml")),
        ("--workers", "1"),
    ],
)
def test_kubernetes_renderer_refuses_unpinned_images_and_broad_daemon_access(extra):
    result = render(*extra)
    assert result.returncode != 0
    assert result.stdout == ""


def test_ci_pins_actions_runs_postgres_and_never_uses_credentials_or_models():
    workflow = read_yaml(".github/workflows/ci.yml")
    assert workflow["permissions"] == {"contents": "read"}
    text = json.dumps(workflow)
    assert "secrets." not in text and "PATCHINGTON_ALLOW_LIVE_MODEL" not in text
    assert "pull_request_target" not in text
    uses = [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[a-f0-9]{40}", action) for action in uses)
    test_job = workflow["jobs"]["test"]
    assert "postgres" in test_job["services"]
    assert "PATCHINGTON_TEST_DATABASE_URL" in test_job["env"]
    assert any("uv sync --locked" in step.get("run", "") for step in test_job["steps"])
    assert any("pytest" in step.get("run", "") for step in test_job["steps"])


def test_mounted_secret_loading_is_explicit_and_does_not_modify_caller_environment(tmp_path):
    load = runpy.run_path(str(ROOT / "deploy/entrypoint.py"))["load_secrets"]
    secret = tmp_path / "private"
    secret.write_text("private-test-key\n")
    original = {"PATCHINGTON_READ_APP_KEY_FILE": str(secret), "PATH": "/usr/bin"}
    loaded = load(original)
    assert loaded["PATCHINGTON_READ_APP_KEY"] == "private-test-key"
    assert "PATCHINGTON_READ_APP_KEY_FILE" not in loaded
    assert "PATCHINGTON_READ_APP_KEY_FILE" in original
    with pytest.raises(ValueError, match="never both"):
        load({**original, "PATCHINGTON_READ_APP_KEY": "other-key"})
    secret.write_text("")
    with pytest.raises(ValueError, match="invalid size"):
        load(original)


def test_entrypoint_models_credentials_follow_worker_and_doctor_role():
    role = runpy.run_path(str(ROOT / "deploy/entrypoint.py"))["runtime_role"]
    assert role(["worker", "--config", "config.toml"]) == "worker"
    assert role(["serve", "--config", "config.toml"]) == "serve"
    assert role(["doctor", "--role", "worker", "--config", "config.toml"]) == "worker"
    assert role(["doctor", "--config", "config.toml"]) == "worker"
    assert role(["doctor", "--role=serve", "--config", "config.toml"]) == "serve"


@pytest.mark.docker
def test_built_service_image_runs_nonroot_without_network_or_model_auth():
    image = os.environ.get("PATCHINGTON_TEST_SERVICE_IMAGE")
    if not image:
        pytest.skip("set PATCHINGTON_TEST_SERVICE_IMAGE after building the service image")
    inspected = subprocess.run(
        ["docker", "image", "inspect", image], check=True, capture_output=True, text=True
    )
    config = json.loads(inspected.stdout)[0]["Config"]
    assert config["User"] == "10001:10001"
    smoke = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
            image,
            "--help",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert smoke.returncode == 0, smoke.stderr
    assert all(
        role in smoke.stdout for role in ("serve", "worker", "publisher", "migrate", "doctor")
    )
    tooling = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--tmpfs=/home/patchington:rw,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700",
            "--entrypoint=/bin/sh",
            image,
            "-c",
            "git --version && docker --version && claude --version",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert tooling.returncode == 0, tooling.stderr


@pytest.mark.docker
def test_built_api_starts_against_isolated_postgres_without_model_or_app_credentials():
    image = os.environ.get("PATCHINGTON_TEST_SERVICE_IMAGE")
    if not image:
        pytest.skip("set PATCHINGTON_TEST_SERVICE_IMAGE after building the service image")
    identifier = uuid4().hex[:12]
    network = f"patchington-smoke-{identifier}"
    database = f"patchington-smoke-db-{identifier}"
    api = f"patchington-smoke-api-{identifier}"
    staging = f"patchington-smoke-staging-{identifier}"
    configuration_volume = f"patchington-smoke-config-{identifier}"
    postgres_image = read_yaml("compose.yaml")["services"]["postgres"]["image"]

    def docker(*argv, check=True):
        return subprocess.run(
            ["docker", *argv], check=check, capture_output=True, text=True, timeout=45
        )

    docker("network", "create", "--internal", network)
    try:
        docker("volume", "create", configuration_volume)
        docker(
            "run",
            "--detach",
            "--network",
            network,
            "--name",
            database,
            "--env",
            "POSTGRES_PASSWORD=private-smoke-test-only",
            "--env",
            "POSTGRES_DB=patchington",
            postgres_image,
        )
        for _ in range(30):
            if (
                docker(
                    "exec", database, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", check=False
                ).returncode
                == 0
            ):
                break
            time.sleep(0.2)
        else:
            pytest.fail("isolated PostgreSQL did not become ready")
        configuration = (
            (ROOT / "deploy/config.production.toml")
            .read_text()
            .replace(
                'artifacts_path = "/var/lib/patchington/artifacts"',
                'artifacts_path = "/tmp/artifacts"',
            )
            + """
[[repositories]]
tenant = "isolated-test"
team = "test"
installation_id = 1
repository_id = 1
repo = "isolated-fixture/never-contacted"
requirements = ["No GitHub or model request may occur during API startup"]
[[repositories.checks]]
name = "fixture-only"
argv = ["python", "-c", "raise SystemExit(0)"]
regression = true
[[principals]]
actor = "user:fixture"
tenant = "isolated-test"
teams = ["test"]
roles = ["read"]
token_digest = "0000000000000000000000000000000000000000000000000000000000000000"
"""
        )
        docker(
            "create",
            "--network",
            network,
            "--name",
            api,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--mount",
            f"type=volume,src={configuration_volume},dst=/etc/patchington,readonly",
            "--env",
            f"PATCHINGTON_DATABASE_URL=postgresql+psycopg://postgres:private-smoke-test-only@{database}:5432/patchington",
            "--env",
            "PATCHINGTON_WEBHOOK_SECRET=" + "w" * 32,
            "--env",
            "PATCHINGTON_APPROVAL_SECRET=" + "a" * 32,
            image,
            "serve",
            "--config",
            "/etc/patchington/config.toml",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        )
        with tempfile.TemporaryDirectory() as temporary:
            config_file = Path(temporary) / "config.toml"
            config_file.write_text(configuration)
            config_file.chmod(0o644)
            docker(
                "create",
                "--network",
                "none",
                "--name",
                staging,
                "--mount",
                f"type=volume,src={configuration_volume},dst=/config",
                "--entrypoint",
                "/bin/true",
                image,
            )
            docker("cp", str(config_file), f"{staging}:/config/config.toml")
        docker("start", api)
        for _ in range(30):
            ready = docker(
                "exec",
                api,
                "python",
                "-c",
                "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).status)",
                check=False,
            )
            if ready.returncode == 0:
                assert ready.stdout.strip() == "200"
                break
            time.sleep(0.2)
        else:
            logs = docker("logs", api, check=False)
            pytest.fail("API failed its real readiness check: " + logs.stderr[-2000:])
    finally:
        docker("rm", "--force", "--volumes", api, database, staging, check=False)
        docker("volume", "rm", configuration_volume, check=False)
        docker("network", "rm", network, check=False)
