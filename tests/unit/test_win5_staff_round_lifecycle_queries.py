"""Staff WIN5 Round lifecycle query application and SQLAlchemy tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import TracebackType

import pytest
from sqlalchemy import create_engine

from uma_st2.application.execution import QueryRunner
from uma_st2.application.win5 import (
    Win5RoundLifecycleAction,
    Win5RoundLifecycleTargetChoice,
    Win5RoundLifecycleTargetPageSource,
    Win5RoundLifecycleTargetSource,
    Win5StaffRoundLifecycleInvalidSourceError,
    Win5StaffRoundLifecycleQueries,
    Win5StaffRoundOpenLimitError,
)
from uma_st2.compose import compose_win5_staff_round_lifecycle_queries
from uma_st2.domain.win5 import Win5RoundStatus, Win5RoundType, Win5SeasonStatus
from uma_st2.infrastructure.database import Base, DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5SeasonORM,
)

NOW = datetime(2026, 8, 26, 9, 0)


def _choice(
    round_id: int,
    *,
    round_type: Win5RoundType = Win5RoundType.NORMAL,
    round_status: Win5RoundStatus = Win5RoundStatus.SETUP,
    race_count: int = 1,
    race_entry_count: int = 5,
    result_count: int = 0,
    open_count: int = 0,
) -> Win5RoundLifecycleTargetChoice:
    return Win5RoundLifecycleTargetChoice(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        round_id=round_id,
        round_name=f"Round {round_id}",
        round_type=round_type,
        round_status=round_status,
        race_count=race_count,
        race_entry_count=race_entry_count,
        result_count=result_count,
        season_open_round_count=open_count,
    )


def _source(
    *,
    action: Win5RoundLifecycleAction = Win5RoundLifecycleAction.OPEN,
    round_type: Win5RoundType = Win5RoundType.NORMAL,
    race_count: int = 1,
    race_entry_count: int = 5,
    result_count: int = 0,
    open_count: int = 0,
    overflow: bool = False,
) -> Win5RoundLifecycleTargetSource:
    return Win5RoundLifecycleTargetSource(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        round_id=11,
        round_name="제3회 아리마 기념",
        round_type=round_type,
        round_status=(Win5RoundStatus.SETUP if action == Win5RoundLifecycleAction.OPEN else Win5RoundStatus.OPEN),
        race_count=race_count,
        race_entry_count=race_entry_count,
        result_count=result_count,
        accepted_submission_count=3,
        season_open_round_count=open_count,
        has_active_open_overflow=overflow,
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        page_source: Win5RoundLifecycleTargetPageSource | None = None,
        target_source: Win5RoundLifecycleTargetSource | None = None,
    ) -> None:
        self.page_source = page_source or Win5RoundLifecycleTargetPageSource()
        self.target_source = target_source
        self.list_calls: list[tuple[Win5RoundLifecycleAction, int, int]] = []
        self.get_calls: list[int] = []

    def list_round_lifecycle_target_page_source(
        self,
        *,
        action: Win5RoundLifecycleAction,
        offset: int,
        limit: int,
    ) -> Win5RoundLifecycleTargetPageSource:
        self.list_calls.append((action, offset, limit))
        return self.page_source

    def get_round_lifecycle_target_source(
        self,
        *,
        round_id: int,
    ) -> Win5RoundLifecycleTargetSource | None:
        self.get_calls.append(round_id)
        return self.target_source


@dataclass
class RecordingUnitOfWork:
    win5_staff_round_lifecycle_queries: RecordingRepository
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


def test_lifecycle_selector_pages_at_discord_limit_in_one_read_only_uow() -> None:
    choices = tuple(_choice(round_id) for round_id in range(1, 27))
    repository = RecordingRepository(page_source=Win5RoundLifecycleTargetPageSource(choices=choices))
    factory = RecordingFactory(repository)
    queries = Win5StaffRoundLifecycleQueries(QueryRunner(factory))

    page = queries.list_round_lifecycle_targets(
        action=Win5RoundLifecycleAction.OPEN,
        offset=0,
        limit=25,
    )

    assert tuple(choice.round_id for choice in page.choices) == tuple(range(1, 26))
    assert page.has_previous is False
    assert page.has_next is True
    assert repository.list_calls == [(Win5RoundLifecycleAction.OPEN, 0, 26)]
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_lifecycle_preview_revalidates_readiness_and_open_limit() -> None:
    repository = RecordingRepository(target_source=_source(open_count=24))
    queries = Win5StaffRoundLifecycleQueries(QueryRunner(RecordingFactory(repository)))

    target = queries.get_round_lifecycle_target(
        round_id=11,
        expected_action=Win5RoundLifecycleAction.OPEN,
    )

    assert target.current_status == Win5RoundStatus.SETUP
    assert target.target_status == Win5RoundStatus.OPEN
    assert target.season_open_round_count == 24

    repository = RecordingRepository(target_source=_source(open_count=25))
    queries = Win5StaffRoundLifecycleQueries(QueryRunner(RecordingFactory(repository)))
    with pytest.raises(Win5StaffRoundOpenLimitError, match="25"):
        queries.get_round_lifecycle_target(
            round_id=11,
            expected_action=Win5RoundLifecycleAction.OPEN,
        )


def test_open_selector_fails_closed_if_repository_returns_unready_candidate() -> None:
    repository = RecordingRepository(
        page_source=Win5RoundLifecycleTargetPageSource(
            choices=(_choice(11, race_entry_count=4),),
        )
    )
    queries = Win5StaffRoundLifecycleQueries(QueryRunner(RecordingFactory(repository)))

    with pytest.raises(Win5StaffRoundLifecycleInvalidSourceError, match="unready"):
        queries.list_round_lifecycle_targets(action=Win5RoundLifecycleAction.OPEN)


def test_close_query_remains_available_to_repair_active_open_overflow() -> None:
    source = _source(
        action=Win5RoundLifecycleAction.CLOSE,
        open_count=26,
        overflow=True,
    )
    repository = RecordingRepository(target_source=source)
    queries = Win5StaffRoundLifecycleQueries(QueryRunner(RecordingFactory(repository)))

    target = queries.get_round_lifecycle_target(
        round_id=11,
        expected_action=Win5RoundLifecycleAction.CLOSE,
    )

    assert target.current_status == Win5RoundStatus.OPEN
    assert target.target_status == Win5RoundStatus.CLOSED


def _season_row(id_: int, *, status: str = "active") -> dict[str, object]:
    return {
        "id": id_,
        "name": f"Season {id_}",
        "status": status,
        "active_marker": True if status == "active" else None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _round_row(
    id_: int,
    *,
    season_id: int = 1,
    round_type: str = "normal",
    status: str = "setup",
) -> dict[str, object]:
    return {
        "id": id_,
        "season_id": season_id,
        "type": round_type,
        "status": status,
        "name": f"Round {id_}",
        "created_at": NOW,
        "updated_at": NOW,
    }


def test_composed_lifecycle_queries_filter_open_readiness_and_page_closed_season_repairs() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            connection.execute(
                Win5SeasonORM.__table__.insert(),
                [_season_row(1), _season_row(2, status="closed")],
            )
            connection.execute(
                Win5RoundORM.__table__.insert(),
                [
                    _round_row(10),
                    _round_row(11),
                    _round_row(12, round_type="special"),
                    _round_row(13, round_type="special"),
                    _round_row(14),
                    _round_row(20, season_id=2, round_type="special", status="open"),
                ],
            )
            connection.execute(
                Win5RaceORM.__table__.insert(),
                [
                    {
                        "id": race_id,
                        "round_id": round_id,
                        "name": f"Race {race_id}",
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                    for race_id, round_id in (
                        (100, 10),
                        (110, 11),
                        (120, 12),
                        (121, 12),
                        (122, 12),
                        (140, 14),
                        (200, 20),
                    )
                ],
            )
            connection.execute(
                Win5RaceEntryORM.__table__.insert(),
                [
                    {
                        "id": (race_id * 10) + gate_number,
                        "race_id": race_id,
                        "gate_number": gate_number,
                        "name": f"Entry {race_id}-{gate_number}",
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                    for race_id, count in ((100, 5), (110, 4), (140, 5))
                    for gate_number in range(1, count + 1)
                ],
            )
            connection.execute(
                Win5ResultORM.__table__.insert().values(
                    id=1,
                    race_id=140,
                    race_entry_id=1401,
                    gate_number=None,
                    position=1,
                    created_at=NOW,
                )
            )

        queries = compose_win5_staff_round_lifecycle_queries(runtime)
        open_page = queries.list_round_lifecycle_targets(action=Win5RoundLifecycleAction.OPEN)
        close_page = queries.list_round_lifecycle_targets(action=Win5RoundLifecycleAction.CLOSE)
        special = queries.get_round_lifecycle_target(
            round_id=12,
            expected_action=Win5RoundLifecycleAction.OPEN,
        )
        repair = queries.get_round_lifecycle_target(
            round_id=20,
            expected_action=Win5RoundLifecycleAction.CLOSE,
        )

        assert tuple(choice.round_id for choice in open_page.choices) == (10, 12)
        assert tuple(choice.round_id for choice in close_page.choices) == (20,)
        assert special.race_count == 3
        assert special.race_entry_count == 0
        assert repair.current_status == Win5RoundStatus.OPEN
    finally:
        runtime.dispose()
