"""MariaDB atomicity and serialization evidence for staff Persona status management."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.identity import ChangePersonaStatus, StaffPersonaStatusStaleError
from uma_st2.compose import compose_staff_persona_status_commands, compose_staff_persona_status_queries
from uma_st2.domain.identity import PersonaStatus
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    CirclePointORM,
    DiscordPublicationORM,
    GameAccountORM,
    IdentityOperationORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
)

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _suffix() -> str:
    return str(uuid4().int % 10**17).zfill(17)


def _seed(engine: Engine, *, persona_id: str, pid: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            PersonaORM.__table__.insert().values(
                id=persona_id,
                display_name="MariaDB 상태 대상",
                status="normal",
                created_at=NOW.replace(tzinfo=None),
                updated_at=NOW.replace(tzinfo=None),
            )
        )
        connection.execute(
            GameAccountORM.__table__.insert().values(
                persona_id=persona_id,
                game_region="KR",
                uma_pid=pid,
                nickname="MariaDB 계정",
                affiliation="기존 소속",
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
        connection.execute(delete(GameAccountORM).where(GameAccountORM.persona_id == persona_id))
        connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id == persona_id))
        if operation_ids:
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(PersonaORM).where(PersonaORM.id == persona_id))


def test_status_change_commits_once_without_identity_economy_or_publication_side_effects(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    persona_id = str(uuid4())
    pid = f"8{suffix}"
    try:
        _seed(migrated_engine, persona_id=persona_id, pid=pid)
        runtime = DatabaseRuntime.from_engine(migrated_engine)
        preview = compose_staff_persona_status_queries(runtime).get_preview(
            guild_id=guild_id,
            persona_id=persona_id,
            desired_status=PersonaStatus.PENDING_APPROVAL,
            reason="MariaDB 상태 검토",
        )
        command = ChangePersonaStatus(
            guild_id=guild_id,
            target_persona_id=persona_id,
            desired_status=preview.desired_status,
            reason=preview.reason,
            updated_by_discord_user_id=f"9{suffix}",
            expected_target_fingerprint=preview.state.state_fingerprint,
            idempotency_key=f"staff-persona-status-{suffix}",
        )

        result = compose_staff_persona_status_commands(runtime).change(command)
        retry = compose_staff_persona_status_commands(runtime).change(command)

        assert result.previous_status is PersonaStatus.NORMAL
        assert result.status is PersonaStatus.PENDING_APPROVAL
        assert retry.exact_retry is True
        with migrated_engine.connect() as connection:
            assert connection.scalar(select(PersonaORM.status).where(PersonaORM.id == persona_id)) == "pending_approval"
            assert (
                connection.scalar(select(CirclePointORM.balance).where(CirclePointORM.persona_id == persona_id)) == 700
            )
            account = connection.execute(
                select(GameAccountORM.persona_id, GameAccountORM.uma_pid, GameAccountORM.affiliation).where(
                    GameAccountORM.persona_id == persona_id
                )
            ).one()
            assert tuple(account) == (persona_id, pid, "기존 소속")
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(IdentityOperationORM)
                    .join(OperationORM, OperationORM.id == IdentityOperationORM.operation_id)
                    .where(
                        OperationORM.guild_id == guild_id,
                        IdentityOperationORM.type == "persona_status_changed",
                    )
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(PointTransactionORM)
                    .where(PointTransactionORM.persona_id == persona_id)
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


def test_concurrent_status_changes_serialize_to_one_winner(migrated_engine: Engine) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    persona_id = str(uuid4())
    barrier = Barrier(2)
    try:
        _seed(migrated_engine, persona_id=persona_id, pid=f"8{suffix}")
        runtime = DatabaseRuntime.from_engine(migrated_engine)
        state = compose_staff_persona_status_queries(runtime).get_state(
            guild_id=guild_id,
            persona_id=persona_id,
        )
        commands = tuple(
            ChangePersonaStatus(
                guild_id=guild_id,
                target_persona_id=persona_id,
                desired_status=desired,
                reason="동시 상태 변경",
                updated_by_discord_user_id=f"9{suffix}",
                expected_target_fingerprint=state.state_fingerprint,
                idempotency_key=f"staff-persona-status-{suffix}-{index}",
            )
            for index, desired in enumerate((PersonaStatus.WARNING, PersonaStatus.EXPELLED))
        )

        def update(command: ChangePersonaStatus) -> str:
            service = compose_staff_persona_status_commands(DatabaseRuntime.from_engine(migrated_engine))
            barrier.wait()
            try:
                service.change(command)
            except StaffPersonaStatusStaleError:
                return "stale"
            return "updated"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures: tuple[Future[str], ...] = tuple(executor.submit(update, command) for command in commands)
            outcomes = tuple(future.result(timeout=20) for future in futures)

        assert outcomes.count("updated") == 1
        assert outcomes.count("stale") == 1
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(IdentityOperationORM)
                    .join(OperationORM, OperationORM.id == IdentityOperationORM.operation_id)
                    .where(
                        OperationORM.guild_id == guild_id,
                        IdentityOperationORM.type == "persona_status_changed",
                    )
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, guild_id=guild_id, persona_id=persona_id)
