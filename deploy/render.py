"""Render Kubernetes JSON from concrete operator inputs; never apply resources."""

import argparse
import ipaddress
import json
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

NAMESPACE = "sir-patchington"


def resource(kind, name, **fields):
    versions = {
        "Deployment": "apps/v1",
        "Job": "batch/v1",
        "NetworkPolicy": "networking.k8s.io/v1",
        "PodDisruptionBudget": "policy/v1",
    }
    return {
        "apiVersion": versions.get(kind, "v1"),
        "kind": kind,
        "metadata": {"name": name, "namespace": NAMESPACE},
        **fields,
    }


def file_secret(name, source, key):
    return {"name": f"{name}_FILE", "value": f"/run/secrets/{source}/{key}"}


def secret_volume(source):
    return {"name": source, "secret": {"secretName": source, "defaultMode": 0o440}}


def secret_mount(source):
    return {"name": source, "mountPath": f"/run/secrets/{source}", "readOnly": True}


def pod(args, role):
    name = f"patchington-{role}"
    secrets = ["patchington-state"]
    environment = [file_secret("PATCHINGTON_DATABASE_URL", "patchington-state", "database-url")]
    mounts = [
        {"name": "config", "mountPath": "/etc/patchington", "readOnly": True},
        {"name": "tmp", "mountPath": "/tmp"},  # noqa: S108 -- isolated emptyDir mount
    ]
    volumes = [
        {"name": "config", "configMap": {"name": "patchington-config"}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}},
    ]
    if role != "migrate":
        mounts.append({"name": "artifacts", "mountPath": "/var/lib/patchington/artifacts"})
        volumes.append(
            {"name": "artifacts", "persistentVolumeClaim": {"claimName": "patchington-artifacts"}}
        )
    if role == "api":
        secrets.append("patchington-intake")
        environment += [
            file_secret("PATCHINGTON_WEBHOOK_SECRET", "patchington-intake", "webhook-secret"),
            file_secret("PATCHINGTON_APPROVAL_SECRET", "patchington-intake", "approval-secret"),
        ]
    if role in {"worker", "publisher"}:
        secrets.append("patchington-read-app")
        environment += [
            {"name": "PATCHINGTON_READ_APP_ID", "value": str(args.read_app_id)},
            file_secret("PATCHINGTON_READ_APP_KEY", "patchington-read-app", "app.pem"),
        ]
    if role == "worker":
        secrets += ["patchington-model", "patchington-docker-tls"]
        environment += [
            {
                "name": "PATCHINGTON_ALLOW_LIVE_MODEL",
                "value": "1" if args.allow_live_model else "0",
            },
            {
                "name": "PATCHINGTON_CLAUDE_CREDENTIALS_FILE",
                "value": "/run/secrets/patchington-model/credentials.json",
            },
            {"name": "DOCKER_HOST", "value": args.docker_host},
            {"name": "DOCKER_TLS_VERIFY", "value": "1"},
            {"name": "DOCKER_CERT_PATH", "value": "/run/secrets/patchington-docker-tls"},
        ]
        mounts.append({"name": "home", "mountPath": "/home/patchington"})
        volumes.append({"name": "home", "emptyDir": {"sizeLimit": "1Gi"}})
    if role == "publisher":
        secrets += ["patchington-write-app", "patchington-approval"]
        environment += [
            {"name": "PATCHINGTON_WRITE_APP_ID", "value": str(args.write_app_id)},
            file_secret("PATCHINGTON_WRITE_APP_KEY", "patchington-write-app", "app.pem"),
            file_secret("PATCHINGTON_APPROVAL_SECRET", "patchington-approval", "approval-secret"),
        ]
    volumes += [secret_volume(source) for source in secrets]
    mounts += [secret_mount(source) for source in secrets]
    command = ["serve" if role == "api" else role, "--config", "/etc/patchington/config.toml"]
    if role == "api":
        command += ["--host", "0.0.0.0", "--port", "8000"]  # noqa: S104 -- ClusterIP behind NetworkPolicy
    limits = {
        "api": ("1", "512Mi"),
        "worker": ("2", "2Gi"),
        "publisher": ("1", "1Gi"),
        "migrate": ("500m", "256Mi"),
    }
    cpu, memory = limits[role]
    container = {
        "name": name,
        "image": args.image,
        "imagePullPolicy": "IfNotPresent",
        "args": command,
        "env": environment,
        "volumeMounts": mounts,
        "securityContext": {
            "readOnlyRootFilesystem": True,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "256Mi"},
            "limits": {"cpu": cpu, "memory": memory, "ephemeral-storage": "2Gi"},
        },
    }
    if role == "api":
        container.update(
            {
                "ports": [{"name": "http", "containerPort": 8000}],
                "startupProbe": {
                    "httpGet": {"path": "/livez", "port": "http"},
                    "failureThreshold": 30,
                    "periodSeconds": 2,
                },
                "readinessProbe": {
                    "httpGet": {"path": "/readyz", "port": "http"},
                    "periodSeconds": 5,
                },
                "livenessProbe": {
                    "httpGet": {"path": "/livez", "port": "http"},
                    "periodSeconds": 10,
                },
            }
        )
    return {
        "metadata": {"labels": {"app": name}},
        "spec": {
            "nodeSelector": {"kubernetes.io/arch": "amd64"},
            "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 150,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "fsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [container],
            "volumes": volumes,
        },
    }


def network(name, selector, egress=None, ingress=None):
    spec = {"podSelector": selector, "policyTypes": []}
    if egress is not None:
        spec.update({"egress": egress, "policyTypes": [*spec["policyTypes"], "Egress"]})
    if ingress is not None:
        spec.update({"ingress": ingress, "policyTypes": [*spec["policyTypes"], "Ingress"]})
    return resource("NetworkPolicy", name, spec=spec)


def render(args):
    configuration = Path(args.config).read_text()
    config = tomllib.loads(configuration)
    if config.get("mode") != "production" or config.get("model") != "claude-cli":
        raise ValueError("renderer requires production Claude CLI configuration")
    if not config.get("repositories") or not config.get("principals"):
        raise ValueError("review repository and principal enrollment before rendering a deployment")
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": NAMESPACE,
            "labels": {
                f"pod-security.kubernetes.io/{mode}": "restricted"
                for mode in ("enforce", "audit", "warn")
            },
        },
    }
    output = [
        namespace,
        resource("ConfigMap", "patchington-config", data={"config.toml": configuration}),
        resource(
            "ResourceQuota",
            "patchington-quota",
            spec={
                "hard": {
                    "pods": "16",
                    "requests.cpu": "4",
                    "requests.memory": "8Gi",
                    "limits.cpu": "16",
                    "limits.memory": "16Gi",
                    "requests.storage": "20Gi",
                }
            },
        ),
        resource(
            "LimitRange",
            "patchington-container-limits",
            spec={
                "limits": [
                    {
                        "type": "Container",
                        "default": {"cpu": "1", "memory": "512Mi"},
                        "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
                    }
                ]
            },
        ),
        resource(
            "PersistentVolumeClaim",
            "patchington-artifacts",
            spec={
                "accessModes": ["ReadWriteMany"],
                "storageClassName": args.artifact_storage_class,
                "resources": {"requests": {"storage": "10Gi"}},
            },
        ),
        resource(
            "Service",
            "patchington-api",
            spec={
                "type": "ClusterIP",
                "selector": {"app": "patchington-api"},
                "ports": [{"name": "http", "port": 8000, "targetPort": "http"}],
            },
        ),
        resource(
            "NetworkPolicy",
            "default-deny",
            spec={"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
        ),
    ]
    for role, replicas in (("api", 2), ("worker", args.workers), ("publisher", args.publishers)):
        name = f"patchington-{role}"
        output.append(
            resource(
                "Deployment",
                name,
                spec={
                    "replicas": replicas,
                    "selector": {"matchLabels": {"app": name}},
                    "strategy": {
                        "type": "RollingUpdate",
                        "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
                    },
                    "template": pod(args, role),
                },
            )
        )
    migration = pod(args, "migrate")
    migration["spec"]["restartPolicy"] = "Never"
    output.append(
        resource(
            "Job",
            "patchington-schema-v1",
            spec={"backoffLimit": 1, "activeDeadlineSeconds": 120, "template": migration},
        )
    )
    output.append(
        resource(
            "PodDisruptionBudget",
            "patchington-api",
            spec={"minAvailable": 1, "selector": {"matchLabels": {"app": "patchington-api"}}},
        )
    )
    dns = {
        "to": [
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}
        ],
        "ports": [{"protocol": protocol, "port": 53} for protocol in ("UDP", "TCP")],
    }
    database = {
        "to": [{"ipBlock": {"cidr": args.postgres_cidr}}],
        "ports": [{"protocol": "TCP", "port": 5432}],
    }
    output.append(network("service-state-and-dns", {}, egress=[dns, database]))
    output.append(
        network(
            "api-ingress",
            {"matchLabels": {"app": "patchington-api"}},
            ingress=[
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": args.ingress_namespace
                                }
                            }
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 8000}],
                }
            ],
        )
    )
    public_tls = {
        "to": [
            {
                "ipBlock": {
                    "cidr": "0.0.0.0/0",
                    "except": [
                        "10.0.0.0/8",
                        "172.16.0.0/12",
                        "192.168.0.0/16",
                        "127.0.0.0/8",
                        "169.254.0.0/16",
                    ],
                }
            }
        ],
        "ports": [{"protocol": "TCP", "port": 443}],
    }
    output.append(
        network(
            "worker-and-publisher-public-tls",
            {
                "matchExpressions": [
                    {
                        "key": "app",
                        "operator": "In",
                        "values": ["patchington-worker", "patchington-publisher"],
                    }
                ]
            },
            egress=[public_tls],
        )
    )
    output.append(
        network(
            "worker-docker-mtls",
            {"matchLabels": {"app": "patchington-worker"}},
            egress=[
                {
                    "to": [{"ipBlock": {"cidr": args.docker_daemon_cidr}}],
                    "ports": [{"protocol": "TCP", "port": 2376}],
                }
            ],
        )
    )
    return {"apiVersion": "v1", "kind": "List", "items": output}


def host_cidr(value):
    network = ipaddress.ip_network(value, strict=True)
    if network.prefixlen != network.max_prefixlen:
        raise argparse.ArgumentTypeError("use one explicit host /32 or /128, never a broad range")
    return str(network)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.production.toml")))
    parser.add_argument("--docker-host", required=True)
    parser.add_argument("--docker-daemon-cidr", type=host_cidr, required=True)
    parser.add_argument("--postgres-cidr", type=host_cidr, required=True)
    parser.add_argument("--artifact-storage-class", default="")
    parser.add_argument("--ingress-namespace", default="ingress-nginx")
    parser.add_argument("--read-app-id", type=int, default=0)
    parser.add_argument("--write-app-id", type=int, default=0)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--publishers", type=int, default=0)
    parser.add_argument("--allow-live-model", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9._:/-]+@sha256:[a-f0-9]{64}", args.image):
        parser.error("provide an actual published digest-pinned service image")
    try:
        if not args.artifact_storage_class:
            raise ValueError("select a reviewed ReadWriteMany artifact storage class")
        docker = urlsplit(args.docker_host)
        address = ipaddress.ip_address(docker.hostname or "")
        if (
            docker.scheme != "tcp"
            or docker.port != 2376
            or docker.username
            or docker.password
            or docker.path
            or docker.query
            or docker.fragment
            or address not in ipaddress.ip_network(args.docker_daemon_cidr)
        ):
            raise ValueError("daemon must be an exact allowlisted IP at tcp://IP:2376 with mTLS")
        if not 0 <= args.workers <= 8 or not 0 <= args.publishers <= 2:
            raise ValueError("worker replicas must be 0..8 and publisher replicas 0..2")
        if args.workers and (not args.allow_live_model or args.read_app_id <= 0):
            raise ValueError("workers require explicit --allow-live-model and --read-app-id")
        if args.publishers and (args.read_app_id <= 0 or args.write_app_id <= 0):
            raise ValueError("publishers require explicit read/write App IDs")
        result = render(args)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
