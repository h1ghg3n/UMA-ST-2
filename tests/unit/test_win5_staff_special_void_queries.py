"""Staff Special WIN5 Race-void query boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest
from sqlalchemy import create_engine

from uma_st2.application.execution import QueryRunner
from uma_st2.application.win5 import (
    Win5StaffSpecialVoidInvalidSourceError,
    Win5StaffSpecialVoidQueries,
    Win5StaffSpecialVoidRace,
    Win5StaffSpecialVoidTargetChoice,
    Win5StaffSpecialVoidTargetSource,
    Win5StaffSpecialVoidUnavailableError,
)
from uma_st2.compose import compose_win5_staff_special_void_queries
from uma_st2.domain.win5 import (
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    fingerprint_special_void_state,
)
from uma_st2.infrastructure.database import Base, DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    Win5RaceORM,
    Win5RoundORM,
    Win5SeasonORM,
)

NOW = datetime(2026, 8, 26, 10, 30)


def _races(*, void_ids: tuple[int, ...] = ()) -> tuple[Win5StaffSpecialVoidRace, ...]:
    return tuple(
        Win5StaffSpecialVoidRace(
            id=race_id,
            name=f"Race {race_id}",
            void_reason=f"void {race_id}" if race_id in void_ids else None,
            voided_at=(datetime(2026, 8, 26, 9, race_id % 60, tzinfo=UTC) if race_id in void_ids else None),
        )
        for race_id in (101, 102, 103)
    )


def _source(
    *,
    void_ids: tuple[int, ...] = (),
    result_count: int = 0,
    has_score_events: bool = False,
    round_status: Win5RoundStatus = Win5RoundStatus.CLOSED,
) -> Win5StaffSpecialVoidTargetSource:
    return Win5StaffSpecialVoidTargetSource(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        round_id=11,
        round_name="여름 Special",
        round_type=Win5RoundType.SPECIAL,
        round_status=round_status,
        result_count=result_count,
        has_score_events=has_score_events,
        races=_races(void_ids=void_ids),
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        choices: tuple[Win5StaffSpecialVoidTargetChoice, ...] = (),
        source: Win5StaffSpecialVoidTargetSource | None = None,
    ) -> None:
        self.choices = choices
        self.source = source
        self.list_calls: list[int] = []
        self.get_calls: list[int] = []

    def list_target_choices(self, *, limit: int) -> tuple[Win5StaffSpecialVoidTargetChoice, ...]:
        self.list_calls.append(limit)
        return self.choices

    def get_target_source(self, *, round_id: int) -> Win5StaffSpecialVoidTargetSource | None:
        self.get_calls.append(round_id)
        return self.source


@dataclass
class RecordingUnitOfWork:
    win5_staff_special_void_queries: RecordingRepository
    commit_count: int = 0
    rollback_count: int = 0

    def __enter__(self) -> RecordingUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class RecordingFactory:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(self.repository)
        self.created.append(unit_of_work)
        return unit_of_work


def test_query_runner_returns_complete_target_without_commit() -> None:
    choice = Win5StaffSpecialVoidTargetChoice(
        season_id=7,
        season_name="2026 하반기",
        round_id=11,
        round_name="여름 Special",
        race_count=3,
        void_count=1,
    )
    repository = RecordingRepository(
        choices=(choice,),
        source=_source(void_ids=(102,), result_count=2),
    )
    factory = RecordingFactory(repository)
    queries = Win5StaffSpecialVoidQueries(QueryRunner(factory))

    assert queries.list_targets() == (choice,)
    target = queries.get_target(round_id=11)

    assert target.choice == choice
    assert target.void_fingerprint == fingerprint_special_void_state((102,))
    assert tuple(race.id for race in target.races) == (101, 102, 103)
    assert target.result_count == 2
    assert repository.list_calls == [25]
    assert repository.get_calls == [11]
    assert all(unit_of_work.commit_count == 0 for unit_of_work in factory.created)
    assert all(unit_of_work.rollback_count == 1 for unit_of_work in factory.created)


def test_query_rejects_score_backed_all_void_and_partial_result_sources() -> None:
    score_backed = Win5StaffSpecialVoidQueries(
        QueryRunner(RecordingFactory(RecordingRepository(source=_source(has_score_events=True))))
    )
    with pytest.raises(Win5StaffSpecialVoidUnavailableError, match="immutable"):
        score_backed.get_target(round_id=11)

    all_void = Win5StaffSpecialVoidQueries(
        QueryRunner(RecordingFactory(RecordingRepository(source=_source(void_ids=(101, 102, 103)))))
    )
    with pytest.raises(Win5StaffSpecialVoidInvalidSourceError, match="all-void"):
        all_void.get_target(round_id=11)

    partial = Win5StaffSpecialVoidQueries(
        QueryRunner(RecordingFactory(RecordingRepository(source=_source(void_ids=(101,), result_count=1))))
    )
    with pytest.raises(Win5StaffSpecialVoidInvalidSourceError, match="complete"):
        partial.get_target(round_id=11)


def test_composed_query_filters_and_builds_current_void_fingerprint() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            connection.execute(
                Win5SeasonORM.__table__.insert().values(
                    id=1,
                    name="2026 하반기",
                    status="active",
                    active_marker=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            connection.execute(
                Win5RoundORM.__table__.insert().values(
                    id=10,
                    season_id=1,
                    type="special",
                    status="closed",
                    name="Special 1",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            connection.execute(
                Win5RaceORM.__table__.insert(),
                [
                    {
                        "id": race_id,
                        "round_id": 10,
                        "name": f"Race {index}",
                        "void_reason": "공식 취소" if race_id == 102 else None,
                        "voided_at": NOW if race_id == 102 else None,
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                    for index, race_id in enumerate((101, 102, 103), start=1)
                ],
            )

        queries = compose_win5_staff_special_void_queries(runtime)
        choices = queries.list_targets()
        target = queries.get_target(round_id=10)

        assert len(choices) == 1
        assert (choices[0].round_id, choices[0].race_count, choices[0].void_count) == (10, 3, 1)
        assert target.void_fingerprint == fingerprint_special_void_state((102,))
        assert target.races[1].void_reason == "공식 취소"
        assert target.races[1].voided_at == datetime(2026, 8, 26, 10, 30, tzinfo=UTC)
    finally:
        runtime.dispose()
