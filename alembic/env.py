from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, Engine, engine_from_config, pool

import umacircle_bot.db.models  # noqa: F401
from umacircle_bot.config import get_settings
from umacircle_bot.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url() -> str:
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url and configured_url != "driver://user:pass@localhost/dbname":
        return configured_url
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    provided_connection = config.attributes.get("connection")
    if isinstance(provided_connection, Connection):
        _run_migrations_with_connection(provided_connection)
        return
    if isinstance(provided_connection, Engine):
        connectable = provided_connection
    else:
        configuration = config.get_section(config.config_ini_section, {})
        configuration["sqlalchemy.url"] = get_database_url()
        connectable = engine_from_config(
            configuration,
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    with connectable.connect() as connection:
        _run_migrations_with_connection(connection)


def _run_migrations_with_connection(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
