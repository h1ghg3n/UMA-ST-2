"""MariaDB transaction evidence for authoritative Special WIN5 results."""

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
    SaveSpecialWin5Result,
    Win5SpecialResultAuditType,
    Win5SpecialResultInvalidSourceError,
    Win5SpecialResultTargetMode,
    Win5SpecialResultVersionConflictError,
    Win5SpecialResultWinnerInput,
)
from uma_st2.compose import compose_win5_special_results, compose_win5_staff_result_queries
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    OperationORM,
    Win5OperationORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5SeasonORM,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class SeededSpecialRound:
    season_id: int
    round_id: int
    race_ids: tuple[int, ...]


def _seed_special_round(engine: Engine, *, suffix: str) -> SeededSpecialRound:
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as connection:
        season_id = connection.execute(
            Win5SeasonORM.__table__.insert().values(
                name=f"Special result Season {suffix}",
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
                name=f"Special result Round {suffix}",
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
    return SeededSpecialRound(
        season_id=season_id,
        round_id=round_id,
        race_ids=race_ids,
    )


def _winners(
    seeded: SeededSpecialRound,
    *,
    corrected: bool = False,
) -> tuple[Win5SpecialResultWinnerInput, ...]:
    gates = (7, 3, 9) if corrected else (3, 7, 9)
    return tuple(
        Win5SpecialResultWinnerInput(race_id=race_id, gate_number=gate_number)
        for race_id, gate_number in zip(seeded.race_ids, gates, strict=True)
    )


def _save_command(
    seeded: SeededSpecialRound,
    *,
    idempotency_key: str,
    winners: tuple[Win5SpecialResultWinnerInput, ...] | None = None,
    expected_result_fingerprint: str | None = None,
) -> SaveSpecialWin5Result:
    return SaveSpecialWin5Result(
        round_id=seeded.round_id,
        winners=_winners(seeded) if winners is None else winners,
        expected_result_fingerprint=expected_result_fingerprint,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id=f"special-result-{idempotency_key}",
        reason="integration Special result confirmed",
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


def test_concurrent_exact_entry_persists_one_complete_special_bundle_and_audit(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_special_round(migrated_engine, suffix=suffix)
    commands = compose_win5_special_results(DatabaseRuntime.from_engine(migrated_engine))
    queries = compose_win5_staff_result_queries(DatabaseRuntime.from_engine(migrated_engine))
    command_ = _save_command(
        seeded,
        idempotency_key=f"special-result-enter-{suffix}",
    )
    start = Barrier(2)

    def save_after_barrier() -> object:
        start.wait()
        return commands.save_result(command_)

    try:
        assert seeded.round_id in {
            choice.round_id for choice in queries.list_special_result_targets(mode=Win5SpecialResultTargetMode.ENTRY)
        }
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(save_after_barrier) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]

        assert results[0] == results[1]
        assert results[0].operation_type == Win5SpecialResultAuditType.ENTERED
        assert seeded.round_id in {
            choice.round_id
            for choice in queries.list_special_result_targets(mode=Win5SpecialResultTargetMode.CORRECTION)
        }

        with migrated_engine.connect() as connection:
            stored_result = connection.execute(
                select(
                    Win5ResultORM.id,
                    Win5ResultORM.race_id,
                    Win5ResultORM.position,
                    Win5ResultORM.race_entry_id,
                    Win5ResultORM.gate_number,
                )
                .where(Win5ResultORM.race_id.in_(seeded.race_ids))
                .order_by(Win5ResultORM.race_id)
            ).all()
            operation_count = connection.scalar(
                select(func.count())
                .select_from(OperationORM)
                .where(OperationORM.idempotency_key == command_.idempotency_key)
            )
            audit = connection.execute(
                select(Win5OperationORM.type, Win5OperationORM.before_data, Win5OperationORM.after_data)
                .join(OperationORM, OperationORM.id == Win5OperationORM.operation_id)
                .where(OperationORM.idempotency_key == command_.idempotency_key)
            ).one()

        assert tuple((row.race_id, row.position, row.race_entry_id, row.gate_number) for row in stored_result) == tuple(
            (race_id, 1, None, gate) for race_id, gate in zip(seeded.race_ids, (3, 7, 9), strict=True)
        )
        assert operation_count == 1
        assert audit.type == Win5SpecialResultAuditType.ENTERED.value
        assert audit.before_data is None
        assert audit.after_data == results[0].to_audit_payload()
    finally:
        _cleanup(migrated_engine, seeded)


def test_special_correction_preserves_ids_and_rejects_stale_or_noop_writes(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_special_round(migrated_engine, suffix=suffix)
    commands = compose_win5_special_results(DatabaseRuntime.from_engine(migrated_engine))

    try:
        entered = commands.save_result(
            _save_command(
                seeded,
                idempotency_key=f"special-result-enter-{suffix}",
            )
        )
        corrected_winners = _winners(seeded, corrected=True)
        corrected = commands.save_result(
            _save_command(
                seeded,
                idempotency_key=f"special-result-correct-{suffix}",
                winners=corrected_winners,
                expected_result_fingerprint=entered.result_fingerprint,
            )
        )

        assert corrected.operation_type == Win5SpecialResultAuditType.CORRECTED
        assert tuple(item.id for item in corrected.winners) == tuple(item.id for item in entered.winners)
        assert tuple(item.gate_number for item in corrected.winners) == (7, 3, 9)

        noop = commands.save_result(
            _save_command(
                seeded,
                idempotency_key=f"special-result-noop-{suffix}",
                winners=corrected_winners,
                expected_result_fingerprint=corrected.result_fingerprint,
            )
        )
        assert noop.operation_type is None
        assert noop.winners == corrected.winners

        with pytest.raises(Win5SpecialResultVersionConflictError):
            commands.save_result(
                _save_command(
                    seeded,
                    idempotency_key=f"special-result-stale-{suffix}",
                    winners=_winners(seeded),
                    expected_result_fingerprint=entered.result_fingerprint,
                )
            )

        with migrated_engine.connect() as connection:
            stored_result = connection.execute(
                select(Win5ResultORM.id, Win5ResultORM.race_id, Win5ResultORM.gate_number)
                .where(Win5ResultORM.race_id.in_(seeded.race_ids))
                .order_by(Win5ResultORM.race_id)
            ).all()
            audits = connection.execute(
                select(
                    Win5OperationORM.type,
                    Win5OperationORM.before_data,
                    Win5OperationORM.after_data,
                )
                .where(Win5OperationORM.round_id == seeded.round_id)
                .order_by(Win5OperationORM.operation_id)
            ).all()

        assert tuple(row.id for row in stored_result) == tuple(item.id for item in entered.winners)
        assert tuple(row.gate_number for row in stored_result) == (7, 3, 9)
        assert [audit.type for audit in audits] == [
            Win5SpecialResultAuditType.ENTERED.value,
            Win5SpecialResultAuditType.CORRECTED.value,
        ]
        assert audits[1].before_data == entered.to_audit_payload()
        assert audits[1].after_data == corrected.to_audit_payload()
    finally:
        _cleanup(migrated_engine, seeded)


def test_mixed_void_entry_revalidates_current_set_and_preserves_non_void_result_ids(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_special_round(migrated_engine, suffix=suffix)
    void_race_id = seeded.race_ids[1]
    non_void_race_ids = (seeded.race_ids[0], seeded.race_ids[2])
    voided_at = datetime.now(UTC).replace(tzinfo=None)
    commands = compose_win5_special_results(DatabaseRuntime.from_engine(migrated_engine))
    queries = compose_win5_staff_result_queries(DatabaseRuntime.from_engine(migrated_engine))
    initial_winners = tuple(
        Win5SpecialResultWinnerInput(race_id=race_id, gate_number=gate)
        for race_id, gate in zip(non_void_race_ids, (3, 9), strict=True)
    )

    try:
        with migrated_engine.begin() as connection:
            connection.execute(
                update(Win5RaceORM)
                .where(Win5RaceORM.id == void_race_id)
                .values(
                    void_reason="official no-contest",
                    voided_at=voided_at,
                    updated_at=voided_at,
                )
            )

        target = queries.get_special_result_target(
            round_id=seeded.round_id,
            expected_mode=Win5SpecialResultTargetMode.ENTRY,
        )
        assert target.choice.void_count == 1
        assert tuple(race.id for race in target.non_void_races) == non_void_race_ids

        with migrated_engine.begin() as connection:
            connection.execute(
                update(Win5RaceORM)
                .where(Win5RaceORM.id == void_race_id)
                .values(void_reason=None, voided_at=None, updated_at=voided_at)
            )
        with pytest.raises(Win5SpecialResultInvalidSourceError, match="non-void Race"):
            commands.save_result(
                _save_command(
                    seeded,
                    idempotency_key=f"special-result-stale-void-{suffix}",
                    winners=initial_winners,
                )
            )

        with migrated_engine.begin() as connection:
            connection.execute(
                update(Win5RaceORM)
                .where(Win5RaceORM.id == void_race_id)
                .values(
                    void_reason="official no-contest",
                    voided_at=voided_at,
                    updated_at=voided_at,
                )
            )
        entered = commands.save_result(
            _save_command(
                seeded,
                idempotency_key=f"special-result-mixed-enter-{suffix}",
                winners=initial_winners,
            )
        )
        corrected_inputs = tuple(
            Win5SpecialResultWinnerInput(race_id=race_id, gate_number=gate)
            for race_id, gate in zip(non_void_race_ids, (8, 2), strict=True)
        )
        corrected = commands.save_result(
            _save_command(
                seeded,
                idempotency_key=f"special-result-mixed-correct-{suffix}",
                winners=corrected_inputs,
                expected_result_fingerprint=entered.result_fingerprint,
            )
        )

        assert tuple(result.id for result in corrected.winners) == tuple(result.id for result in entered.winners)
        with migrated_engine.connect() as connection:
            stored_results = connection.execute(
                select(Win5ResultORM.id, Win5ResultORM.race_id, Win5ResultORM.gate_number)
                .where(Win5ResultORM.race_id.in_(seeded.race_ids))
                .order_by(Win5ResultORM.race_id)
            ).all()
            operation_types = tuple(
                connection.scalars(
                    select(Win5OperationORM.type)
                    .where(Win5OperationORM.round_id == seeded.round_id)
                    .order_by(Win5OperationORM.operation_id)
                )
            )

        assert tuple((row.race_id, row.gate_number) for row in stored_results) == tuple(
            zip(non_void_race_ids, (8, 2), strict=True)
        )
        assert void_race_id not in {row.race_id for row in stored_results}
        assert operation_types == (
            Win5SpecialResultAuditType.ENTERED.value,
            Win5SpecialResultAuditType.CORRECTED.value,
        )
    finally:
        _cleanup(migrated_engine, seeded)
