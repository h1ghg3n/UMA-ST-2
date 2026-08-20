import re
from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from umacircle_bot.db.session import SessionLocal, configure_session

T = TypeVar("T")
_READ_ONLY_SQL = re.compile(r"^\s*(?:SELECT|SHOW|DESCRIBE|EXPLAIN)\b", re.IGNORECASE)


class ApplicationQueryMutationError(RuntimeError):
    """Raised when a read-only application query leaves pending ORM mutations."""


def run_application_command(operation: Callable[[Session], T]) -> T:
    """Execute one state-changing operation and own its transaction boundary."""

    return _run_application_operation(operation, commit=True)


def run_application_query(operation: Callable[[Session], T]) -> T:
    """Execute one read-only operation without committing its transaction."""

    return _run_application_operation(operation, commit=False)


def run_application_transaction(operation: Callable[[Session], T]) -> T:
    """Backward-compatible alias for the application command runner."""

    return run_application_command(operation)


def _run_application_operation(
    operation: Callable[[Session], T],
    *,
    commit: bool,
) -> T:
    """Run one operation with the only application-owned Session lifecycle."""

    configure_session()
    session = SessionLocal()
    query_connection: Connection | None = None
    try:
        if not commit and isinstance(session, Session):
            query_connection = session.connection()
            event.listen(query_connection, "before_cursor_execute", _reject_query_write_statement)
        result = operation(session)
        if commit:
            session.commit()
        else:
            if session.new or session.dirty or session.deleted:
                raise ApplicationQueryMutationError("application query attempted to mutate ORM state")
            # A SELECT starts a database transaction as well. End it explicitly
            # without ever turning a query into a commit boundary.
            session.rollback()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        if query_connection is not None:
            event.remove(query_connection, "before_cursor_execute", _reject_query_write_statement)
        session.close()


def _reject_query_write_statement(
    _connection: Connection,
    _cursor: object,
    statement: str,
    _parameters: object,
    context: Any,
    _executemany: bool,
) -> None:
    compiled = getattr(context, "compiled", None)
    clause = getattr(compiled, "statement", None)
    if clause is not None and bool(getattr(clause, "is_select", False)):
        return
    if _READ_ONLY_SQL.match(statement):
        return
    raise ApplicationQueryMutationError("application query attempted to execute a write statement")
