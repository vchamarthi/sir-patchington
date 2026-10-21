"""Operator-owned configuration, never configuration from a proposed change."""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field

from .api import Principal
from .contracts import Frozen, RepoPolicy


class RuntimeConfig(Frozen):
    mode: Literal["development", "production"] = "development"
    model: Literal["disabled", "fixture", "claude-cli"] = "disabled"
    execution_image: str = (
        "python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
    )
    artifacts_path: str = ".local/artifacts"
    lease_seconds: int = Field(default=120, ge=30, le=600)
    tenant_concurrency: int = Field(default=4, ge=1, le=100)
    reconcile_interval_seconds: int = Field(default=300, ge=30, le=3600)
    repositories: tuple[RepoPolicy, ...] = ()
    principals: tuple[Principal, ...] = ()


def load_config(path: str | Path) -> RuntimeConfig:
    with Path(path).open("rb") as stream:
        config = RuntimeConfig.model_validate_json(json.dumps(tomllib.load(stream)))
    ids = [(p.installation_id, p.repository_id) for p in config.repositories]
    names = [(p.tenant, p.repo) for p in config.repositories]
    if len(set(ids)) != len(ids) or len(set(names)) != len(names):
        raise ValueError("repository enrollment must be unique")
    if len({p.token_digest for p in config.principals}) != len(config.principals):
        raise ValueError("principal tokens must be unique")
    return config


def validate_runtime(config: RuntimeConfig, role: str, env: dict[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    if role in {"serve", "worker"} and env.get("PATCHINGTON_WRITE_APP_KEY"):
        raise ValueError("write App credentials must exist only in the publisher")
    if config.mode == "production":
        if not env.get("PATCHINGTON_DATABASE_URL", "").startswith("postgresql+psycopg://"):
            raise ValueError("production requires PostgreSQL using psycopg")
        if not re.fullmatch(r"[a-zA-Z0-9_./:-]+@sha256:[0-9a-f]{64}", config.execution_image):
            raise ValueError("production execution image must be digest pinned")
        if role == "worker" and config.model != "claude-cli":
            raise ValueError("production refuses fixture/disabled model execution")
        if role == "worker" and env.get("PATCHINGTON_ALLOW_LIVE_MODEL") != "1":
            raise ValueError("live model execution has not been explicitly enabled")
        if not config.repositories:
            raise ValueError("production has no enrolled repositories")
        if any(not p.checks or not p.requirements for p in config.repositories):
            raise ValueError("production requires trusted checks and requirements per repository")
        if any(
            (p.publish_enabled or p.post_reviews) and not p.write_installation_id
            for p in config.repositories
        ):
            raise ValueError("publication requires the separate write App installation identity")
    for secret in {
        "serve": ("PATCHINGTON_WEBHOOK_SECRET", "PATCHINGTON_APPROVAL_SECRET"),
        "publisher": ("PATCHINGTON_APPROVAL_SECRET",),
    }.get(role, ()):
        if len(env.get(secret, "").encode()) < 32:
            raise ValueError(f"{secret} must be configured with at least 32 bytes")


def database_url() -> str:
    return os.environ.get("PATCHINGTON_DATABASE_URL", "sqlite:///.local/state.db")
