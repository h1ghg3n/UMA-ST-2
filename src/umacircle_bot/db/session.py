from __future__ import annotations

from threading import RLock

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from umacircle_bot.config import get_settings


class SessionLifecycleError(RuntimeError):
    """Raised when one process attempts to change its configured database runtime."""


def make_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.database_url, pool_pre_ping=True, future=True)


SessionLocal = sessionmaker[Session](autocommit=False, autoflush=False, future=True)

_runtime_lock = RLock()
_runtime_engine: Engine | None = None
_runtime_database_url: str | None = None


def configure_session() -> Engine:
    """Configure and return the process-scoped application Engine.

    Repeated calls for the same configured URL are exact retries. Rebinding a live
    process to another database is rejected because concurrent operations could
    otherwise open Sessions against different authoritative stores.
    """

    global _runtime_database_url, _runtime_engine

    database_url = get_settings().database_url
    with _runtime_lock:
        if _runtime_engine is not None:
            if _runtime_database_url != database_url:
                raise SessionLifecycleError("database runtime is already configured for another URL")
            if SessionLocal.kw.get("bind") is not _runtime_engine:
                raise SessionLifecycleError("application Session factory was rebound outside the runtime owner")
            return _runtime_engine

        engine = make_engine()
        try:
            SessionLocal.configure(bind=engine)
        except BaseException:
            engine.dispose()
            raise
        _runtime_engine = engine
        _runtime_database_url = database_url
        return engine


def dispose_session_engine() -> None:
    """Dispose the configured application Engine at its composition-root boundary."""

    global _runtime_database_url, _runtime_engine

    with _runtime_lock:
        engine = _runtime_engine
        if engine is None:
            return
        if SessionLocal.kw.get("bind") is not engine:
            raise SessionLifecycleError("application Session factory was rebound outside the runtime owner")
        SessionLocal.configure(bind=None)
        _runtime_engine = None
        _runtime_database_url = None

    engine.dispose()
