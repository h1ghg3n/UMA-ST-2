"""MariaDB atomicity/idempotency evidence for guarded WIN5 setup-Round deletion."""

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
    DeleteWin5SetupRound,
    Win5SetupRoundDeletionAuditType,
    Win5SetupRoundDeletionSnapshot,
    Win5SetupRoundDeletionUnavailableError,
    Win5SetupRoundDeletionVersionConflictError,
    Win5StaffRoundDeletionUnavailableError,
)
from uma_st2.compose import (
    compose_win5_setup_round_deletion,
    compose_win5_staff_round_deletion_queries,
)
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    DiscordPublicationORM,
    OperationORM,
    Win5OperationORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5RoundORM,
    Win5SeasonORM,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class SeededDeletionSeason:
    season_id: int
    round_ids: tuple[int, ...]
    race_ids: tuple[int, ...]
    entry_ids: tuple[int, ...]


def _seed_deletion_season(
    engine: Engine,
    *,
    suffix: str,
    round_types: tuple[str, ...],
) -> SeededDeletionSeason:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    round_ids: list[int] = []
    race_ids: list[int] = []
    entry_ids: list[int] = []
    with engine.begin() as connection:
        season_id = connection.execute(
            Win5SeasonORM.__table__.insert().values(
                name=f"Deletion Season {suffix}",
                status="active",
                active_marker=True,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        for round_index, round_type in enumerate(round_types, start=1):
            round_id = connection.execute(
                Win5RoundORM.__table__.insert().values(
                    season_id=season_id,
                    type=round_type,
                    status="setup",
                    name=f"Deletion {round_type} Round {round_index} {suffix}",
                    created_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key[0]
            round_ids.append(round_id)
            race_count = 1 if round_type == "normal" else 3
            for race_index in range(1, race_count + 1):
                race_id = connection.execute(
                    Win5RaceORM.__table__.insert().values(
                        round_id=round_id,
                        name=f"Deletion Race {round_index}-{race_index} {suffix}",
                        scheduled_at=now if race_index == 1 else None,
                        created_at=now,
                        updated_at=now,
                    )
                ).inserted_primary_key[0]
                race_ids.append(race_id)
                if round_type == "normal":
                    for gate_number in (1, 2, 4, 7, 8):
                        entry_ids.append(
                            connection.execute(
                                Win5RaceEntryORM.__table__.insert().values(
                                    race_id=race_id,
                                    gate_number=gate_number,
                                    name=f"Horse {gate_number} {suffix}",
                                    created_at=now,
                                    updated_at=now,
                                )
                            ).inserted_primary_key[0]
                        )
    return SeededDeletionSeason(
        season_id=season_id,
        round_ids=tuple(round_ids),
        race_ids=tuple(race_ids),
        entry_ids=tuple(entry_ids),
    )


def _deletion_command(
    snapshot: Win5SetupRoundDeletionSnapshot,
    *,
    suffix: str,
) -> DeleteWin5SetupRound:
    return DeleteWin5SetupRound(
        season_id=snapshot.season_id,
        round_id=snapshot.round_id,
        expected_graph_fingerprint=snapshot.graph_fingerprint,
        idempotency_key=f"setup-round-delete-{suffix}",
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id=f"deletion-{suffix}",
        reason="integration deletion confirmed",
    )


def _cleanup(engine: Engine, seeded: SeededDeletionSeason) -> None:
    with engine.begin() as connection:
        connection.execute(
            delete(DiscordPublicationORM).where(
                DiscordPublicationORM.source_kind == "win5_round",
                DiscordPublicationORM.source_id.in_(seeded.round_ids),
            )
        )
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


def test_normal_and_special_deletion_remove_graph_and_retain_exact_retry_audit(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_deletion_season(
        migrated_engine,
        suffix=suffix,
        round_types=("normal", "special"),
    )
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    queries = compose_win5_staff_round_deletion_queries(runtime)
    commands = compose_win5_setup_round_deletion(runtime)

    try:
        page = queries.list_setup_round_deletion_targets()
        assert {choice.round_id for choice in page.choices} == set(seeded.round_ids)

        normal_snapshot = queries.get_setup_round_deletion_target(round_id=seeded.round_ids[0])
        normal_command = _deletion_command(normal_snapshot, suffix=f"normal-{suffix}")
        deleted_normal = commands.delete_round(normal_command)

        with migrated_engine.begin() as connection:
            connection.execute(
                update(Win5SeasonORM)
                .where(Win5SeasonORM.id == seeded.season_id)
                .values(status="closed", active_marker=None)
            )
        assert commands.delete_round(normal_command) == deleted_normal

        with migrated_engine.begin() as connection:
            connection.execute(
                update(Win5SeasonORM)
                .where(Win5SeasonORM.id == seeded.season_id)
                .values(status="active", active_marker=True)
            )
        special_snapshot = queries.get_setup_round_deletion_target(round_id=seeded.round_ids[1])
        deleted_special = commands.delete_round(_deletion_command(special_snapshot, suffix=f"special-{suffix}"))

        with migrated_engine.connect() as connection:
            round_count = connection.scalar(
                select(func.count()).select_from(Win5RoundORM).where(Win5RoundORM.id.in_(seeded.round_ids))
            )
            race_count = connection.scalar(
                select(func.count()).select_from(Win5RaceORM).where(Win5RaceORM.id.in_(seeded.race_ids))
            )
            entry_count = connection.scalar(
                select(func.count()).select_from(Win5RaceEntryORM).where(Win5RaceEntryORM.id.in_(seeded.entry_ids))
            )
            audits = connection.execute(
                select(
                    Win5OperationORM.type,
                    Win5OperationORM.round_id,
                    Win5OperationORM.before_data,
                    Win5OperationORM.after_data,
                )
                .where(Win5OperationORM.season_id == seeded.season_id)
                .order_by(Win5OperationORM.operation_id)
            ).all()

        assert (round_count, race_count, entry_count) == (0, 0, 0)
        assert [audit.type for audit in audits] == [
            Win5SetupRoundDeletionAuditType.DELETED.value,
            Win5SetupRoundDeletionAuditType.DELETED.value,
        ]
        assert [audit.round_id for audit in audits] == list(seeded.round_ids)
        assert audits[0].before_data == normal_snapshot.to_audit_payload()
        assert audits[0].after_data == {
            "schema_version": 1,
            "deleted": True,
            "season_id": seeded.season_id,
            "round_id": normal_snapshot.round_id,
            "graph_fingerprint": normal_snapshot.graph_fingerprint,
        }
        assert deleted_special.snapshot == special_snapshot
    finally:
        _cleanup(migrated_engine, seeded)


def test_stale_graph_and_durable_publication_each_reject_without_partial_write(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_deletion_season(migrated_engine, suffix=suffix, round_types=("normal",))
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    queries = compose_win5_staff_round_deletion_queries(runtime)
    commands = compose_win5_setup_round_deletion(runtime)

    try:
        stale_snapshot = queries.get_setup_round_deletion_target(round_id=seeded.round_ids[0])
        with migrated_engine.begin() as connection:
            connection.execute(
                update(Win5RaceEntryORM)
                .where(Win5RaceEntryORM.id == seeded.entry_ids[0])
                .values(name=f"Corrected Horse {suffix}")
            )

        with pytest.raises(Win5SetupRoundDeletionVersionConflictError):
            commands.delete_round(_deletion_command(stale_snapshot, suffix=f"stale-{suffix}"))

        fresh_snapshot = queries.get_setup_round_deletion_target(round_id=seeded.round_ids[0])
        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        with migrated_engine.begin() as connection:
            connection.execute(
                DiscordPublicationORM.__table__.insert().values(
                    guild_id="987654321",
                    destination_kind="win5_announcement",
                    event_type="win5_round_result",
                    event_key=f"round:{seeded.round_ids[0]}:{suffix}",
                    source_kind="win5_round",
                    source_id=seeded.round_ids[0],
                    payload_json={"round_id": seeded.round_ids[0]},
                    payload_fingerprint="0" * 64,
                    status="pending",
                    attempt_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )

        assert queries.list_setup_round_deletion_targets().choices == ()
        with pytest.raises(Win5StaffRoundDeletionUnavailableError, match="downstream"):
            queries.get_setup_round_deletion_target(round_id=seeded.round_ids[0])
        with pytest.raises(Win5SetupRoundDeletionUnavailableError, match="downstream"):
            commands.delete_round(_deletion_command(fresh_snapshot, suffix=f"publication-{suffix}"))

        with migrated_engine.connect() as connection:
            round_count = connection.scalar(
                select(func.count()).select_from(Win5RoundORM).where(Win5RoundORM.id == seeded.round_ids[0])
            )
            race_count = connection.scalar(
                select(func.count()).select_from(Win5RaceORM).where(Win5RaceORM.id.in_(seeded.race_ids))
            )
            entry_count = connection.scalar(
                select(func.count()).select_from(Win5RaceEntryORM).where(Win5RaceEntryORM.id.in_(seeded.entry_ids))
            )
            audit_count = connection.scalar(
                select(func.count()).select_from(Win5OperationORM).where(Win5OperationORM.season_id == seeded.season_id)
            )
        assert (round_count, race_count, entry_count, audit_count) == (1, 1, 5, 0)
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_exact_deletion_retry_converges_to_one_operation(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_deletion_season(migrated_engine, suffix=suffix, round_types=("special",))
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    queries = compose_win5_staff_round_deletion_queries(runtime)
    commands = compose_win5_setup_round_deletion(runtime)
    snapshot = queries.get_setup_round_deletion_target(round_id=seeded.round_ids[0])
    command_ = _deletion_command(snapshot, suffix=f"retry-{suffix}")
    start = Barrier(2)

    def delete_after_barrier() -> object:
        start.wait()
        return commands.delete_round(command_)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(delete_after_barrier) for _ in range(2)]
            results = [_resolve(future) for future in futures]

        assert results[0] == results[1]
        with migrated_engine.connect() as connection:
            round_count = connection.scalar(
                select(func.count()).select_from(Win5RoundORM).where(Win5RoundORM.id == seeded.round_ids[0])
            )
            operation_count = connection.scalar(
                select(func.count())
                .select_from(OperationORM)
                .where(OperationORM.idempotency_key == command_.idempotency_key)
            )
        assert round_count == 0
        assert operation_count == 1
    finally:
        _cleanup(migrated_engine, seeded)
