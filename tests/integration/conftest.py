"""Common pytest fixtures for the disposable MariaDB integration lane."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from mariadb_test_support import require_test_database_url, upgrade_to_head
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


@pytest.fixture(scope="module")
def migrated_engine() -> Iterator[Engine]:
    """Provide a module-scoped Engine migrated to the current canonical head."""

    engine = create_engine(require_test_database_url())
    try:
        with engine.connect() as connection:
            upgrade_to_head(connection)
        yield engine
    finally:
        engine.dispose()
