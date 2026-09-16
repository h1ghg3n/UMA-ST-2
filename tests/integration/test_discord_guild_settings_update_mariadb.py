"""MariaDB serialization and audit evidence for runtime Discord settings updates."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from uma_st2.application.discord import (
    DISCORD_GUILD_SETTINGS_UPDATE_OPERATION_TYPE,
    DiscordGuildEditableSettings,
    DiscordGuildSettingsUpdateStaleError,
    UpdatedDiscordGuildSettings,
    UpdateDiscordGuildSettings,
)
from uma_st2.compose import (
    compose_discord_guild_settings_update_commands,
    compose_discord_guild_settings_update_queries,
)
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyDiscordGuildSettingsUpdateRepository,
)
from uma_st2.infrastructure.database.orm import (
    BotGuildSettingORM,
    DiscordPublicationORM,
    OperationORM,
    SettingsOperationORM,
)

pytestmark = pytest.mark.integration


def _snowflake(prefix: int, suffix: int) -> str:
    return str(prefix * 10**18 + suffix % 10**18)


def _seed(engine: Engine, *, suffix: int) -> tuple[str, str]:
    guild_id = _snowflake(1, suffix)
    event_key = f"existing-settings-{suffix}"
    now = datetime(2026, 9, 1, 4, 0)
    with engine.begin() as connection:
        connection.execute(
            BotGuildSettingORM.__table__.insert().values(
                guild_id=guild_id,
                win5_announcement_channel_id=_snowflake(2, suffix),
                match_announcement_channel_id=_snowflake(3, suffix),
                log_channel_id=_snowflake(4, suffix),
                operator_role_id=_snowflake(5, suffix),
                bot_manager_role_id=_snowflake(6, suffix),
                default_timezone="Asia/Seoul",
                win5_announcements_enabled=True,
                match_announcements_enabled=True,
                match_odds_refresh_mode="live",
                match_odds_refresh_next_at=now,
                match_odds_refresh_sequence=19,
                match_odds_last_projection_fingerprint="a" * 64,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            DiscordPublicationORM.__table__.insert().values(
                guild_id=guild_id,
                destination_kind="match_announcement",
                event_type="match_betting_opened",
                event_key=event_key,
                source_kind="match",
                source_id=1,
                target_channel_id=_snowflake(3, suffix),
                payload_json={"schema_version": 1},
                payload_fingerprint="b" * 64,
                status="ready",
                attempt_count=0,
                created_at=now,
                updated_at=now,
            )
        )
    return guild_id, event_key


def _cleanup(engine: Engine, *, guild_id: str, event_key: str) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(connection.scalars(select(OperationORM.id).where(OperationORM.guild_id == guild_id)))
        if operation_ids:
            connection.execute(delete(SettingsOperationORM).where(SettingsOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(DiscordPublicationORM).where(DiscordPublicationORM.event_key == event_key))
        connection.execute(delete(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id))


def _command(
    runtime: DatabaseRuntime,
    *,
    guild_id: str,
    suffix: int,
    key: str,
    timezone: str = "UTC",
) -> UpdateDiscordGuildSettings:
    state = compose_discord_guild_settings_update_queries(runtime).get_state(guild_id=guild_id)
    return UpdateDiscordGuildSettings(
        guild_id=guild_id,
        desired=DiscordGuildEditableSettings(
            win5_announcement_channel_id=None,
            match_announcement_channel_id=state.editable.match_announcement_channel_id,
            log_channel_id=state.editable.log_channel_id,
            default_timezone=timezone,
            win5_announcements_enabled=False,
            match_announcements_enabled=state.editable.match_announcements_enabled,
        ),
        reason="MariaDB 운영 설정 변경",
        expected_state_fingerprint=state.state_fingerprint,
        actor_discord_user_id=_snowflake(7, suffix),
        idempotency_key=key,
        correlation_id=key,
    )


def test_update_commits_once_and_preserves_roles_cursor_and_publication(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().int % 10**18
    guild_id, event_key = _seed(migrated_engine, suffix=suffix)
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    command = _command(
        runtime,
        guild_id=guild_id,
        suffix=suffix,
        key=f"settings-update-{suffix}",
    )
    try:
        with migrated_engine.begin() as connection:
            connection.execute(
                update(BotGuildSettingORM)
                .where(BotGuildSettingORM.guild_id == guild_id)
                .values(
                    match_odds_refresh_sequence=20,
                    match_odds_last_projection_fingerprint="c" * 64,
                    updated_at=datetime(2026, 9, 1, 4, 1),
                )
            )
        result = compose_discord_guild_settings_update_commands(runtime).update(command)
        retry = compose_discord_guild_settings_update_commands(runtime).update(command)

        assert retry.operation_id == result.operation_id
        assert retry.exact_retry is True
        with Session(migrated_engine) as session:
            setting = session.scalar(select(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id))
            audit = session.scalar(
                select(SettingsOperationORM).where(SettingsOperationORM.operation_id == result.operation_id)
            )
            publication = session.scalar(
                select(DiscordPublicationORM).where(DiscordPublicationORM.event_key == event_key)
            )
            assert setting is not None
            assert audit is not None
            assert publication is not None
            assert setting.win5_announcement_channel_id is None
            assert setting.default_timezone == "UTC"
            assert setting.win5_announcements_enabled is False
            assert setting.operator_role_id == _snowflake(5, suffix)
            assert setting.bot_manager_role_id == _snowflake(6, suffix)
            assert setting.match_odds_refresh_mode == "live"
            assert setting.match_odds_refresh_sequence == 20
            assert setting.match_odds_last_projection_fingerprint == "c" * 64
            assert audit.type == DISCORD_GUILD_SETTINGS_UPDATE_OPERATION_TYPE
            assert audit.before_data["schema_version"] == 1
            assert audit.after_data["schema_version"] == 1
            assert publication.target_channel_id == _snowflake(3, suffix)
            assert publication.status == "ready"
            assert (
                session.scalar(select(func.count()).select_from(OperationORM).where(OperationORM.guild_id == guild_id))
                == 1
            )
    finally:
        runtime.dispose()
        _cleanup(migrated_engine, guild_id=guild_id, event_key=event_key)


def test_audit_failure_rolls_back_settings_and_operation(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().int % 10**18
    guild_id, event_key = _seed(migrated_engine, suffix=suffix)
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    command = _command(
        runtime,
        guild_id=guild_id,
        suffix=suffix,
        key=f"settings-failure-{suffix}",
    )

    def fail_audit(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("simulated settings audit failure")

    monkeypatch.setattr(SqlAlchemyDiscordGuildSettingsUpdateRepository, "add_audit", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="simulated settings audit failure"):
            compose_discord_guild_settings_update_commands(runtime).update(command)
        with Session(migrated_engine) as session:
            setting = session.scalar(select(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id))
            assert setting is not None
            assert setting.win5_announcement_channel_id == _snowflake(2, suffix)
            assert setting.default_timezone == "Asia/Seoul"
            assert setting.win5_announcements_enabled is True
            assert (
                session.scalar(select(func.count()).select_from(OperationORM).where(OperationORM.guild_id == guild_id))
                == 0
            )
    finally:
        runtime.dispose()
        _cleanup(migrated_engine, guild_id=guild_id, event_key=event_key)


def test_concurrent_same_preview_updates_serialize_to_one_winner(migrated_engine: Engine) -> None:
    suffix = uuid4().int % 10**18
    guild_id, event_key = _seed(migrated_engine, suffix=suffix)
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    original = compose_discord_guild_settings_update_queries(runtime).get_state(guild_id=guild_id)
    barrier = Barrier(2)

    def update(timezone: str, key: str) -> object:
        commands = compose_discord_guild_settings_update_commands(runtime)
        command = UpdateDiscordGuildSettings(
            guild_id=guild_id,
            desired=DiscordGuildEditableSettings(
                win5_announcement_channel_id=original.editable.win5_announcement_channel_id,
                match_announcement_channel_id=original.editable.match_announcement_channel_id,
                log_channel_id=original.editable.log_channel_id,
                default_timezone=timezone,
                win5_announcements_enabled=original.editable.win5_announcements_enabled,
                match_announcements_enabled=original.editable.match_announcements_enabled,
            ),
            reason="동시 설정 변경",
            expected_state_fingerprint=original.state_fingerprint,
            actor_discord_user_id=_snowflake(7, suffix),
            idempotency_key=key,
        )
        barrier.wait()
        try:
            return commands.update(command)
        except DiscordGuildSettingsUpdateStaleError as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures: tuple[Future[object], ...] = (
                executor.submit(update, "UTC", f"settings-concurrent-a-{suffix}"),
                executor.submit(update, "Asia/Tokyo", f"settings-concurrent-b-{suffix}"),
            )
            outcomes = tuple(future.result(timeout=20) for future in futures)

        assert sum(isinstance(outcome, UpdatedDiscordGuildSettings) for outcome in outcomes) == 1
        assert sum(isinstance(outcome, DiscordGuildSettingsUpdateStaleError) for outcome in outcomes) == 1
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count()).select_from(OperationORM).where(OperationORM.guild_id == guild_id)
                )
                == 1
            )
    finally:
        runtime.dispose()
        _cleanup(migrated_engine, guild_id=guild_id, event_key=event_key)
