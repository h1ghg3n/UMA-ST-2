"""MariaDB transaction evidence for explicit Special WIN5 Race voids."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine

from uma_st2.application.win5 import (
    CancelSpecialWin5Round,
    SetSpecialWin5RaceVoid,
    Win5SpecialVoidAuditType,
)
from uma_st2.compose import (
    compose_win5_special_voids,
    compose_win5_staff_special_void_queries,
)
from uma_st2.domain.win5 import Win5RoundStatus, fingerprint_special_void_state
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    OperationORM,
    Win5OperationORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5ScoreEventORM,
    Win5SeasonORM,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class SeededSpecialRound:
    season_id: int
    round_id: int
    race_ids: tuple[int, ...]
    result_ids: tuple[int, ...]


def _seed_special_round(engine: Engine, *, suffix: str) -> SeededSpecialRound:
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as connection:
        season_id = connection.execute(
            Win5SeasonORM.__table__.insert().values(
                name=f"Special void Season {suffix}",
                status="active",
                active_marker=True,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        round_id = connection.execute(
            Win5RoundORM.__table__.insert().values(
                season_id=season_id,
                type="special",
                status="closed",
                name=f"Special void Round {suffix}",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        race_ids = tuple(
            connection.execute(
                Win5RaceORM.__table__.insert().values(
                    round_id=round_id,
                    name=f"Special Race {index} {suffix}",
                    created_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key[0]
            for index in range(1, 4)
        )
        result_ids = tuple(
            connection.execute(
                Win5ResultORM.__table__.insert().values(
                    race_id=race_id,
                    race_entry_id=None,
                    gate_number=gate_number,
                    position=1,
                    created_at=now,
                )
            ).inserted_primary_key[0]
            for race_id, gate_number in zip(race_ids, (3, 7, 9), strict=True)
        )
    return SeededSpecialRound(
        season_id=season_id,
        round_id=round_id,
        race_ids=race_ids,
        result_ids=result_ids,
    )


def _void_command(
    seeded: SeededSpecialRound,
    *,
    race_id: int,
    expected_void_ids: tuple[int, ...],
    suffix: str,
) -> SetSpecialWin5RaceVoid:
    return SetSpecialWin5RaceVoid(
        round_id=seeded.round_id,
        race_id=race_id,
        voided=True,
        expected_void_fingerprint=fingerprint_special_void_state(expected_void_ids),
        idempotency_key=f"special-void-{suffix}-{race_id}",
        actor_discord_user_id="123456789",
        reason=f"official no-contest {race_id}",
        guild_id="987654321",
        correlation_id=f"special-void-{suffix}",
    )


def _round_cancellation_command(
    seeded: SeededSpecialRound,
    *,
    suffix: str,
    expected_void_ids: tuple[int, ...] = (),
) -> CancelSpecialWin5Round:
    return CancelSpecialWin5Round(
        round_id=seeded.round_id,
        expected_void_fingerprint=fingerprint_special_void_state(expected_void_ids),
        idempotency_key=f"special-round-cancel-{suffix}",
        actor_discord_user_id="123456789",
        reason="official whole-round cancellation",
        guild_id="987654321",
        correlation_id=f"special-round-cancel-{suffix}",
    )


def _cleanup(engine: Engine, seeded: SeededSpecialRound) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(Win5OperationORM.operation_id).where(Win5OperationORM.round_id == seeded.round_id)
            )
        )
        if operation_ids:
            connection.execute(delete(Win5OperationORM).where(Win5OperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(Win5ResultORM).where(Win5ResultORM.race_id.in_(seeded.race_ids)))
        connection.execute(delete(Win5RaceORM).where(Win5RaceORM.id.in_(seeded.race_ids)))
        connection.execute(delete(Win5RoundORM).where(Win5RoundORM.id == seeded.round_id))
        connection.execute(delete(Win5SeasonORM).where(Win5SeasonORM.id == seeded.season_id))


def test_concurrent_exact_void_resets_results_and_persists_one_audit(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_special_round(migrated_engine, suffix=suffix)
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    commands = compose_win5_special_voids(runtime)
    queries = compose_win5_staff_special_void_queries(runtime)
    command_ = _void_command(
        seeded,
        race_id=seeded.race_ids[0],
        expected_void_ids=(),
        suffix=suffix,
    )
    start = Barrier(2)

    def void_after_barrier() -> object:
        start.wait()
        return commands.set_race_void(command_)

    try:
        before = queries.get_target(round_id=seeded.round_id)
        assert before.result_count == 3
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(void_after_barrier) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]

        assert results[0] == results[1]
        assert results[0].operation_type == Win5SpecialVoidAuditType.VOIDED
        after = queries.get_target(round_id=seeded.round_id)
        assert after.result_count == 0
        assert after.void_fingerprint == fingerprint_special_void_state((seeded.race_ids[0],))

        with migrated_engine.connect() as connection:
            result_count = connection.scalar(
                select(func.count()).select_from(Win5ResultORM).where(Win5ResultORM.race_id.in_(seeded.race_ids))
            )
            race = connection.execute(
                select(Win5RaceORM.void_reason, Win5RaceORM.voided_at).where(Win5RaceORM.id == seeded.race_ids[0])
            ).one()
            operations = connection.scalar(
                select(func.count())
                .select_from(OperationORM)
                .where(OperationORM.idempotency_key == command_.idempotency_key)
            )
            audit = connection.execute(
                select(Win5OperationORM.type, Win5OperationORM.before_data, Win5OperationORM.after_data)
                .join(OperationORM, OperationORM.id == Win5OperationORM.operation_id)
                .where(OperationORM.idempotency_key == command_.idempotency_key)
            ).one()

        assert result_count == 0
        assert race.void_reason == command_.reason
        assert race.voided_at is not None
        assert operations == 1
        assert audit.type == Win5SpecialVoidAuditType.VOIDED.value
        assert tuple(item["result_id"] for item in audit.before_data["results"]) == seeded.result_ids
        assert audit.after_data["results"] == []
    finally:
        _cleanup(migrated_engine, seeded)


def test_last_void_terminally_cancels_without_score_events(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_special_round(migrated_engine, suffix=suffix)
    commands = compose_win5_special_voids(DatabaseRuntime.from_engine(migrated_engine))

    try:
        void_ids: tuple[int, ...] = ()
        final = None
        for race_id in seeded.race_ids:
            final = commands.set_race_void(
                _void_command(
                    seeded,
                    race_id=race_id,
                    expected_void_ids=void_ids,
                    suffix=suffix,
                )
            )
            void_ids = (*void_ids, race_id)

        assert final is not None
        assert final.operation_type == Win5SpecialVoidAuditType.ALL_VOID_CANCELLED
        assert final.state.round_status == Win5RoundStatus.CANCELLED

        with migrated_engine.connect() as connection:
            round_status = connection.scalar(select(Win5RoundORM.status).where(Win5RoundORM.id == seeded.round_id))
            score_count = connection.scalar(
                select(func.count()).select_from(Win5ScoreEventORM).where(Win5ScoreEventORM.round_id == seeded.round_id)
            )
            audit_types = tuple(
                connection.scalars(
                    select(Win5OperationORM.type)
                    .where(Win5OperationORM.round_id == seeded.round_id)
                    .order_by(Win5OperationORM.operation_id)
                )
            )

        assert round_status == Win5RoundStatus.CANCELLED.value
        assert score_count == 0
        assert audit_types == (
            Win5SpecialVoidAuditType.VOIDED.value,
            Win5SpecialVoidAuditType.VOIDED.value,
            Win5SpecialVoidAuditType.ALL_VOID_CANCELLED.value,
        )
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_batch_cancellation_voids_all_races_in_one_operation(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_special_round(migrated_engine, suffix=suffix)
    existing_void_reason = "previous official no-contest"
    existing_voided_at = datetime(2026, 8, 26, 3, 0)
    with migrated_engine.begin() as connection:
        changed = connection.execute(
            update(Win5RaceORM)
            .where(Win5RaceORM.id == seeded.race_ids[0])
            .values(
                void_reason=existing_void_reason,
                voided_at=existing_voided_at,
                updated_at=existing_voided_at,
            )
        )
        assert changed.rowcount == 1
        deleted = connection.execute(delete(Win5ResultORM).where(Win5ResultORM.id == seeded.result_ids[0]))
        assert deleted.rowcount == 1
    commands = compose_win5_special_voids(DatabaseRuntime.from_engine(migrated_engine))
    command_ = _round_cancellation_command(
        seeded,
        suffix=suffix,
        expected_void_ids=(seeded.race_ids[0],),
    )
    start = Barrier(2)

    def cancel_after_barrier() -> object:
        start.wait()
        return commands.cancel_round(command_)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(cancel_after_barrier) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]

        assert results[0] == results[1]
        assert results[0].newly_voided_race_ids == seeded.race_ids[1:]
        assert results[0].state.round_status == Win5RoundStatus.CANCELLED

        with migrated_engine.connect() as connection:
            round_status = connection.scalar(select(Win5RoundORM.status).where(Win5RoundORM.id == seeded.round_id))
            race_facts = tuple(
                connection.execute(
                    select(Win5RaceORM.id, Win5RaceORM.void_reason, Win5RaceORM.voided_at)
                    .where(Win5RaceORM.id.in_(seeded.race_ids))
                    .order_by(Win5RaceORM.id)
                )
            )
            result_count = connection.scalar(
                select(func.count()).select_from(Win5ResultORM).where(Win5ResultORM.race_id.in_(seeded.race_ids))
            )
            score_count = connection.scalar(
                select(func.count()).select_from(Win5ScoreEventORM).where(Win5ScoreEventORM.round_id == seeded.round_id)
            )
            audit_rows = tuple(
                connection.execute(
                    select(Win5OperationORM.type, Win5OperationORM.before_data, Win5OperationORM.after_data)
                    .join(OperationORM, OperationORM.id == Win5OperationORM.operation_id)
                    .where(OperationORM.idempotency_key == command_.idempotency_key)
                )
            )

        assert round_status == Win5RoundStatus.CANCELLED.value
        assert tuple(row.id for row in race_facts) == seeded.race_ids
        assert race_facts[0].void_reason == existing_void_reason
        assert race_facts[0].voided_at == existing_voided_at
        assert {row.void_reason for row in race_facts[1:]} == {command_.reason}
        assert all(row.voided_at is not None for row in race_facts)
        assert result_count == 0
        assert score_count == 0
        assert len(audit_rows) == 1
        audit = audit_rows[0]
        assert audit.type == Win5SpecialVoidAuditType.ALL_VOID_CANCELLED.value
        assert tuple(item["result_id"] for item in audit.before_data["results"]) == seeded.result_ids[1:]
        assert audit.before_data["voids"] == [
            {
                "race_id": seeded.race_ids[0],
                "reason": existing_void_reason,
                "voided_at": "2026-08-26T03:00:00+00:00",
            }
        ]
        assert audit.after_data["command_scope"] == "round"
        assert tuple(audit.after_data["newly_voided_race_ids"]) == seeded.race_ids[1:]
    finally:
        _cleanup(migrated_engine, seeded)
