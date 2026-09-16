"""MariaDB transaction evidence for authoritative Normal WIN5 results."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.win5 import (
    SaveNormalWin5Result,
    Win5NormalResultAuditType,
    Win5NormalResultPlacementInput,
    Win5NormalResultVersionConflictError,
)
from uma_st2.compose import compose_win5_normal_results
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    OperationORM,
    Win5OperationORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5SeasonORM,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class SeededNormalRound:
    season_id: int
    round_id: int
    race_id: int
    entry_ids: tuple[int, ...]


def _seed_normal_round(engine: Engine, *, suffix: str) -> SeededNormalRound:
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as connection:
        season_id = connection.execute(
            Win5SeasonORM.__table__.insert().values(
                name=f"Normal result Season {suffix}",
                status="active",
                active_marker=True,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        round_id = connection.execute(
            Win5RoundORM.__table__.insert().values(
                season_id=season_id,
                type="normal",
                status="closed",
                name=f"Normal result Round {suffix}",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        race_id = connection.execute(
            Win5RaceORM.__table__.insert().values(
                round_id=round_id,
                name=f"Normal result Race {suffix}",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        entry_ids = tuple(
            connection.execute(
                Win5RaceEntryORM.__table__.insert().values(
                    race_id=race_id,
                    gate_number=gate_number,
                    name=f"entry-{gate_number}-{suffix}",
                    created_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key[0]
            for gate_number in range(1, 7)
        )
    return SeededNormalRound(
        season_id=season_id,
        round_id=round_id,
        race_id=race_id,
        entry_ids=entry_ids,
    )


def _placements(
    seeded: SeededNormalRound,
    *,
    swap_first_two: bool = False,
) -> tuple[Win5NormalResultPlacementInput, ...]:
    entry_ids = list(seeded.entry_ids[:5])
    if swap_first_two:
        entry_ids[:2] = reversed(entry_ids[:2])
    return tuple(
        Win5NormalResultPlacementInput(position=position, race_entry_id=entry_id)
        for position, entry_id in enumerate(entry_ids, start=1)
    )


def _save_command(
    seeded: SeededNormalRound,
    *,
    idempotency_key: str,
    placements: tuple[Win5NormalResultPlacementInput, ...] | None = None,
    expected_result_fingerprint: str | None = None,
) -> SaveNormalWin5Result:
    return SaveNormalWin5Result(
        round_id=seeded.round_id,
        placements=_placements(seeded) if placements is None else placements,
        expected_result_fingerprint=expected_result_fingerprint,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id=f"normal-result-{idempotency_key}",
        reason="integration result confirmed",
    )


def _cleanup(engine: Engine, seeded: SeededNormalRound) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(Win5OperationORM.operation_id).where(Win5OperationORM.round_id == seeded.round_id)
            )
        )
        if operation_ids:
            connection.execute(delete(Win5OperationORM).where(Win5OperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(Win5ResultORM).where(Win5ResultORM.race_id == seeded.race_id))
        connection.execute(delete(Win5RaceEntryORM).where(Win5RaceEntryORM.race_id == seeded.race_id))
        connection.execute(delete(Win5RaceORM).where(Win5RaceORM.id == seeded.race_id))
        connection.execute(delete(Win5RoundORM).where(Win5RoundORM.id == seeded.round_id))
        connection.execute(delete(Win5SeasonORM).where(Win5SeasonORM.id == seeded.season_id))


def test_concurrent_exact_entry_persists_one_complete_result_and_audit(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_normal_round(migrated_engine, suffix=suffix)
    commands = compose_win5_normal_results(DatabaseRuntime.from_engine(migrated_engine))
    command_ = _save_command(
        seeded,
        idempotency_key=f"normal-result-enter-{suffix}",
    )
    start = Barrier(2)

    def save_after_barrier() -> object:
        start.wait()
        return commands.save_result(command_)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(save_after_barrier) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]

        assert results[0] == results[1]
        assert results[0].operation_type == Win5NormalResultAuditType.ENTERED

        with migrated_engine.connect() as connection:
            stored_result = connection.execute(
                select(
                    Win5ResultORM.id,
                    Win5ResultORM.position,
                    Win5ResultORM.race_entry_id,
                )
                .where(Win5ResultORM.race_id == seeded.race_id)
                .order_by(Win5ResultORM.position)
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

        assert tuple((row.position, row.race_entry_id) for row in stored_result) == tuple(
            (position, entry_id) for position, entry_id in enumerate(seeded.entry_ids[:5], start=1)
        )
        assert operation_count == 1
        assert audit.type == Win5NormalResultAuditType.ENTERED.value
        assert audit.before_data is None
        assert audit.after_data == results[0].to_audit_payload()
    finally:
        _cleanup(migrated_engine, seeded)


def test_correction_preserves_result_ids_and_rejects_stale_or_noop_writes(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_normal_round(migrated_engine, suffix=suffix)
    commands = compose_win5_normal_results(DatabaseRuntime.from_engine(migrated_engine))

    try:
        entered = commands.save_result(
            _save_command(
                seeded,
                idempotency_key=f"normal-result-enter-{suffix}",
            )
        )
        corrected_placements = _placements(seeded, swap_first_two=True)
        corrected = commands.save_result(
            _save_command(
                seeded,
                idempotency_key=f"normal-result-correct-{suffix}",
                placements=corrected_placements,
                expected_result_fingerprint=entered.result_fingerprint,
            )
        )

        assert corrected.operation_type == Win5NormalResultAuditType.CORRECTED
        assert tuple(item.id for item in corrected.placements) == tuple(item.id for item in entered.placements)
        assert tuple(item.race_entry_id for item in corrected.placements) == (
            seeded.entry_ids[1],
            seeded.entry_ids[0],
            *seeded.entry_ids[2:5],
        )

        noop = commands.save_result(
            _save_command(
                seeded,
                idempotency_key=f"normal-result-noop-{suffix}",
                placements=corrected_placements,
                expected_result_fingerprint=corrected.result_fingerprint,
            )
        )
        assert noop.operation_type is None
        assert noop.placements == corrected.placements

        with pytest.raises(Win5NormalResultVersionConflictError):
            commands.save_result(
                _save_command(
                    seeded,
                    idempotency_key=f"normal-result-stale-{suffix}",
                    placements=_placements(seeded),
                    expected_result_fingerprint=entered.result_fingerprint,
                )
            )

        with migrated_engine.connect() as connection:
            stored_result = connection.execute(
                select(
                    Win5ResultORM.id,
                    Win5ResultORM.position,
                    Win5ResultORM.race_entry_id,
                )
                .where(Win5ResultORM.race_id == seeded.race_id)
                .order_by(Win5ResultORM.position)
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

        assert tuple(row.id for row in stored_result) == tuple(item.id for item in entered.placements)
        assert tuple(row.race_entry_id for row in stored_result) == tuple(
            item.race_entry_id for item in corrected.placements
        )
        assert [audit.type for audit in audits] == [
            Win5NormalResultAuditType.ENTERED.value,
            Win5NormalResultAuditType.CORRECTED.value,
        ]
        assert audits[1].before_data == entered.to_audit_payload()
        assert audits[1].after_data == corrected.to_audit_payload()
    finally:
        _cleanup(migrated_engine, seeded)
