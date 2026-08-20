from dataclasses import dataclass
from enum import StrEnum

from umacircle_bot.domain.errors import GuildDiscordSettingsError


class GuildDefaultTimezone(StrEnum):
    KST = "KST"
    UTC = "UTC"


@dataclass(frozen=True, slots=True)
class GuildDiscordSettingsValues:
    win5_announcement_channel_id: str | None = None
    room_match_announcement_channel_id: str | None = None
    log_channel_id: str | None = None
    operator_role_id: str | None = None
    bot_manager_role_id: str | None = None
    default_timezone: str = GuildDefaultTimezone.KST.value
    win5_announcements_enabled: bool = True
    room_match_announcements_enabled: bool = True


DEFAULT_GUILD_DISCORD_SETTINGS = GuildDiscordSettingsValues()


def normalize_guild_discord_settings(
    values: GuildDiscordSettingsValues,
) -> GuildDiscordSettingsValues:
    if not isinstance(values, GuildDiscordSettingsValues):
        raise GuildDiscordSettingsError("guild Discord settings values are invalid")
    normalized = GuildDiscordSettingsValues(
        win5_announcement_channel_id=_optional_snowflake(
            values.win5_announcement_channel_id,
            field="WIN5 announcement channel ID",
        ),
        room_match_announcement_channel_id=_optional_snowflake(
            values.room_match_announcement_channel_id,
            field="Room Match announcement channel ID",
        ),
        log_channel_id=_optional_snowflake(values.log_channel_id, field="log channel ID"),
        operator_role_id=_optional_snowflake(values.operator_role_id, field="운영자 역할 ID"),
        bot_manager_role_id=_optional_snowflake(values.bot_manager_role_id, field="봇 관리 역할 ID"),
        default_timezone=_timezone(values.default_timezone),
        win5_announcements_enabled=_boolean(
            values.win5_announcements_enabled,
            field="WIN5 announcements enabled",
        ),
        room_match_announcements_enabled=_boolean(
            values.room_match_announcements_enabled,
            field="Room Match announcements enabled",
        ),
    )
    role_ids = [
        role_id
        for role_id in (
            normalized.operator_role_id,
            normalized.bot_manager_role_id,
        )
        if role_id is not None
    ]
    if len(role_ids) != len(set(role_ids)):
        raise GuildDiscordSettingsError("설정된 역할 ID는 서로 달라야 합니다")
    return normalized


def normalize_discord_snowflake(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise GuildDiscordSettingsError(f"{field} must be text")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 32
        or not normalized.isascii()
        or not normalized.isdigit()
        or int(normalized) <= 0
    ):
        raise GuildDiscordSettingsError(f"{field} must be a positive decimal Discord snowflake")
    return normalized


def _optional_snowflake(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    return normalize_discord_snowflake(value, field=field)


def _timezone(value: str) -> str:
    if isinstance(value, GuildDefaultTimezone):
        return value.value
    if not isinstance(value, str):
        raise GuildDiscordSettingsError("default timezone must be KST or UTC")
    normalized = value.strip().upper()
    try:
        return GuildDefaultTimezone(normalized).value
    except ValueError as exc:
        raise GuildDiscordSettingsError("default timezone must be KST or UTC") from exc


def _boolean(value: bool, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise GuildDiscordSettingsError(f"{field} must be boolean")
    return value
