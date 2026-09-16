"""SQLite persistence tests for runtime Discord guild settings updates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from uma_st2.application.discord import UpdateDiscordGuildSettings
from uma_st2.compose import (
    compose_discord_guild_settings_update_commands,
    compose_discord_guild_settings_update_queries,
)
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    Base,
    BotGuildSettingORM,
    DiscordPublicationORM,
    OperationORM,
    SettingsOperationORM,
)

NOW = datetime(2026, 9, 1, 3, 30, tzinfo=UTC)
STORED_NOW = NOW.replace(tzinfo=None)
GUILD_ID = "987"


def _runtime() -> DatabaseRuntime:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    return runtime


def _seed(runtime: DatabaseRuntime) -> None:
    with runtime.session_factory.begin() as session:
        session.add(
            BotGuildSettingORM(
                guild_id=GUILD_ID,
                win5_announcement_channel_id="201",
                match_announcement_channel_id="202",
                log_channel_id="203",
                operator_role_id="301",
                bot_manager_role_id="302",
                default_timezone="Asia/Seoul",
                win5_announcements_enabled=True,
                match_announcements_enabled=True,
                match_odds_refresh_mode="live",
                match_odds_refresh_next_at=STORED_NOW + timedelta(minutes=1),
                match_odds_refresh_sequence=17,
                match_odds_last_projection_fingerprint="a" * 64,
                created_at=STORED_NOW - timedelta(days=10),
                updated_at=STORED_NOW,
            )
        )
        session.add(
            DiscordPublicationORM(
                id=91,
                guild_id=GUILD_ID,
                destination_kind="match_announcement",
                event_type="match_betting_opened",
                event_key="existing-settings-test",
                source_kind="match",
                source_id=1,
                target_channel_id="202",
                payload_json={"schema_version": 1},
                payload_fingerprint="b" * 64,
                status="ready",
                attempt_count=0,
                created_at=STORED_NOW,
                updated_at=STORED_NOW,
            )
        )


def _count(runtime: DatabaseRuntime, model: type[object]) -> int:
    with runtime.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _command(runtime: DatabaseRuntime, *, key: str) -> UpdateDiscordGuildSettings:
    state = compose_discord_guild_settings_update_queries(runtime).get_state(guild_id=GUILD_ID)
    return UpdateDiscordGuildSettings(
        guild_id=GUILD_ID,
        desired=state.editable.__class__(
            win5_announcement_channel_id=None,
            match_announcement_channel_id=state.editable.match_announcement_channel_id,
            log_channel_id=state.editable.log_channel_id,
            default_timezone="UTC",
            win5_announcements_enabled=False,
            match_announcements_enabled=state.editable.match_announcements_enabled,
        ),
        reason="운영 설정 변경",
        expected_state_fingerprint=state.state_fingerprint,
        actor_discord_user_id="900",
        idempotency_key=key,
        correlation_id=key,
    )


def test_update_commits_audit_once_and_preserves_noneditable_state() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        command = _command(runtime, key="settings-update-1")

        result = compose_discord_guild_settings_update_commands(runtime).update(command)
        retry = compose_discord_guild_settings_update_commands(runtime).update(command)

        assert retry.operation_id == result.operation_id
        assert retry.exact_retry is True
        with runtime.session_factory() as session:
            setting = session.get(BotGuildSettingORM, GUILD_ID)
            operation = session.get(OperationORM, result.operation_id)
            audit = session.get(SettingsOperationORM, result.operation_id)
            publication = session.scalar(
                select(DiscordPublicationORM).where(DiscordPublicationORM.event_key == "existing-settings-test")
            )
            assert setting is not None
            assert setting.win5_announcement_channel_id is None
            assert setting.default_timezone == "UTC"
            assert setting.win5_announcements_enabled is False
            assert (setting.operator_role_id, setting.bot_manager_role_id) == ("301", "302")
            assert setting.match_odds_refresh_mode == "live"
            assert setting.match_odds_refresh_sequence == 17
            assert setting.match_odds_last_projection_fingerprint == "a" * 64
            assert setting.created_at == STORED_NOW - timedelta(days=10)
            assert operation is not None and operation.actor_discord_user_id == "900"
            assert audit is not None
            assert audit.before_data["schema_version"] == 1
            assert audit.after_data["schema_version"] == 1
            assert audit.after_data["changed_fields"] == [
                "win5_announcement_channel_id",
                "default_timezone",
                "win5_announcements_enabled",
            ]
            assert publication is not None
            assert (publication.target_channel_id, publication.status) == ("202", "ready")
        assert _count(runtime, OperationORM) == 1
        assert _count(runtime, SettingsOperationORM) == 1
        assert _count(runtime, DiscordPublicationORM) == 1
    finally:
        runtime.dispose()


def test_settings_audit_failure_rolls_back_row_and_generic_operation() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        command = _command(runtime, key="settings-update-failure")

        def fail_audit(session: Session, _flush_context: object, _instances: object) -> None:
            if any(isinstance(item, SettingsOperationORM) for item in session.new):
                raise RuntimeError("simulated settings audit failure")

        event.listen(Session, "before_flush", fail_audit)
        try:
            with pytest.raises(RuntimeError, match="simulated settings audit failure"):
                compose_discord_guild_settings_update_commands(runtime).update(command)
        finally:
            event.remove(Session, "before_flush", fail_audit)

        with runtime.session_factory() as session:
            setting = session.get(BotGuildSettingORM, GUILD_ID)
            assert setting is not None
            assert setting.win5_announcement_channel_id == "201"
            assert setting.default_timezone == "Asia/Seoul"
            assert setting.win5_announcements_enabled is True
        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, SettingsOperationORM) == 0
    finally:
        runtime.dispose()
