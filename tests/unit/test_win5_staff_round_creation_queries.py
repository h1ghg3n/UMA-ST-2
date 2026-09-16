"""Staff WIN5 Round-creation query application and SQLAlchemy tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType

import pytest
from sqlalchemy import create_engine

from uma_st2.application.execution import QueryRunner
from uma_st2.application.win5 import (
    Win5RoundCreationSeasonChoice,
    Win5StaffRoundCreationInvalidSourceError,
    Win5StaffRoundCreationQueries,
)
from uma_st2.compose import compose_win5_staff_round_creation_queries
from uma_st2.domain.win5 import Win5SeasonStatus
from uma_st2.infrastructure.database import Base, DatabaseRuntime
from uma_st2.infrastructure.database.orm import Win5SeasonORM

NOW = datetime(2026, 8, 26, 9, 0)


def _choice(
    season_id: int,
    *,
    status: Win5SeasonStatus = Win5SeasonStatus.ACTIVE,
) -> Win5RoundCreationSeasonChoice:
    return Win5RoundCreationSeasonChoice(
        id=season_id,
        name=f"Season {season_id}",
        status=status,
    )


class RecordingRepository:
    def __init__(self, choices: tuple[Win5RoundCreationSeasonChoice, ...] = ()) -> None:
        self.choices = choices
        self.calls: list[tuple[int, int]] = []

    def list_round_creation_seasons(
        self,
        *,
        offset: int,
        limit: int,
    ) -> tuple[Win5RoundCreationSeasonChoice, ...]:
        self.calls.append((offset, limit))
        return self.choices


@dataclass
class RecordingUnitOfWork:
    win5_staff_round_creation_queries: RecordingRepository
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


def test_creation_season_selector_pages_at_discord_limit_in_read_only_uow() -> None:
    repository = RecordingRepository(tuple(_choice(season_id) for season_id in range(1, 27)))
    factory = RecordingFactory(repository)
    queries = Win5StaffRoundCreationQueries(QueryRunner(factory))

    page = queries.list_round_creation_seasons(offset=0, limit=25)

    assert tuple(choice.id for choice in page.choices) == tuple(range(1, 26))
    assert page.has_previous is False
    assert page.has_next is True
    assert repository.calls == [(0, 26)]
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_creation_season_selector_rejects_repository_status_drift() -> None:
    repository = RecordingRepository((_choice(7, status=Win5SeasonStatus.CLOSED),))
    queries = Win5StaffRoundCreationQueries(QueryRunner(RecordingFactory(repository)))

    with pytest.raises(Win5StaffRoundCreationInvalidSourceError, match="ineligible"):
        queries.list_round_creation_seasons()


@pytest.mark.parametrize(
    ("offset", "limit"),
    [(-1, 25), (0, 0), (0, 26), (0, True)],
)
def test_creation_season_selector_validates_page_bounds(offset: int, limit: int) -> None:
    queries = Win5StaffRoundCreationQueries(QueryRunner(RecordingFactory(RecordingRepository())))

    with pytest.raises(ValueError):
        queries.list_round_creation_seasons(offset=offset, limit=limit)


def _season_row(
    season_id: int,
    *,
    status: str,
    created_offset: int,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> dict[str, object]:
    created_at = NOW + timedelta(minutes=created_offset)
    return {
        "id": season_id,
        "name": f"Season {season_id}",
        "status": status,
        "active_marker": True if status == "active" else None,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "created_at": created_at,
        "updated_at": created_at,
    }


def test_composed_creation_query_filters_terminal_seasons_and_prioritizes_active() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            connection.execute(
                Win5SeasonORM.__table__.insert(),
                [
                    _season_row(1, status="draft", created_offset=1),
                    _season_row(
                        2,
                        status="active",
                        created_offset=0,
                        starts_at=NOW,
                        ends_at=NOW + timedelta(days=30),
                    ),
                    _season_row(3, status="closed", created_offset=3),
                    _season_row(4, status="cancelled", created_offset=4),
                    _season_row(5, status="draft", created_offset=2),
                ],
            )

        page = compose_win5_staff_round_creation_queries(runtime).list_round_creation_seasons()

        assert tuple((choice.id, choice.status) for choice in page.choices) == (
            (2, Win5SeasonStatus.ACTIVE),
            (5, Win5SeasonStatus.DRAFT),
            (1, Win5SeasonStatus.DRAFT),
        )
        assert page.choices[0].starts_at == NOW.replace(tzinfo=UTC)
        assert page.choices[0].ends_at == (NOW + timedelta(days=30)).replace(tzinfo=UTC)
        assert page.has_next is False
    finally:
        runtime.dispose()
