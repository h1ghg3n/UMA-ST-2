"""MariaDB atomicity and serialization evidence for staff Circle Point operations."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.point import (
    ApplyStaffCirclePoint,
    StaffCirclePointOperation,
    StaffCirclePointStaleError,
)
from uma_st2.compose import compose_staff_circle_point_commands, compose_staff_circle_point_queries
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    CirclePointORM,
    DiscordPublicationORM,
    IdentityOperationORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
)

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _suffix() -> str:
    return str(uuid4().int % 10**17).zfill(17)


def _seed(engine: Engine, *, persona_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            PersonaORM.__table__.insert().values(
                id=persona_id,
                display_name="MariaDB 포인트 대상",
                status="withdrawn",
                created_at=NOW.replace(tzinfo=None),
                updated_at=NOW.replace(tzinfo=None),
            )
        )
        connection.execute(
            CirclePointORM.__table__.insert().values(
                persona_id=persona_id,
                balance=700,
                updated_at=NOW.replace(tzinfo=None),
            )
        )


def _cleanup(engine: Engine, *, guild_id: str, persona_id: str) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(connection.scalars(select(OperationORM.id).where(OperationORM.guild_id == guild_id)))
        if operation_ids:
            connection.execute(delete(IdentityOperationORM).where(IdentityOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(PointTransactionORM).where(PointTransactionORM.operation_id.in_(operation_ids)))
        connection.execute(delete(DiscordPublicationORM).where(DiscordPublicationORM.guild_id == guild_id))
        connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id == persona_id))
        if operation_ids:
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(PersonaORM).where(PersonaORM.id == persona_id))


def _command(
    *,
    guild_id: str,
    persona_id: str,
    actor_id: str,
    key: str,
    amount: int,
    fingerprint: str,
) -> ApplyStaffCirclePoint:
    return ApplyStaffCirclePoint(
        guild_id=guild_id,
        target_persona_id=persona_id,
        operation=StaffCirclePointOperation.ADJUSTMENT,
        amount=amount,
        reason="MariaDB 운영 조정",
        expected_target_fingerprint=fingerprint,
        actor_discord_user_id=actor_id,
        idempotency_key=key,
        correlation_id=key,
    )


def test_manual_delta_commits_once_with_exact_retry_and_no_unrelated_side_effects(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    actor_id = f"9{suffix}"
    persona_id = str(uuid4())
    key = f"staff-circle-point-{suffix}"
    try:
        _seed(migrated_engine, persona_id=persona_id)
        runtime = DatabaseRuntime.from_engine(migrated_engine)
        preview = compose_staff_circle_point_queries(runtime).get_preview(
            guild_id=guild_id,
            persona_id=persona_id,
            operation=StaffCirclePointOperation.GRANT,
            amount=125,
            reason="MariaDB 수동 지급",
        )
        command = ApplyStaffCirclePoint(
            guild_id=guild_id,
            target_persona_id=persona_id,
            operation=preview.operation,
            amount=preview.amount,
            reason=preview.reason,
            expected_target_fingerprint=preview.state.state_fingerprint,
            actor_discord_user_id=actor_id,
            idempotency_key=key,
            correlation_id=key,
        )

        result = compose_staff_circle_point_commands(runtime).apply(command)
        retry = compose_staff_circle_point_commands(runtime).apply(command)

        assert result.action == "manual_grant"
        assert result.current_balance == 825
        assert retry.transaction_id == result.transaction_id
        assert retry.exact_retry is True
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(select(CirclePointORM.balance).where(CirclePointORM.persona_id == persona_id)) == 825
            )
            operation = connection.execute(
                select(
                    OperationORM.actor_discord_user_id,
                    OperationORM.idempotency_key,
                    OperationORM.reason,
                ).where(OperationORM.guild_id == guild_id)
            ).one()
            assert tuple(operation) == (actor_id, key, "MariaDB 수동 지급")
            transaction = connection.execute(
                select(
                    PointTransactionORM.action,
                    PointTransactionORM.amount,
                ).where(PointTransactionORM.persona_id == persona_id)
            ).one()
            assert tuple(transaction) == ("manual_grant", 125)
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(IdentityOperationORM)
                    .join(OperationORM, OperationORM.id == IdentityOperationORM.operation_id)
                    .where(OperationORM.guild_id == guild_id)
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(DiscordPublicationORM)
                    .where(DiscordPublicationORM.guild_id == guild_id)
                )
                == 0
            )
    finally:
        _cleanup(migrated_engine, guild_id=guild_id, persona_id=persona_id)


def test_concurrent_staff_deltas_serialize_to_one_fresh_preview_winner(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    actor_id = f"9{suffix}"
    persona_id = str(uuid4())
    barrier = Barrier(2)
    try:
        _seed(migrated_engine, persona_id=persona_id)
        runtime = DatabaseRuntime.from_engine(migrated_engine)
        state = compose_staff_circle_point_queries(runtime).get_state(
            guild_id=guild_id,
            persona_id=persona_id,
        )
        commands = tuple(
            _command(
                guild_id=guild_id,
                persona_id=persona_id,
                actor_id=actor_id,
                key=f"staff-circle-point-{suffix}-{index}",
                amount=amount,
                fingerprint=state.state_fingerprint,
            )
            for index, amount in enumerate((100, -50))
        )

        def apply(command: ApplyStaffCirclePoint) -> str:
            service = compose_staff_circle_point_commands(DatabaseRuntime.from_engine(migrated_engine))
            barrier.wait()
            try:
                service.apply(command)
            except StaffCirclePointStaleError:
                return "stale"
            return "updated"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures: tuple[Future[str], ...] = tuple(executor.submit(apply, command) for command in commands)
            outcomes = tuple(future.result(timeout=20) for future in futures)

        assert outcomes.count("updated") == 1
        assert outcomes.count("stale") == 1
        with migrated_engine.connect() as connection:
            balance = connection.scalar(select(CirclePointORM.balance).where(CirclePointORM.persona_id == persona_id))
            assert balance in {650, 800}
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(PointTransactionORM)
                    .join(OperationORM, OperationORM.id == PointTransactionORM.operation_id)
                    .where(OperationORM.guild_id == guild_id)
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, guild_id=guild_id, persona_id=persona_id)
