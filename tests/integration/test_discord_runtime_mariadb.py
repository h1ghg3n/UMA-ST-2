"""MariaDB composition evidence for the concrete V2 Discord runtime."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from uma_st2.compose import compose_discord_runtime
from uma_st2.config import RuntimeSettings
from uma_st2.infrastructure.database.orm import BotGuildSettingORM

pytestmark = pytest.mark.integration


def test_discord_runtime_composes_guild_commands_and_delivery_from_mariadb(
    migrated_engine: Engine,
) -> None:
    database_url = migrated_engine.url.render_as_string(hide_password=False)
    suffix = uuid.uuid4().int
    guild_id = 1_000_000_000_000_000_000 + suffix % 1_000_000_000_000_000_000
    win5_channel_id = 2_000_000_000_000_000_000 + suffix % 1_000_000_000_000_000_000
    operator_role_id = 4_000_000_000_000_000_000 + suffix % 1_000_000_000_000_000_000
    now = datetime(2026, 8, 27, 0, 0)
    with Session(migrated_engine) as session, session.begin():
        session.add(
            BotGuildSettingORM(
                guild_id=str(guild_id),
                win5_announcement_channel_id=str(win5_channel_id),
                match_announcement_channel_id=None,
                log_channel_id=None,
                operator_role_id=str(operator_role_id),
                bot_manager_role_id=None,
                default_timezone="Asia/Seoul",
                win5_announcements_enabled=True,
                match_announcements_enabled=False,
                created_at=now,
                updated_at=now,
            )
        )
    settings = RuntimeSettings(
        _env_file=None,
        database_url=database_url,
        discord_token_file=Path("/run/secrets/discord_token"),
        discord_guild_id=guild_id,
        win5_delivery_poll_interval_seconds=10,
        win5_delivery_batch_size=10,
        win5_delivery_max_attempts=3,
        win5_delivery_retry_delay_seconds=300,
        win5_delivery_pending_timeout_seconds=900,
    )

    runtime = compose_discord_runtime(settings)
    try:
        commands = runtime.client.tree.get_commands(guild=runtime.client.configured_guild)
        assert [command.name for command in commands] == [
            "account",
            "staff",
            "settings",
            "win5",
            "match",
            "export",
        ]
        assert runtime.client.startup_error is None
    finally:
        runtime.dispose()
