"""MariaDB locking evidence for WIN5 Round open/close lifecycle commands."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.win5 import (
    TransitionWin5Round,
    Win5RoundLifecycleAction,
    Win5RoundLifecycleAuditType,
    Win5RoundOpenLimitError,
)
from uma_st2.compose import compose_win5_round_lifecycle
from uma_st2.domain.win5 import Win5RoundStatus
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
class SeededLifecycleSeason:
    season_id: int
    round_ids: tuple[int, ...]
    target_round_ids: tuple[int, ...]
    race_ids: tuple[int, ...]
    entry_ids: tuple[int, ...]
    scheduled_opens_at: datetime


def _seed_lifecycle_season(
    engine: Engine,
    *,
    suffix: str,
    existing_open_count: int,
    target_types: tuple[str, ...],
) -> SeededLifecycleSeason:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    scheduled_opens_at = now - timedelta(days=1)
    round_ids: list[int] = []
    target_round_ids: list[int] = []
    race_ids: list[int] = []
    entry_ids: list[int] = []
    with engine.begin() as connection:
        season_id = connection.execute(
            Win5SeasonORM.__table__.insert().values(
                name=f"Lifecycle Season {suffix}",
                status="active",
                active_marker=True,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        for index in range(existing_open_count):
            round_id = connection.execute(
                Win5RoundORM.__table__.insert().values(
                    season_id=season_id,
                    type="special",
                    status="open",
                    name=f"Existing open Round {index} {suffix}",
                    created_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key[0]
            round_ids.append(round_id)

        for target_index, target_type in enumerate(target_types):
            round_id = connection.execute(
                Win5RoundORM.__table__.insert().values(
                    season_id=season_id,
                    type=target_type,
                    status="setup",
                    name=f"Target {target_type} Round {target_index} {suffix}",
                    opens_at=scheduled_opens_at,
                    created_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key[0]
            round_ids.append(round_id)
            target_round_ids.append(round_id)
            race_count = 1 if target_type == "normal" else 5
            for race_index in range(race_count):
                race_id = connection.execute(
                    Win5RaceORM.__table__.insert().values(
                        round_id=round_id,
                        name=f"Target Race {target_index}-{race_index} {suffix}",
                        created_at=now,
                        updated_at=now,
                    )
                ).inserted_primary_key[0]
                race_ids.append(race_id)
                if target_type == "normal":
                    for gate_number in range(1, 6):
                        entry_ids.append(
                            connection.execute(
                                Win5RaceEntryORM.__table__.insert().values(
                                    race_id=race_id,
                                    gate_number=gate_number,
                                    name=f"Entry {gate_number} {suffix}",
                                    created_at=now,
                                    updated_at=now,
                                )
                            ).inserted_primary_key[0]
                        )
    return SeededLifecycleSeason(
        season_id=season_id,
        round_ids=tuple(round_ids),
        target_round_ids=tuple(target_round_ids),
        race_ids=tuple(race_ids),
        entry_ids=tuple(entry_ids),
        scheduled_opens_at=scheduled_opens_at,
    )


def _open_command(*, round_id: int, idempotency_key: str) -> TransitionWin5Round:
    return TransitionWin5Round(
        round_id=round_id,
        action=Win5RoundLifecycleAction.OPEN,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id=f"lifecycle-{idempotency_key}",
        reason="integration lifecycle confirmed",
    )


def _cleanup(engine: Engine, seeded: SeededLifecycleSeason) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(Win5OperationORM.operation_id).where(Win5OperationORM.season_id == seeded.season_id)
            )
        )
        if operation_ids:
            connection.execute(delete(Win5OperationORM).where(Win5OperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        if seeded.entry_ids:
            connection.execute(delete(Win5RaceEntryORM).where(Win5RaceEntryORM.id.in_(seeded.entry_ids)))
        if seeded.race_ids:
            connection.execute(delete(Win5RaceORM).where(Win5RaceORM.id.in_(seeded.race_ids)))
        connection.execute(delete(Win5RoundORM).where(Win5RoundORM.id.in_(seeded.round_ids)))
        connection.execute(delete(Win5SeasonORM).where(Win5SeasonORM.id == seeded.season_id))


def _resolve(future: Future[object]) -> object:
    return future.result(timeout=15)


def test_concurrent_distinct_opens_serialize_at_season_and_stop_at_25(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_lifecycle_season(
        migrated_engine,
        suffix=suffix,
        existing_open_count=24,
        target_types=("normal", "special"),
    )
    commands = compose_win5_round_lifecycle(DatabaseRuntime.from_engine(migrated_engine))
    commands_to_run = tuple(
        _open_command(
            round_id=round_id,
            idempotency_key=f"round-open-limit-{suffix}-{index}",
        )
        for index, round_id in enumerate(seeded.target_round_ids)
    )
    start = Barrier(2)

    def open_after_barrier(command_: TransitionWin5Round) -> object:
        start.wait()
        return commands.transition_round(command_)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(open_after_barrier, command_) for command_ in commands_to_run]
            results: list[object] = []
            errors: list[Exception] = []
            for future in futures:
                try:
                    results.append(_resolve(future))
                except Exception as error:
                    errors.append(error)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], Win5RoundOpenLimitError)

        with migrated_engine.connect() as connection:
            open_count = connection.scalar(
                select(func.count())
                .select_from(Win5RoundORM)
                .where(
                    Win5RoundORM.season_id == seeded.season_id,
                    Win5RoundORM.status == Win5RoundStatus.OPEN.value,
                )
            )
            target_rows = connection.execute(
                select(Win5RoundORM.status, Win5RoundORM.opens_at)
                .where(Win5RoundORM.id.in_(seeded.target_round_ids))
                .order_by(Win5RoundORM.id)
            ).all()
            audits = connection.execute(
                select(Win5OperationORM.type)
                .where(Win5OperationORM.season_id == seeded.season_id)
                .order_by(Win5OperationORM.operation_id)
            ).all()

        assert open_count == 25
        assert sorted(row.status for row in target_rows) == ["open", "setup"]
        assert all(row.opens_at == seeded.scheduled_opens_at for row in target_rows)
        assert [audit.type for audit in audits] == [Win5RoundLifecycleAuditType.OPENED.value]
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_exact_open_retry_converges_to_one_operation(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_lifecycle_season(
        migrated_engine,
        suffix=suffix,
        existing_open_count=0,
        target_types=("normal",),
    )
    commands = compose_win5_round_lifecycle(DatabaseRuntime.from_engine(migrated_engine))
    command_ = _open_command(
        round_id=seeded.target_round_ids[0],
        idempotency_key=f"round-open-retry-{suffix}",
    )
    start = Barrier(2)

    def open_after_barrier() -> object:
        start.wait()
        return commands.transition_round(command_)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(open_after_barrier) for _ in range(2)]
            results = [_resolve(future) for future in futures]

        assert results[0] == results[1]
        assert results[0].status == Win5RoundStatus.OPEN
        with migrated_engine.connect() as connection:
            operation_count = connection.scalar(
                select(func.count())
                .select_from(OperationORM)
                .where(OperationORM.idempotency_key == command_.idempotency_key)
            )
        assert operation_count == 1
    finally:
        _cleanup(migrated_engine, seeded)
