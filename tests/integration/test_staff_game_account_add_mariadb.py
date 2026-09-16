"""MariaDB atomicity and serialization evidence for peer GameAccount addition."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.identity import (
    AddGameAccountToPersona,
    StaffGameAccountAddConcurrentConflictError,
    StaffGameAccountAddPidUnavailableError,
)
from uma_st2.compose import compose_staff_game_account_add_commands, compose_staff_game_account_add_queries
from uma_st2.domain.identity import GameRegion
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
NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)


def _suffix() -> str:
    return str(uuid4().int % 10**17).zfill(17)


def _seed_personas(engine: Engine, *, persona_ids: tuple[str, ...]) -> None:
    with engine.begin() as connection:
        for index, persona_id in enumerate(persona_ids):
            connection.execute(
                PersonaORM.__table__.insert().values(
                    id=persona_id,
                    display_name=f"MariaDB Persona {index + 1}",
                    status="normal",
                    created_at=NOW.replace(tzinfo=None),
                    updated_at=NOW.replace(tzinfo=None),
                )
            )
            connection.execute(
                GameAccountORM.__table__.insert().values(
                    persona_id=persona_id,
                    game_region="KR",
                    uma_pid=f"7{_suffix()}",
                    nickname=f"기존 계정 {index + 1}",
                    affiliation=None,
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


def _command(
    engine: Engine,
    *,
    guild_id: str,
    persona_id: str,
    uma_pid: str,
    key: str,
) -> AddGameAccountToPersona:
    runtime = DatabaseRuntime.from_engine(engine)
    preview = compose_staff_game_account_add_queries(runtime).get_preview(
        guild_id=guild_id,
        persona_id=persona_id,
        game_region=GameRegion.JP,
        uma_pid=uma_pid,
        nickname="MariaDB 새 계정",
        affiliation="통합 테스트",
        reason="MariaDB peer add",
    )
    return AddGameAccountToPersona(
        guild_id=guild_id,
        target_persona_id=persona_id,
        game_region=preview.state.game_region,
        uma_pid=preview.state.uma_pid,
        nickname=preview.nickname,
        affiliation=preview.affiliation,
        reason=preview.reason,
        added_by_discord_user_id=f"8{_suffix()}",
        expected_target_fingerprint=preview.state.state_fingerprint,
        idempotency_key=key,
        correlation_id=key,
    )


def _cleanup(engine: Engine, *, guild_id: str, persona_ids: tuple[str, ...]) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(connection.scalars(select(OperationORM.id).where(OperationORM.guild_id == guild_id)))
        if operation_ids:
            connection.execute(delete(IdentityOperationORM).where(IdentityOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(PointTransactionORM).where(PointTransactionORM.operation_id.in_(operation_ids)))
        connection.execute(delete(DiscordPublicationORM).where(DiscordPublicationORM.guild_id == guild_id))
        connection.execute(delete(GameAccountORM).where(GameAccountORM.persona_id.in_(persona_ids)))
        connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id.in_(persona_ids)))
        if operation_ids:
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(persona_ids)))


def test_peer_add_commits_account_and_audit_once_without_economy(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    persona_id = str(uuid4())
    uma_pid = f"9{suffix}"
    try:
        _seed_personas(migrated_engine, persona_ids=(persona_id,))
        command = _command(
            migrated_engine,
            guild_id=guild_id,
            persona_id=persona_id,
            uma_pid=uma_pid,
            key=f"staff-game-account-add-{suffix}",
        )
        service = compose_staff_game_account_add_commands(DatabaseRuntime.from_engine(migrated_engine))

        created = service.add(command)
        retried = service.add(command)

        assert created.member_mutation_eligible is True
        assert retried.exact_retry is True
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(GameAccountORM)
                    .where(
                        GameAccountORM.persona_id == persona_id,
                        GameAccountORM.game_region == "JP",
                        GameAccountORM.uma_pid == uma_pid,
                    )
                )
                == 1
            )
            assert (
                connection.scalar(select(CirclePointORM.balance).where(CirclePointORM.persona_id == persona_id)) == 700
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(IdentityOperationORM)
                    .join(OperationORM, OperationORM.id == IdentityOperationORM.operation_id)
                    .where(
                        OperationORM.guild_id == guild_id,
                        IdentityOperationORM.type == "game_account_added",
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
    finally:
        _cleanup(migrated_engine, guild_id=guild_id, persona_ids=(persona_id,))


def test_concurrent_same_pid_peer_add_has_exactly_one_winner(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    persona_ids = (str(uuid4()), str(uuid4()))
    uma_pid = f"9{suffix}"
    barrier = Barrier(2)
    try:
        _seed_personas(migrated_engine, persona_ids=persona_ids)
        commands = tuple(
            _command(
                migrated_engine,
                guild_id=guild_id,
                persona_id=persona_id,
                uma_pid=uma_pid,
                key=f"staff-game-account-add-{suffix}-{index}",
            )
            for index, persona_id in enumerate(persona_ids)
        )

        def add(command: AddGameAccountToPersona) -> str:
            service = compose_staff_game_account_add_commands(DatabaseRuntime.from_engine(migrated_engine))
            barrier.wait()
            try:
                service.add(command)
            except StaffGameAccountAddPidUnavailableError:
                return "pid-unavailable"
            except StaffGameAccountAddConcurrentConflictError:
                return "concurrent-conflict"
            return "added"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures: tuple[Future[str], ...] = tuple(executor.submit(add, command) for command in commands)
            outcomes = tuple(future.result(timeout=20) for future in futures)

        assert outcomes.count("added") == 1
        assert len({*outcomes} - {"added"}) == 1
        assert ({*outcomes} - {"added"}).pop() in {"pid-unavailable", "concurrent-conflict"}
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count()).select_from(GameAccountORM).where(GameAccountORM.uma_pid == uma_pid)
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(IdentityOperationORM)
                    .join(OperationORM, OperationORM.id == IdentityOperationORM.operation_id)
                    .where(
                        OperationORM.guild_id == guild_id,
                        IdentityOperationORM.type == "game_account_added",
                    )
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, guild_id=guild_id, persona_ids=persona_ids)
