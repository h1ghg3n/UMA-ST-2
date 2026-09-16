"""Static safety contract for the repository-owned production package."""

from __future__ import annotations

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (_ROOT / relative_path).read_text(encoding="utf-8")


def test_python_313_runtime_is_consistent_across_owned_tooling() -> None:
    project = tomllib.loads(_read("pyproject.toml"))
    dockerfile = _read("Dockerfile")
    integration_dockerfile = _read("Dockerfile.integration")
    ci = _read(".github/workflows/v2-ci.yml")

    assert project["project"]["requires-python"] == ">=3.13,<3.14"
    assert project["tool"]["ruff"]["target-version"] == "py313"
    assert dockerfile.count("FROM python:3.13-slim-bookworm") == 2
    assert integration_dockerfile.startswith("FROM python:3.13-slim-bookworm\n")
    assert 'python-version: "3.13"' in ci


def test_production_compose_keeps_activation_and_secret_boundaries() -> None:
    compose = _read("docker-compose.yml")
    example_environment = _read(".env.example")
    alembic_environment = _read("alembic/env.py")

    assert compose.startswith("name: uma-st-2-production\n")
    assert "\n  mariadb:\n" in compose
    assert "\n  migrate:\n" in compose
    assert "\n  bot:\n" in compose
    assert compose.count("profiles: [runtime]") == 2
    assert "condition: service_healthy" in compose
    assert "condition: service_completed_successfully" in compose
    assert "DISCORD_TOKEN_FILE: /run/secrets/discord_token" in compose
    assert "DATABASE_PASSWORD_FILE: /run/secrets/mariadb_app_password" in compose
    assert "MARIADB_PASSWORD_FILE: /run/secrets/mariadb_app_password" in compose
    assert "MARIADB_ROOT_PASSWORD_FILE: /run/secrets/mariadb_root_password" in compose
    assert "file: ${DISCORD_TOKEN_FILE:?DISCORD_TOKEN_FILE is required}" in compose
    assert "file: ${MYSQL_PASSWORD_FILE:?MYSQL_PASSWORD_FILE is required}" in compose
    assert "file: ${MYSQL_ROOT_PASSWORD_FILE:?MYSQL_ROOT_PASSWORD_FILE is required}" in compose
    assert "env_file:" not in compose
    assert "DISCORD_TOKEN:" not in compose
    assert "MARIADB_PASSWORD:" not in compose
    assert "MARIADB_ROOT_PASSWORD:" not in compose
    assert "DATABASE_URL:" not in compose
    assert "container_name:" not in compose
    assert "ports:" not in compose
    assert "DISCORD_TOKEN_FILE=secrets/discord_token" in example_environment
    assert "MYSQL_PASSWORD_FILE=secrets/mariadb_app_password" in example_environment
    assert "MYSQL_ROOT_PASSWORD_FILE=secrets/mariadb_root_password" in example_environment
    assert "DISCORD_TOKEN=" not in example_environment
    assert "MYSQL_PASSWORD=\n" not in example_environment
    assert "MYSQL_ROOT_PASSWORD=\n" not in example_environment
    assert "DATABASE_URL=\n" not in example_environment
    assert "from uma_st2.config import DatabaseSettings" in alembic_environment
    assert "DatabaseSettings().database_url_value" in alembic_environment


def test_production_image_contains_runtime_and_migrations_only() -> None:
    dockerfile = _read("Dockerfile")
    dockerignore = _read(".dockerignore")

    assert "FROM python:3.13-slim-bookworm AS runtime" in dockerfile
    assert "python -m pip install --no-cache-dir --no-index" in dockerfile
    assert "COPY --chown=10001:10001 alembic ./alembic" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert 'CMD ["uma-st-2"]' in dockerfile
    assert '".[dev]"' not in dockerfile
    assert "COPY tests" not in dockerfile

    ignored_paths = set(dockerignore.splitlines())
    assert {".env", "secrets", "tests", "docs", ".git"} <= ignored_paths
