"""MariaDB atomicity and serialization evidence for staff display edits."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.identity import (
    StaffDisplayEditStaleError,
    UpdateGameAccountDisplayInfo,
    UpdatePersonaDisplayName,
)
from uma_st2.compose import compose_staff_display_edit_commands, compose_staff_display_edit_queries
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
NOW = datetime(2026, 9, 3, 11, 0, tzinfo=UTC)


def _suffix() -> str:
    return str(uuid4().int % 10**17).zfill(17)


def _seed(engine: Engine, *, persona_id: str) -> int:
    with engine.begin() as connection:
        connection.execute(
            PersonaORM.__table__.insert().values(
                id=persona_id,
                display_name="MariaDB 기존 Persona",
                status="withdrawn",
                created_at=NOW.replace(tzinfo=None),
                updated_at=NOW.replace(tzinfo=None),
            )
        )
        result = connection.execute(
            GameAccountORM.__table__.insert().values(
                persona_id=persona_id,
                game_region="KR",
                uma_pid=None,
                nickname="MariaDB 기존 계정",
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
        return int(result.inserted_primary_key[0])


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


def test_display_edits_commit_once_on_terminal_persona_without_economy(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    persona_id = str(uuid4())
    try:
        account_id = _seed(migrated_engine, persona_id=persona_id)
        runtime = DatabaseRuntime.from_engine(migrated_engine)
        queries = compose_staff_display_edit_queries(runtime)
        commands = compose_staff_display_edit_commands(runtime)
        persona_preview = queries.get_persona_preview(
            guild_id=guild_id,
            persona_id=persona_id,
            display_name="MariaDB 수정 Persona",
            reason="MariaDB 이름 정정",
        )
        persona_command = UpdatePersonaDisplayName(
            guild_id=guild_id,
            target_persona_id=persona_id,
            display_name=persona_preview.display_name,
            reason=persona_preview.reason,
            updated_by_discord_user_id=f"8{suffix}",
            expected_target_fingerprint=persona_preview.state.state_fingerprint,
            idempotency_key=f"staff-persona-display-{suffix}",
        )

        persona_result = commands.update_persona(persona_command)
        persona_retry = commands.update_persona(persona_command)
        account_preview = queries.get_game_account_preview(
            guild_id=guild_id,
            persona_id=persona_id,
            game_account_id=account_id,
            nickname="MariaDB 수정 계정",
            affiliation=None,
            reason="MariaDB 계정 정정",
        )
        account_command = UpdateGameAccountDisplayInfo(
            guild_id=guild_id,
            target_persona_id=persona_id,
            game_account_id=account_id,
            nickname=account_preview.nickname,
            affiliation=account_preview.affiliation,
            reason=account_preview.reason,
            updated_by_discord_user_id=f"8{suffix}",
            expected_target_fingerprint=account_preview.state.state_fingerprint,
            idempotency_key=f"staff-account-display-{suffix}",
        )
        account_result = commands.update_game_account(account_command)
        account_retry = commands.update_game_account(account_command)

        assert persona_result.status.value == "withdrawn"
        assert persona_retry.exact_retry is True
        assert account_result.uma_pid is None
        assert account_retry.exact_retry is True
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(select(PersonaORM.display_name).where(PersonaORM.id == persona_id))
                == "MariaDB 수정 Persona"
            )
            account_row = connection.execute(
                select(
                    GameAccountORM.persona_id,
                    GameAccountORM.game_region,
                    GameAccountORM.uma_pid,
                    GameAccountORM.nickname,
                    GameAccountORM.affiliation,
                ).where(GameAccountORM.id == account_id)
            ).one()
            assert tuple(account_row) == (
                persona_id,
                "KR",
                None,
                "MariaDB 수정 계정",
                None,
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(IdentityOperationORM)
                    .join(OperationORM, OperationORM.id == IdentityOperationORM.operation_id)
                    .where(OperationORM.guild_id == guild_id)
                )
                == 2
            )
            assert (
                connection.scalar(select(CirclePointORM.balance).where(CirclePointORM.persona_id == persona_id)) == 700
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(PointTransactionORM)
                    .where(PointTransactionORM.persona_id == persona_id)
                )
                == 0
            )
    finally:
        _cleanup(migrated_engine, guild_id=guild_id, persona_id=persona_id)


def test_concurrent_persona_display_edits_serialize_to_one_winner(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    persona_id = str(uuid4())
    barrier = Barrier(2)
    try:
        _seed(migrated_engine, persona_id=persona_id)
        runtime = DatabaseRuntime.from_engine(migrated_engine)
        preview = compose_staff_display_edit_queries(runtime).get_persona_preview(
            guild_id=guild_id,
            persona_id=persona_id,
            display_name="임시",
            reason="동시 정정",
        )
        commands = tuple(
            UpdatePersonaDisplayName(
                guild_id=guild_id,
                target_persona_id=persona_id,
                display_name=f"동시 수정 {index}",
                reason="동시 정정",
                updated_by_discord_user_id=f"8{suffix}",
                expected_target_fingerprint=preview.state.state_fingerprint,
                idempotency_key=f"staff-persona-display-{suffix}-{index}",
            )
            for index in range(2)
        )

        def update(command: UpdatePersonaDisplayName) -> str:
            service = compose_staff_display_edit_commands(DatabaseRuntime.from_engine(migrated_engine))
            barrier.wait()
            try:
                service.update_persona(command)
            except StaffDisplayEditStaleError:
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
                        IdentityOperationORM.type == "persona_display_name_updated",
                    )
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, guild_id=guild_id, persona_id=persona_id)
