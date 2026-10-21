import pytest

from patchington.config import RuntimeConfig, validate_runtime


def test_production_rejects_sqlite_and_fixture_models():
    config = RuntimeConfig(mode="production", model="fixture")
    with pytest.raises(ValueError):
        validate_runtime(config, "worker", {"PATCHINGTON_DATABASE_URL": "sqlite:///state.db"})


def test_worker_rejects_write_credentials():
    config = RuntimeConfig()
    with pytest.raises(ValueError, match="write"):
        validate_runtime(config, "worker", {"PATCHINGTON_WRITE_APP_KEY": "secret"})


def test_production_rejects_mutable_execution_image():
    config = RuntimeConfig(mode="production", model="claude-cli", execution_image="python:latest")
    with pytest.raises(ValueError, match="digest"):
        validate_runtime(
            config, "worker", {"PATCHINGTON_DATABASE_URL": "postgresql+psycopg://localhost/x"}
        )
