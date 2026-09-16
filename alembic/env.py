"""Alembic environment for the fresh V2 canonical schema."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from alembic import context
from uma_st2.config import DatabaseSettings
from uma_st2.infrastructure.database.orm import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url:
        return configured_url
    return DatabaseSettings().database_url_value


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )


@contextmanager
def _connection() -> Iterator[Connection]:
    injected_connection = config.attributes.get("connection")
    if injected_connection is not None:
        yield injected_connection
        return

    engine: Engine = create_engine(_database_url(), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with _connection() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
