"""Concrete SQLAlchemy runtime and Unit-of-Work lifecycle tests."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
    SqlAlchemyUnitOfWork,
)


class _ProbeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session


class _TwoRepositoryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory) -> None:
        super().__init__(session_factory)
        self._first: _ProbeRepository | None = None
        self._second: _ProbeRepository | None = None

    @property
    def first(self) -> _ProbeRepository:
        return self._require_active_repository(self._first)

    @property
    def second(self) -> _ProbeRepository:
        return self._require_active_repository(self._second)

    def _activate_repositories(self) -> None:
        self._first = _ProbeRepository(self.session)
        self._second = _ProbeRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._first = None
        self._second = None


class _TwoRepositoryUnitOfWorkFactory(SqlAlchemyFeatureUnitOfWorkFactory[_TwoRepositoryUnitOfWork]):
    unit_of_work_type = _TwoRepositoryUnitOfWork


class _FailingProbeRepository:
    def __init__(self, session: Session) -> None:
        session.execute(text("INSERT INTO uow_probe VALUES (40)"))
        raise LookupError("repository construction failed")


class _FailingRepositoryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory) -> None:
        super().__init__(session_factory)
        self._first: _ProbeRepository | None = None
        self._second: _FailingProbeRepository | None = None

    @property
    def first(self) -> _ProbeRepository:
        return self._require_active_repository(self._first)

    def _activate_repositories(self) -> None:
        self._first = _ProbeRepository(self.session)
        self._second = _FailingProbeRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._first = None
        self._second = None


@pytest.fixture
def database_runtime() -> DatabaseRuntime:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    with runtime.engine.begin() as connection:
        connection.execute(text("CREATE TABLE uow_probe (value INTEGER NOT NULL)"))
    try:
        yield runtime
    finally:
        runtime.dispose()


def _stored_values(runtime: DatabaseRuntime) -> list[int]:
    with runtime.engine.connect() as connection:
        return list(connection.scalars(text("SELECT value FROM uow_probe ORDER BY value")))


def test_command_runner_commits_concrete_sqlalchemy_transaction(database_runtime: DatabaseRuntime) -> None:
    runner = CommandRunner(database_runtime.unit_of_work_factory)

    runner.run(lambda unit_of_work: unit_of_work.session.execute(text("INSERT INTO uow_probe VALUES (10)")))

    assert _stored_values(database_runtime) == [10]


def test_failed_command_rolls_back_concrete_sqlalchemy_transaction(database_runtime: DatabaseRuntime) -> None:
    runner = CommandRunner(database_runtime.unit_of_work_factory)

    def fail_after_write(unit_of_work: SqlAlchemyUnitOfWork) -> None:
        unit_of_work.session.execute(text("INSERT INTO uow_probe VALUES (20)"))
        raise ValueError("stop")

    with pytest.raises(ValueError, match="stop"):
        runner.run(fail_after_write)

    assert _stored_values(database_runtime) == []


def test_query_runner_rolls_back_incidental_write(database_runtime: DatabaseRuntime) -> None:
    runner = QueryRunner(database_runtime.unit_of_work_factory)

    runner.run(lambda unit_of_work: unit_of_work.session.execute(text("INSERT INTO uow_probe VALUES (30)")))

    assert _stored_values(database_runtime) == []


def test_unit_of_work_session_is_only_available_inside_active_context(database_runtime: DatabaseRuntime) -> None:
    unit_of_work = database_runtime.unit_of_work_factory()

    with pytest.raises(RuntimeError, match="not active"):
        _ = unit_of_work.session

    with unit_of_work:
        session = unit_of_work.session
        assert session.in_transaction()
        unit_of_work.rollback()

    assert not session.in_transaction()
    with pytest.raises(RuntimeError, match="not active"):
        _ = unit_of_work.session


def test_unit_of_work_instance_cannot_be_reentered_after_exit(database_runtime: DatabaseRuntime) -> None:
    unit_of_work = database_runtime.unit_of_work_factory()

    with unit_of_work:
        unit_of_work.rollback()

    with pytest.raises(RuntimeError, match="cannot be re-entered"):
        with unit_of_work:
            pass


def test_unit_of_work_clears_state_when_session_close_fails(
    database_runtime: DatabaseRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = database_runtime.session_factory()
    close_session = session.close

    def fail_after_close() -> None:
        close_session()
        raise RuntimeError("close failed")

    monkeypatch.setattr(session, "close", fail_after_close)
    unit_of_work = SqlAlchemyUnitOfWork(lambda: session)

    with pytest.raises(RuntimeError, match="close failed"):
        with unit_of_work:
            pass

    with pytest.raises(RuntimeError, match="not active"):
        _ = unit_of_work.session


def test_feature_unit_of_work_factory_activates_multiple_repositories_on_one_session(
    database_runtime: DatabaseRuntime,
) -> None:
    factory = _TwoRepositoryUnitOfWorkFactory(database_runtime.session_factory)
    unit_of_work = factory()

    with pytest.raises(RuntimeError, match="not active"):
        _ = unit_of_work.first

    with unit_of_work:
        assert unit_of_work.first.session is unit_of_work.session
        assert unit_of_work.second.session is unit_of_work.session

    with pytest.raises(RuntimeError, match="not active"):
        _ = unit_of_work.first
    with pytest.raises(RuntimeError, match="not active"):
        _ = unit_of_work.second


def test_feature_unit_of_work_closes_session_when_repository_construction_fails(
    database_runtime: DatabaseRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = database_runtime.session_factory()
    close_session = session.close
    close_calls: list[bool] = []

    def record_close() -> None:
        close_calls.append(True)
        close_session()

    monkeypatch.setattr(session, "close", record_close)
    unit_of_work = _FailingRepositoryUnitOfWork(lambda: session)

    with pytest.raises(LookupError, match="repository construction failed"):
        with unit_of_work:
            pass

    assert close_calls == [True]
    assert not session.in_transaction()
    assert _stored_values(database_runtime) == []
    with pytest.raises(RuntimeError, match="not active"):
        _ = unit_of_work.first
    with pytest.raises(RuntimeError, match="cannot be re-entered"):
        with unit_of_work:
            pass


def test_database_runtime_disposes_its_runtime_scoped_engine() -> None:
    runtime = DatabaseRuntime.from_url("sqlite+pysqlite:///:memory:")
    disposed: list[bool] = []
    event.listen(runtime.engine, "engine_disposed", lambda _engine: disposed.append(True))

    runtime.dispose()

    assert disposed == [True]
