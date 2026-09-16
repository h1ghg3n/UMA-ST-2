"""MariaDB atomicity/idempotency evidence for WIN5 Round creation commands."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine

from uma_st2.application.win5 import (
    CreateWin5Round,
    Win5RoundCreationAuditType,
    Win5RoundCreationEntryInput,
    Win5RoundCreationRaceInput,
    Win5RoundCreationUnavailableError,
)
from uma_st2.compose import compose_win5_round_creation
from uma_st2.domain.win5 import Win5RoundSourceKind, Win5RoundStatus, Win5RoundType
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    OperationORM,
    Win5OperationORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5RoundORM,
    Win5SeasonORM,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class SeededCreationSeason:
    season_id: int


def _seed_creation_season(engine: Engine, *, suffix: str) -> SeededCreationSeason:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    with engine.begin() as connection:
        season_id = connection.execute(
            Win5SeasonORM.__table__.insert().values(
                name=f"Creation Season {suffix}",
                status="active",
                active_marker=True,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
    return SeededCreationSeason(season_id=season_id)


def _creation_command(
    *,
    season_id: int,
    suffix: str,
    round_type: Win5RoundType = Win5RoundType.NORMAL,
    idempotency_key: str | None = None,
) -> CreateWin5Round:
    normal_entries = tuple(
        Win5RoundCreationEntryInput(
            gate_number=gate_number,
            name=f"Horse {gate_number} {suffix}",
        )
        for gate_number in (1, 2, 4, 7, 8)
    )
    races = (
        (
            Win5RoundCreationRaceInput(
                name=f"Normal Race {suffix}",
                scheduled_at=datetime(2026, 8, 30, 6, 30, tzinfo=UTC),
                entries=normal_entries,
            ),
        )
        if round_type == Win5RoundType.NORMAL
        else tuple(Win5RoundCreationRaceInput(name=f"Special Race {index} {suffix}") for index in range(1, 4))
    )
    return CreateWin5Round(
        season_id=season_id,
        round_type=round_type,
        round_name=f"{round_type.value.title()} Round {suffix}",
        races=races,
        idempotency_key=idempotency_key or f"round-create-{round_type.value}-{suffix}",
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id=f"creation-{suffix}",
        reason="integration creation confirmed",
    )


def _cleanup(engine: Engine, seeded: SeededCreationSeason) -> None:
    with engine.begin() as connection:
        round_ids = tuple(connection.scalars(select(Win5RoundORM.id).where(Win5RoundORM.season_id == seeded.season_id)))
        operation_ids = tuple(
            connection.scalars(
                select(Win5OperationORM.operation_id).where(Win5OperationORM.season_id == seeded.season_id)
            )
        )
        if operation_ids:
            connection.execute(delete(Win5OperationORM).where(Win5OperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        if round_ids:
            race_ids = tuple(connection.scalars(select(Win5RaceORM.id).where(Win5RaceORM.round_id.in_(round_ids))))
            if race_ids:
                connection.execute(delete(Win5RaceEntryORM).where(Win5RaceEntryORM.race_id.in_(race_ids)))
                connection.execute(delete(Win5RaceORM).where(Win5RaceORM.id.in_(race_ids)))
            connection.execute(delete(Win5RoundORM).where(Win5RoundORM.id.in_(round_ids)))
        connection.execute(delete(Win5SeasonORM).where(Win5SeasonORM.id == seeded.season_id))


def _resolve(future: Future[object]) -> object:
    return future.result(timeout=15)


def test_normal_and_special_creation_persist_order_audit_and_exact_retry(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_creation_season(migrated_engine, suffix=suffix)
    commands = compose_win5_round_creation(DatabaseRuntime.from_engine(migrated_engine))
    normal_command = _creation_command(season_id=seeded.season_id, suffix=suffix)
    special_command = _creation_command(
        season_id=seeded.season_id,
        suffix=suffix,
        round_type=Win5RoundType.SPECIAL,
    )

    try:
        normal = commands.create_round(normal_command)
        retried = commands.create_round(normal_command)
        special = commands.create_round(special_command)

        assert retried == normal
        assert tuple(race.name for race in special.snapshot.races) == tuple(race.name for race in special_command.races)
        with migrated_engine.connect() as connection:
            rounds = connection.execute(
                select(Win5RoundORM.id, Win5RoundORM.source_kind, Win5RoundORM.status)
                .where(Win5RoundORM.season_id == seeded.season_id)
                .order_by(Win5RoundORM.id)
            ).all()
            assert {round_.source_kind for round_ in rounds} == {Win5RoundSourceKind.NATIVE_V2.value}
            races = connection.execute(
                select(
                    Win5RaceORM.id,
                    Win5RaceORM.name,
                    Win5RaceORM.scheduled_at,
                )
                .where(Win5RaceORM.round_id.in_((normal.snapshot.round_id, special.snapshot.round_id)))
                .order_by(Win5RaceORM.round_id, Win5RaceORM.id)
            ).all()
            audits = connection.execute(
                select(
                    Win5OperationORM.type,
                    Win5OperationORM.before_data,
                    Win5OperationORM.after_data,
                )
                .where(Win5OperationORM.season_id == seeded.season_id)
                .order_by(Win5OperationORM.operation_id)
            ).all()
            entry_count = connection.scalar(
                select(func.count())
                .select_from(Win5RaceEntryORM)
                .where(Win5RaceEntryORM.race_id.in_(tuple(race.id for race in races)))
            )
            entries = connection.execute(
                select(
                    Win5RaceEntryORM.gate_number,
                    Win5RaceEntryORM.name,
                )
                .where(Win5RaceEntryORM.race_id == normal.snapshot.races[0].id)
                .order_by(Win5RaceEntryORM.gate_number)
            ).all()

        assert len(rounds) == 2
        assert all(round_.status == Win5RoundStatus.SETUP.value for round_ in rounds)
        assert [race.name for race in races] == [
            normal_command.races[0].name,
            *(race.name for race in special_command.races),
        ]
        assert races[0].scheduled_at == datetime(2026, 8, 30, 6, 30)
        assert all(race.scheduled_at is None for race in races[1:])
        assert entry_count == 5
        assert [(entry.gate_number, entry.name) for entry in entries] == [
            (entry.gate_number, entry.name) for entry in normal_command.races[0].entries
        ]
        assert [audit.type for audit in audits] == [
            Win5RoundCreationAuditType.NORMAL_CREATED.value,
            Win5RoundCreationAuditType.SPECIAL_CREATED.value,
        ]
        assert audits[0].before_data is None
        assert audits[0].after_data["season_id"] == seeded.season_id
        assert audits[0].after_data["schema_version"] == 2
        assert [entry["gate_number"] for entry in audits[0].after_data["races"][0]["entries"]] == [1, 2, 4, 7, 8]
        assert audits[1].after_data["races"][0]["race_name"] == special_command.races[0].name
        assert all(not race["entries"] for race in audits[1].after_data["races"])
    finally:
        _cleanup(migrated_engine, seeded)


def test_creation_rechecks_stale_season_eligibility_without_graph_or_audit(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_creation_season(migrated_engine, suffix=suffix)
    command_ = _creation_command(season_id=seeded.season_id, suffix=suffix)
    commands = compose_win5_round_creation(DatabaseRuntime.from_engine(migrated_engine))

    try:
        with migrated_engine.begin() as connection:
            connection.execute(
                update(Win5SeasonORM)
                .where(Win5SeasonORM.id == seeded.season_id)
                .values(status="closed", active_marker=None)
            )

        with pytest.raises(Win5RoundCreationUnavailableError, match="draft or active"):
            commands.create_round(command_)

        with migrated_engine.connect() as connection:
            round_count = connection.scalar(
                select(func.count()).select_from(Win5RoundORM).where(Win5RoundORM.season_id == seeded.season_id)
            )
            operation_count = connection.scalar(
                select(func.count()).select_from(Win5OperationORM).where(Win5OperationORM.season_id == seeded.season_id)
            )
        assert round_count == 0
        assert operation_count == 0
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_exact_creation_retry_converges_to_one_round_and_operation(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_creation_season(migrated_engine, suffix=suffix)
    commands = compose_win5_round_creation(DatabaseRuntime.from_engine(migrated_engine))
    command_ = _creation_command(
        season_id=seeded.season_id,
        suffix=suffix,
        idempotency_key=f"round-create-retry-{suffix}",
    )
    start = Barrier(2)

    def create_after_barrier() -> object:
        start.wait()
        return commands.create_round(command_)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(create_after_barrier) for _ in range(2)]
            results = [_resolve(future) for future in futures]

        assert results[0] == results[1]
        with migrated_engine.connect() as connection:
            round_count = connection.scalar(
                select(func.count()).select_from(Win5RoundORM).where(Win5RoundORM.season_id == seeded.season_id)
            )
            operation_count = connection.scalar(
                select(func.count())
                .select_from(OperationORM)
                .where(OperationORM.idempotency_key == command_.idempotency_key)
            )
            entry_count = connection.scalar(
                select(func.count())
                .select_from(Win5RaceEntryORM)
                .join(Win5RaceORM, Win5RaceORM.id == Win5RaceEntryORM.race_id)
                .join(Win5RoundORM, Win5RoundORM.id == Win5RaceORM.round_id)
                .where(Win5RoundORM.season_id == seeded.season_id)
            )
        assert round_count == 1
        assert operation_count == 1
        assert entry_count == 5
    finally:
        _cleanup(migrated_engine, seeded)
