"""MariaDB atomicity and exact-retry evidence for Discord guild provisioning."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.discord import (
    DISCORD_GUILD_PROVISIONING_AUDIT_SCHEMA_VERSION,
    DISCORD_GUILD_PROVISIONING_OPERATION_TYPE,
    DiscordGuildProvisioningConflictError,
    DiscordGuildProvisioningValues,
    ProvisionDiscordGuildSettings,
)
from uma_st2.compose import compose_discord_guild_provisioning
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyDiscordGuildProvisioningRepository,
)
from uma_st2.infrastructure.database.orm import BotGuildSettingORM, OperationORM, SettingsOperationORM

pytestmark = pytest.mark.integration


def _snowflake(prefix: int, suffix: int) -> str:
    return str(prefix * 10**18 + suffix % 10**18)


def _command(
    *,
    suffix: int,
    match_enabled: bool = True,
    with_publication_destinations: bool = True,
) -> ProvisionDiscordGuildSettings:
    return ProvisionDiscordGuildSettings(
        values=DiscordGuildProvisioningValues(
            guild_id=_snowflake(1, suffix),
            win5_announcement_channel_id=(_snowflake(2, suffix) if with_publication_destinations else None),
            match_announcement_channel_id=(_snowflake(3, suffix) if with_publication_destinations else None),
            log_channel_id=None,
            operator_role_id=_snowflake(4, suffix),
            bot_manager_role_id=None,
            default_timezone="Asia/Seoul",
            win5_announcements_enabled=True,
            match_announcements_enabled=match_enabled,
        ),
        actor_discord_user_id=_snowflake(5, suffix),
        reason="reviewed MariaDB provisioning evidence",
    )


def _cleanup(engine: Engine, *, guild_id: str) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(SettingsOperationORM.operation_id).where(SettingsOperationORM.guild_id == guild_id)
            )
        )
        if operation_ids:
            connection.execute(delete(SettingsOperationORM).where(SettingsOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id))


def test_provisioning_commits_settings_and_audit_once_with_exact_retry(migrated_engine: Engine) -> None:
    suffix = uuid4().int % 10**18
    command_ = _command(suffix=suffix)
    commands = compose_discord_guild_provisioning(DatabaseRuntime.from_engine(migrated_engine))
    try:
        created = commands.provision(command_)
        retried = commands.provision(command_)

        assert created.created is True
        assert retried.created is False
        assert created.operation_id == retried.operation_id
        assert created.snapshot == retried.snapshot
        with migrated_engine.connect() as connection:
            setting = connection.execute(
                select(
                    BotGuildSettingORM.default_timezone,
                    BotGuildSettingORM.operator_role_id,
                ).where(BotGuildSettingORM.guild_id == command_.values.guild_id)
            ).one()
            operation = connection.execute(
                select(
                    OperationORM.idempotency_key,
                    OperationORM.request_fingerprint,
                ).where(OperationORM.id == created.operation_id)
            ).one()
            settings_operation = connection.execute(
                select(
                    SettingsOperationORM.type,
                    SettingsOperationORM.before_data,
                    SettingsOperationORM.after_data,
                ).where(SettingsOperationORM.operation_id == created.operation_id)
            ).one()
            assert setting.default_timezone == "Asia/Seoul"
            assert setting.operator_role_id == command_.values.operator_role_id
            assert operation.idempotency_key == command_.idempotency_key
            assert operation.request_fingerprint == command_.request_fingerprint
            assert settings_operation.type == DISCORD_GUILD_PROVISIONING_OPERATION_TYPE
            assert settings_operation.before_data is None
            assert settings_operation.after_data["schema_version"] == (DISCORD_GUILD_PROVISIONING_AUDIT_SCHEMA_VERSION)

        with pytest.raises(DiscordGuildProvisioningConflictError, match="cannot overwrite"):
            commands.provision(_command(suffix=suffix, match_enabled=False))
    finally:
        _cleanup(migrated_engine, guild_id=command_.values.guild_id)


def test_repository_failure_rolls_back_settings_and_operation(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().int % 10**18
    command_ = _command(suffix=suffix)
    commands = compose_discord_guild_provisioning(DatabaseRuntime.from_engine(migrated_engine))

    def fail_audit(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(SqlAlchemyDiscordGuildProvisioningRepository, "add_audit", fail_audit)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        commands.provision(command_)

    with migrated_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(BotGuildSettingORM)
                .where(BotGuildSettingORM.guild_id == command_.values.guild_id)
            )
            == 0
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(SettingsOperationORM)
                .where(SettingsOperationORM.guild_id == command_.values.guild_id)
            )
            == 0
        )


def test_provisioning_persists_nullable_publication_destinations(migrated_engine: Engine) -> None:
    suffix = uuid4().int % 10**18
    command_ = _command(suffix=suffix, with_publication_destinations=False)
    commands = compose_discord_guild_provisioning(DatabaseRuntime.from_engine(migrated_engine))
    try:
        receipt = commands.provision(command_)

        with migrated_engine.connect() as connection:
            setting = connection.execute(
                select(
                    BotGuildSettingORM.win5_announcement_channel_id,
                    BotGuildSettingORM.match_announcement_channel_id,
                ).where(BotGuildSettingORM.guild_id == command_.values.guild_id)
            ).one()
        assert receipt.snapshot.values.win5_announcement_channel_id is None
        assert receipt.snapshot.values.match_announcement_channel_id is None
        assert setting.win5_announcement_channel_id is None
        assert setting.match_announcement_channel_id is None
    finally:
        _cleanup(migrated_engine, guild_id=command_.values.guild_id)


def test_concurrent_same_manifest_converges_to_one_operation(migrated_engine: Engine) -> None:
    suffix = uuid4().int % 10**18
    command_ = _command(suffix=suffix)
    barrier = Barrier(2)

    def provision() -> object:
        commands = compose_discord_guild_provisioning(DatabaseRuntime.from_engine(migrated_engine))
        barrier.wait()
        return commands.provision(command_)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures: tuple[Future[object], ...] = tuple(executor.submit(provision) for _ in range(2))
            receipts = tuple(future.result(timeout=20) for future in futures)

        assert sorted(receipt.created for receipt in receipts) == [False, True]  # type: ignore[attr-defined]
        assert len({receipt.operation_id for receipt in receipts}) == 1  # type: ignore[attr-defined]
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(BotGuildSettingORM)
                    .where(BotGuildSettingORM.guild_id == command_.values.guild_id)
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(SettingsOperationORM)
                    .where(SettingsOperationORM.guild_id == command_.values.guild_id)
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, guild_id=command_.values.guild_id)
