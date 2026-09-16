"""Concrete SQLAlchemy Unit-of-Work mechanics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from types import TracebackType
from typing import Self, TypeVar

from sqlalchemy.orm import Session, SessionTransaction

SessionFactory = Callable[[], Session]
RepositoryT = TypeVar("RepositoryT")


class SqlAlchemyUnitOfWork:
    """Own one concrete Session and transaction for one application operation."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._entered = False
        self._session: Session | None = None
        self._transaction: SessionTransaction | None = None

    @property
    def session(self) -> Session:
        """Expose the active Session to infrastructure repository implementations."""

        if self._session is None:
            raise RuntimeError("Unit of work is not active.")
        return self._session

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        if self._entered:
            raise RuntimeError("Unit of work cannot be re-entered.")
        self._entered = True

        session = self._session_factory()
        try:
            transaction = session.begin()
        except BaseException:
            session.close()
            raise

        self._session = session
        self._transaction = transaction
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        session = self._session
        transaction = self._transaction
        self._session = None
        self._transaction = None
        try:
            if transaction is not None and transaction.is_active:
                transaction.rollback()
        finally:
            if session is not None:
                session.close()
        return False

    def commit(self) -> None:
        transaction = self._active_transaction()
        transaction.commit()

    def rollback(self) -> None:
        transaction = self._active_transaction()
        transaction.rollback()

    def _active_transaction(self) -> SessionTransaction:
        if self._session is None or self._transaction is None:
            raise RuntimeError("Unit of work is not active.")
        if not self._transaction.is_active:
            raise RuntimeError("Unit of work transaction is already complete.")
        return self._transaction


class SqlAlchemyFeatureUnitOfWork(SqlAlchemyUnitOfWork, ABC):
    """Activate feature repositories inside one concrete Session lifecycle."""

    def __enter__(self) -> Self:
        super().__enter__()
        try:
            self._activate_repositories()
        except BaseException as error:
            try:
                self._deactivate_repositories()
            finally:
                super().__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            self._deactivate_repositories()
        finally:
            super().__exit__(exc_type, exc_value, traceback)
        return False

    @staticmethod
    def _require_active_repository(repository: RepositoryT | None) -> RepositoryT:
        if repository is None:
            raise RuntimeError("Unit of work is not active.")
        return repository

    @abstractmethod
    def _activate_repositories(self) -> None:
        """Construct all repositories that participate in this feature UoW."""

    @abstractmethod
    def _deactivate_repositories(self) -> None:
        """Clear repository references without performing persistence work."""


class SqlAlchemyFeatureUnitOfWorkFactory[FeatureUnitOfWorkT: SqlAlchemyFeatureUnitOfWork]:
    """Create a configured feature UoW from one runtime-scoped Session factory."""

    unit_of_work_type: type[FeatureUnitOfWorkT]

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def __call__(self) -> FeatureUnitOfWorkT:
        return self.unit_of_work_type(self._session_factory)


class SqlAlchemyUnitOfWorkFactory:
    """Create fresh SQLAlchemy units of work from one runtime-scoped Session factory."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def __call__(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self._session_factory)
