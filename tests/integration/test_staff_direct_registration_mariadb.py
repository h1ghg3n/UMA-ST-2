"""MariaDB atomicity and concurrency evidence for staff direct registration."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.identity import (
    DirectlyRegisterDiscordAccount,
    StaffDirectRegistrationConcurrentConflictError,
    StaffDirectRegistrationPidUnavailableError,
)
from uma_st2.compose import (
    compose_staff_direct_registration_commands,
    compose_staff_direct_registration_queries,
)
from uma_st2.domain.identity import GameRegion
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    CirclePointORM,
    DiscordAccountORM,
    DiscordPublicationORM,
    GameAccountORM,
    GameAccountRegistrationRequestORM,
    IdentityOperationORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
)

pytestmark = pytest.mark.integration


def _suffix() -> str:
    return str(uuid4().int % 10**17).zfill(17)


def _command(
    engine: Engine,
    *,
    guild_id: str,
    target_user_id: str,
    uma_pid: str,
    key: str,
) -> DirectlyRegisterDiscordAccount:
    preview = compose_staff_direct_registration_queries(DatabaseRuntime.from_engine(engine)).get_preview(
        guild_id=guild_id,
        target_discord_user_id=target_user_id,
        target_display_name_snapshot=f"MariaDB 대상 {target_user_id[-4:]}",
        game_region=GameRegion.KR,
        uma_pid=uma_pid,
        nickname=f"MariaDB 계정 {target_user_id[-4:]}",
        affiliation="통합 테스트",
        operational_note="MariaDB direct registration",
    )
    return DirectlyRegisterDiscordAccount(
        guild_id=guild_id,
        target_discord_user_id=target_user_id,
        target_display_name_snapshot=preview.target_display_name_snapshot,
        game_region=preview.state.game_region,
        uma_pid=preview.state.uma_pid,
        nickname=preview.nickname,
        affiliation=preview.affiliation,
        registered_by_discord_user_id=f"8{_suffix()}",
        expected_target_fingerprint=preview.state.state_fingerprint,
        idempotency_key=key,
        correlation_id=key,
        operational_note=preview.operational_note,
    )


def _cleanup(engine: Engine, *, guild_id: str, target_user_ids: tuple[str, ...]) -> None:
    with engine.begin() as connection:
        persona_ids = tuple(
            value
            for value in connection.scalars(
                select(DiscordAccountORM.persona_id).where(
                    DiscordAccountORM.discord_user_id.in_(target_user_ids),
                    DiscordAccountORM.persona_id.is_not(None),
                )
            )
            if value is not None
        )
        operation_ids = tuple(connection.scalars(select(OperationORM.id).where(OperationORM.guild_id == guild_id)))
        if operation_ids:
            connection.execute(delete(IdentityOperationORM).where(IdentityOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(PointTransactionORM).where(PointTransactionORM.operation_id.in_(operation_ids)))
        connection.execute(delete(DiscordPublicationORM).where(DiscordPublicationORM.guild_id == guild_id))
        connection.execute(
            delete(GameAccountRegistrationRequestORM).where(GameAccountRegistrationRequestORM.guild_id == guild_id)
        )
        connection.execute(delete(DiscordAccountORM).where(DiscordAccountORM.discord_user_id.in_(target_user_ids)))
        if persona_ids:
            connection.execute(delete(GameAccountORM).where(GameAccountORM.persona_id.in_(persona_ids)))
            connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id.in_(persona_ids)))
        if operation_ids:
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        if persona_ids:
            connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(persona_ids)))


def test_direct_registration_commits_complete_graph_once_with_exact_retry(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    target_user_id = f"9{suffix}"
    uma_pid = f"7{suffix}"
    key = f"staff-direct-registration-{suffix}"
    try:
        command = _command(
            migrated_engine,
            guild_id=guild_id,
            target_user_id=target_user_id,
            uma_pid=uma_pid,
            key=key,
        )
        commands = compose_staff_direct_registration_commands(DatabaseRuntime.from_engine(migrated_engine))

        created = commands.register(command)
        retried = commands.register(command)

        assert created.wallet_balance == created.initial_grant_amount == 500
        assert retried.exact_retry is True
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(DiscordAccountORM.persona_id).where(DiscordAccountORM.discord_user_id == target_user_id)
                )
                == created.persona_id
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(GameAccountORM)
                    .where(
                        GameAccountORM.persona_id == created.persona_id,
                        GameAccountORM.game_region == "KR",
                        GameAccountORM.uma_pid == uma_pid,
                    )
                )
                == 1
            )
            assert (
                connection.scalar(select(CirclePointORM.balance).where(CirclePointORM.persona_id == created.persona_id))
                == 500
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(PointTransactionORM)
                    .where(
                        PointTransactionORM.persona_id == created.persona_id,
                        PointTransactionORM.action == "initial_grant",
                        PointTransactionORM.amount == 500,
                    )
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
                        IdentityOperationORM.type == "account_directly_registered",
                    )
                )
                == 1
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
        _cleanup(migrated_engine, guild_id=guild_id, target_user_ids=(target_user_id,))


def test_concurrent_same_pid_direct_registration_has_exactly_one_complete_winner(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    target_user_ids = (f"8{suffix}", f"9{suffix}")
    uma_pid = f"7{suffix}"
    barrier = Barrier(2)
    try:
        commands = tuple(
            _command(
                migrated_engine,
                guild_id=guild_id,
                target_user_id=target_user_id,
                uma_pid=uma_pid,
                key=f"staff-direct-registration-{suffix}-{index}",
            )
            for index, target_user_id in enumerate(target_user_ids)
        )

        def register(command: DirectlyRegisterDiscordAccount) -> str:
            service = compose_staff_direct_registration_commands(DatabaseRuntime.from_engine(migrated_engine))
            barrier.wait()
            try:
                service.register(command)
            except StaffDirectRegistrationPidUnavailableError:
                return "pid-unavailable"
            except StaffDirectRegistrationConcurrentConflictError:
                return "concurrent-conflict"
            return "registered"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures: tuple[Future[str], ...] = tuple(executor.submit(register, command) for command in commands)
            outcomes = tuple(future.result(timeout=20) for future in futures)

        assert outcomes.count("registered") == 1
        assert len({*outcomes} - {"registered"}) == 1
        assert ({*outcomes} - {"registered"}).pop() in {
            "pid-unavailable",
            "concurrent-conflict",
        }
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
                        IdentityOperationORM.type == "account_directly_registered",
                    )
                )
                == 1
            )
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
        _cleanup(migrated_engine, guild_id=guild_id, target_user_ids=target_user_ids)
