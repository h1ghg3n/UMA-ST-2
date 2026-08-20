from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from umacircle_bot.db.session import make_engine
from umacircle_bot.domain.errors import LegacyImportError

_ISOLATION_SQL = {
    "READ-UNCOMMITTED": "READ UNCOMMITTED",
    "READ-COMMITTED": "READ COMMITTED",
    "REPEATABLE-READ": "REPEATABLE READ",
    "SERIALIZABLE": "SERIALIZABLE",
}


@contextmanager
def open_mariadb_consistent_read_session(*, engine: Engine | None = None) -> Iterator[Session]:
    """Open one physically read-only MariaDB repeatable-read snapshot."""

    owns_engine = engine is None
    database_engine = make_engine() if engine is None else engine
    try:
        if database_engine.dialect.name not in {"mysql", "mariadb"}:
            raise LegacyImportError("consistent read requires MariaDB")
        with database_engine.connect() as connection:
            original_isolation = (
                str(connection.exec_driver_sql("SELECT @@session.tx_isolation").scalar_one()).upper().replace("_", "-")
            )
            original_isolation_sql = _ISOLATION_SQL.get(original_isolation)
            if original_isolation_sql is None:
                raise LegacyImportError("database reported an unsupported transaction isolation")
            connection.commit()
            restore_required = False
            try:
                restore_required = True
                connection.exec_driver_sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
                connection.commit()
                with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                    require_mariadb_consistent_read(session)
                    try:
                        yield session
                    finally:
                        session.rollback()
            finally:
                if restore_required:
                    _restore_mariadb_session(
                        connection,
                        original_isolation_sql=original_isolation_sql,
                    )
    finally:
        if owns_engine:
            database_engine.dispose()


def require_mariadb_consistent_read(session: Session) -> None:
    dialect_name = session.get_bind().dialect.name
    if dialect_name not in {"mysql", "mariadb"}:
        raise LegacyImportError("consistent read requires MariaDB")
    read_only = int(session.scalar(text("SELECT @@tx_read_only")) or 0)
    isolation = str(session.scalar(text("SELECT @@tx_isolation")) or "").upper().replace("_", "-")
    if read_only != 1 or isolation != "REPEATABLE-READ":
        raise LegacyImportError("consistent read requires physical read-only repeatable-read")


def _restore_mariadb_session(connection, *, original_isolation_sql: str) -> None:
    try:
        connection.rollback()
        connection.exec_driver_sql(f"SET SESSION TRANSACTION ISOLATION LEVEL {original_isolation_sql}")
        connection.exec_driver_sql("SET SESSION TRANSACTION READ WRITE")
        connection.commit()
    except BaseException:
        try:
            connection.invalidate()
        finally:
            raise
