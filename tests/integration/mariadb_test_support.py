"""Shared bootstrap helpers for the disposable MariaDB integration lane."""

from __future__ import annotations

import os

import pytest
from alembic.config import Config
from sqlalchemy.engine import Connection, make_url

from alembic import command


def require_test_database_url() -> str:
    """Return the explicitly configured, test-suffixed MariaDB URL."""

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for MariaDB integration tests.")

    database_name = make_url(url).database
    if database_name is None or not database_name.endswith("_test"):
        raise RuntimeError("MariaDB integration tests require a database name ending in '_test'.")
    return url


def upgrade_to_head(connection: Connection) -> None:
    """Upgrade one explicitly supplied connection to the canonical Alembic head."""

    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "head")
