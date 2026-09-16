"""Staff setup-Round deletion query application and SQLAlchemy tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest
from sqlalchemy import create_engine

from uma_st2.application.execution import QueryRunner
from uma_st2.application.win5 import (
    Win5SetupRoundDeletionEntry,
    Win5SetupRoundDeletionRace,
    Win5SetupRoundDeletionSnapshot,
    Win5SetupRoundDeletionTargetChoice,
    Win5SetupRoundDeletionTargetSource,
    Win5SetupRoundDependencyState,
    Win5StaffRoundDeletionInvalidSourceError,
    Win5StaffRoundDeletionQueries,
    Win5StaffRoundDeletionUnavailableError,
)
from uma_st2.compose import compose_win5_staff_round_deletion_queries
from uma_st2.domain.win5 import Win5RoundSourceKind, Win5RoundStatus, Win5RoundType, Win5SeasonStatus
from uma_st2.infrastructure.database import Base, DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    DiscordPublicationORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5RoundORM,
    Win5SeasonORM,
)

NOW = datetime(2026, 8, 26, 10, 0)


def _choice(
    round_id: int,
    *,
    season_status: Win5SeasonStatus = Win5SeasonStatus.ACTIVE,
    round_status: Win5RoundStatus = Win5RoundStatus.SETUP,
) -> Win5SetupRoundDeletionTargetChoice:
    return Win5SetupRoundDeletionTargetChoice(
        season_id=7,
        season_name="2026 하반기",
        season_status=season_status,
        round_id=round_id,
        round_name=f"Round {round_id}",
        round_type=Win5RoundType.SPECIAL,
        round_status=round_status,
        race_count=2,
        entry_count=0,
    )


def _snapshot() -> Win5SetupRoundDeletionSnapshot:
    return Win5SetupRoundDeletionSnapshot(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        round_id=11,
        round_name="특별전",
        round_type=Win5RoundType.SPECIAL,
        round_status=Win5RoundStatus.SETUP,
        source_kind=Win5RoundSourceKind.NATIVE_V2,
        races=(
            Win5SetupRoundDeletionRace(
                id=101,
                name="Race A",
                entries=(Win5SetupRoundDeletionEntry(id=201, gate_number=3, name="말 A"),),
            ),
        ),
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        choices: tuple[Win5SetupRoundDeletionTargetChoice, ...] = (),
        source: Win5SetupRoundDeletionTargetSource | None = None,
    ) -> None:
        self.choices = choices
        self.source = source
        self.list_calls: list[tuple[int, int]] = []
        self.get_calls: list[int] = []

    def list_setup_round_deletion_targets(
        self,
        *,
        offset: int,
        limit: int,
    ) -> tuple[Win5SetupRoundDeletionTargetChoice, ...]:
        self.list_calls.append((offset, limit))
        return self.choices

    def get_setup_round_deletion_target_source(
        self,
        *,
        round_id: int,
    ) -> Win5SetupRoundDeletionTargetSource | None:
        self.get_calls.append(round_id)
        return self.source


@dataclass
class RecordingUnitOfWork:
    win5_staff_round_deletion_queries: RecordingRepository
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


def _queries(repository: RecordingRepository) -> tuple[Win5StaffRoundDeletionQueries, RecordingFactory]:
    factory = RecordingFactory(repository)
    return Win5StaffRoundDeletionQueries(QueryRunner(factory)), factory


def test_deletion_selector_pages_at_discord_limit_in_read_only_uow() -> None:
    repository = RecordingRepository(choices=tuple(_choice(round_id) for round_id in range(1, 27)))
    queries, factory = _queries(repository)

    page = queries.list_setup_round_deletion_targets(limit=25)

    assert tuple(choice.round_id for choice in page.choices) == tuple(range(1, 26))
    assert page.has_next is True
    assert repository.list_calls == [(0, 26)]
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_deletion_selector_rejects_repository_eligibility_drift() -> None:
    repository = RecordingRepository(choices=(_choice(11, season_status=Win5SeasonStatus.CLOSED),))
    queries, _ = _queries(repository)

    with pytest.raises(Win5StaffRoundDeletionInvalidSourceError, match="ineligible"):
        queries.list_setup_round_deletion_targets()


@pytest.mark.parametrize(
    "dependencies",
    [
        Win5SetupRoundDependencyState(1, 0, 0, 0),
        Win5SetupRoundDependencyState(0, 1, 0, 0),
        Win5SetupRoundDependencyState(0, 0, 1, 0),
        Win5SetupRoundDependencyState(0, 0, 0, 1),
    ],
)
def test_selected_target_rejects_any_downstream_fact(
    dependencies: Win5SetupRoundDependencyState,
) -> None:
    repository = RecordingRepository(
        source=Win5SetupRoundDeletionTargetSource(snapshot=_snapshot(), dependencies=dependencies)
    )
    queries, _ = _queries(repository)

    with pytest.raises(Win5StaffRoundDeletionUnavailableError, match="downstream"):
        queries.get_setup_round_deletion_target(round_id=11)


def test_selected_target_returns_closed_session_complete_graph() -> None:
    snapshot = _snapshot()
    repository = RecordingRepository(
        source=Win5SetupRoundDeletionTargetSource(
            snapshot=snapshot,
            dependencies=Win5SetupRoundDependencyState(0, 0, 0, 0),
        )
    )
    queries, factory = _queries(repository)

    target = queries.get_setup_round_deletion_target(round_id=11)

    assert target == snapshot
    assert len(target.graph_fingerprint) == 64
    assert factory.created[0].rollback_count == 1


def _season_row(season_id: int, *, status: str) -> dict[str, object]:
    return {
        "id": season_id,
        "name": f"Season {season_id}",
        "status": status,
        "active_marker": True if status == "active" else None,
        "starts_at": None,
        "ends_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _round_row(
    round_id: int,
    *,
    season_id: int,
    status: str = "setup",
) -> dict[str, object]:
    return {
        "id": round_id,
        "season_id": season_id,
        "type": "special",
        "status": status,
        "name": f"Round {round_id}",
        "opens_at": None,
        "closes_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def test_composed_deletion_queries_filter_terminal_and_published_rounds_and_order_graph() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            connection.execute(
                Win5SeasonORM.__table__.insert(),
                [
                    _season_row(1, status="active"),
                    _season_row(2, status="closed"),
                ],
            )
            connection.execute(
                Win5RoundORM.__table__.insert(),
                [
                    _round_row(11, season_id=1),
                    _round_row(12, season_id=1),
                    _round_row(13, season_id=1, status="open"),
                    _round_row(14, season_id=2),
                ],
            )
            connection.execute(
                Win5RaceORM.__table__.insert(),
                [
                    {
                        "id": 102,
                        "round_id": 11,
                        "name": "Race B",
                        "scheduled_at": None,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": 101,
                        "round_id": 11,
                        "name": "Race A",
                        "scheduled_at": NOW,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                ],
            )
            connection.execute(
                Win5RaceEntryORM.__table__.insert(),
                [
                    {
                        "id": 201,
                        "race_id": 101,
                        "gate_number": 7,
                        "name": "말 7",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": 202,
                        "race_id": 101,
                        "gate_number": 2,
                        "name": "말 2",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                ],
            )
            connection.execute(
                DiscordPublicationORM.__table__.insert(),
                {
                    "id": 301,
                    "guild_id": "987",
                    "destination_kind": "win5_announcement",
                    "event_type": "win5_round_result",
                    "event_key": "round:12",
                    "source_kind": "win5_round",
                    "source_id": 12,
                    "target_channel_id": None,
                    "payload_json": {"round_id": 12},
                    "payload_fingerprint": "0" * 64,
                    "status": "pending",
                    "attempt_count": 0,
                    "discord_message_id": None,
                    "last_error_code": None,
                    "failure_stage": None,
                    "attempt_started_at": None,
                    "published_at": None,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )

        queries = compose_win5_staff_round_deletion_queries(runtime)
        page = queries.list_setup_round_deletion_targets()
        target = queries.get_setup_round_deletion_target(round_id=11)

        assert tuple(choice.round_id for choice in page.choices) == (11,)
        assert tuple(race.id for race in target.races) == (101, 102)
        assert tuple(entry.gate_number for entry in target.races[0].entries) == (2, 7)
        assert target.races[0].scheduled_at == NOW.replace(tzinfo=UTC)
    finally:
        runtime.dispose()
