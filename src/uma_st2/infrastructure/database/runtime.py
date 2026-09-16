"""Runtime-scoped SQLAlchemy Engine and Session factory ownership."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .uow import SqlAlchemyUnitOfWorkFactory


@dataclass(slots=True)
class DatabaseRuntime:
    """Own database resources shared for the lifetime of one composed runtime."""

    engine: Engine
    session_factory: sessionmaker[Session]
    unit_of_work_factory: SqlAlchemyUnitOfWorkFactory

    @classmethod
    def from_url(cls, database_url: str, **engine_options: Any) -> Self:
        """Create one runtime-scoped Engine from injected deployment configuration."""

        return cls.from_engine(create_engine(database_url, **engine_options))

    @classmethod
    def from_engine(cls, engine: Engine) -> Self:
        """Bind Session and Unit-of-Work factories to an existing runtime Engine."""

        session_factory = sessionmaker(bind=engine)
        return cls(
            engine=engine,
            session_factory=session_factory,
            unit_of_work_factory=SqlAlchemyUnitOfWorkFactory(session_factory),
        )

    def dispose(self) -> None:
        """Release the runtime-scoped Engine and connection pool."""

        self.engine.dispose()
