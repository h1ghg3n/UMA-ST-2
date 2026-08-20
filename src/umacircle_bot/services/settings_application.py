from __future__ import annotations

from umacircle_bot.services.application import run_application_command, run_application_query
from umacircle_bot.services.guild_discord_settings import (
    GuildDiscordSettingsDTO,
    GuildDiscordSettingsMutationDTO,
    UpdateGuildDiscordSettingsCommand,
    get_guild_discord_settings,
    update_guild_discord_settings,
)


def query_guild_discord_settings(*, guild_id: str) -> GuildDiscordSettingsDTO:
    """Return one bounded settings DTO through the read-only application boundary."""

    return run_application_query(
        lambda session: get_guild_discord_settings(
            session,
            guild_id=guild_id,
        )
    )


def execute_guild_discord_settings_update(
    command: UpdateGuildDiscordSettingsCommand,
) -> GuildDiscordSettingsMutationDTO:
    """Execute one settings mutation through the application-owned transaction."""

    return run_application_command(
        lambda session: update_guild_discord_settings(
            session,
            command=command,
        )
    )
