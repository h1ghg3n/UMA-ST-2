"""Tests for the DB-backed Discord runtime settings query."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from uma_st2.application.discord import (
    DiscordGuildRuntimeSettings,
    DiscordGuildSettingsInvalidSourceError,
    DiscordGuildSettingsQueries,
    DiscordGuildSettingsUnavailableError,
)
from uma_st2.application.execution import QueryRunner
from uma_st2.infrastructure.database import (
    Base,
    SqlAlchemyDiscordGuildSettingsQueryUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import BotGuildSettingORM


def _queries(
    session_factory: sessionmaker[Session],
) -> DiscordGuildSettingsQueries:
    return DiscordGuildSettingsQueries(
        QueryRunner(SqlAlchemyDiscordGuildSettingsQueryUnitOfWorkFactory(session_factory))
    )


def test_discord_guild_settings_query_returns_closed_session_dto() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    now = datetime(2026, 8, 27, 0, 0)
    with session_factory.begin() as session:
        session.add(
            BotGuildSettingORM(
                guild_id="987654321",
                win5_announcement_channel_id="111111111",
                match_announcement_channel_id=None,
                log_channel_id=None,
                operator_role_id="222222222",
                bot_manager_role_id="333333333",
                default_timezone="Asia/Seoul",
                win5_announcements_enabled=True,
                match_announcements_enabled=False,
                created_at=now,
                updated_at=now,
            )
        )

    result = _queries(session_factory).get_guild_settings(guild_id="987654321")

    assert result == DiscordGuildRuntimeSettings(
        guild_id="987654321",
        win5_announcement_channel_id="111111111",
        match_announcement_channel_id=None,
        operator_role_id="222222222",
        bot_manager_role_id="333333333",
        win5_announcements_enabled=True,
    )
    assert result.staff_role_ids == (222222222, 333333333)
    engine.dispose()


def test_discord_guild_settings_query_rejects_missing_row() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    with pytest.raises(DiscordGuildSettingsUnavailableError):
        _queries(session_factory).get_guild_settings(guild_id="987654321")
    engine.dispose()


def test_discord_guild_settings_query_wraps_malformed_stored_snowflake() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    now = datetime(2026, 8, 27, 0, 0)
    with session_factory.begin() as session:
        session.add(
            BotGuildSettingORM(
                guild_id="987654321",
                win5_announcement_channel_id="not-a-snowflake",
                match_announcement_channel_id=None,
                log_channel_id=None,
                operator_role_id="222222222",
                bot_manager_role_id=None,
                default_timezone="Asia/Seoul",
                win5_announcements_enabled=True,
                match_announcements_enabled=False,
                created_at=now,
                updated_at=now,
            )
        )

    with pytest.raises(DiscordGuildSettingsInvalidSourceError):
        _queries(session_factory).get_guild_settings(guild_id="987654321")
    engine.dispose()
