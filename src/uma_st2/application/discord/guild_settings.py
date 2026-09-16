"""Read-only guild settings required by the Discord runtime boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork

_MAX_DISCORD_SNOWFLAKE = 2**64 - 1


class DiscordGuildSettingsQueryError(ValueError):
    """Base error for a rejected Discord runtime settings query."""


class DiscordGuildSettingsUnavailableError(DiscordGuildSettingsQueryError):
    """The configured guild has no canonical runtime settings row."""


class DiscordGuildSettingsInvalidSourceError(DiscordGuildSettingsQueryError):
    """Stored guild settings cannot form a safe runtime authorization snapshot."""


def _require_snowflake(
    value: str | None,
    *,
    field_name: str,
    optional: bool = False,
) -> None:
    if value is None and optional:
        return
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or not 1 <= int(value) <= _MAX_DISCORD_SNOWFLAKE
    ):
        qualifier = "optional " if optional else ""
        raise ValueError(f"{field_name} must be an {qualifier}positive decimal Discord snowflake.")


@dataclass(frozen=True, slots=True)
class DiscordGuildRuntimeSettings:
    """Closed-session channel and Role snapshot for one configured guild."""

    guild_id: str
    win5_announcement_channel_id: str | None
    match_announcement_channel_id: str | None
    operator_role_id: str | None
    bot_manager_role_id: str | None
    win5_announcements_enabled: bool

    def __post_init__(self) -> None:
        _require_snowflake(self.guild_id, field_name="guild_id")
        _require_snowflake(
            self.win5_announcement_channel_id,
            field_name="win5_announcement_channel_id",
            optional=True,
        )
        _require_snowflake(
            self.match_announcement_channel_id,
            field_name="match_announcement_channel_id",
            optional=True,
        )
        _require_snowflake(
            self.operator_role_id,
            field_name="operator_role_id",
            optional=True,
        )
        _require_snowflake(
            self.bot_manager_role_id,
            field_name="bot_manager_role_id",
            optional=True,
        )
        if not isinstance(self.win5_announcements_enabled, bool):
            raise ValueError("win5_announcements_enabled must be a boolean.")

    @property
    def staff_role_ids(self) -> tuple[int, ...]:
        """Return the configured non-null staff Role IDs."""

        return tuple(
            int(role_id) for role_id in (self.operator_role_id, self.bot_manager_role_id) if role_id is not None
        )


class DiscordGuildSettingsQueryRepository(Protocol):
    """Read one guild settings snapshot from persistence."""

    def get_guild_settings(
        self,
        *,
        guild_id: str,
    ) -> DiscordGuildRuntimeSettings | None: ...


class DiscordGuildSettingsQueryUnitOfWork(UnitOfWork, Protocol):
    """Read-only UoW exposing Discord runtime guild settings."""

    @property
    def discord_guild_settings(self) -> DiscordGuildSettingsQueryRepository: ...


@dataclass(frozen=True, slots=True)
class DiscordGuildSettingsQueries:
    """Application entry point for fresh Discord runtime settings snapshots."""

    query_runner: QueryRunner[DiscordGuildSettingsQueryUnitOfWork]

    def get_guild_settings(self, *, guild_id: str) -> DiscordGuildRuntimeSettings:
        _require_snowflake(guild_id, field_name="guild_id")

        def query(unit_of_work: DiscordGuildSettingsQueryUnitOfWork) -> DiscordGuildRuntimeSettings:
            try:
                settings = unit_of_work.discord_guild_settings.get_guild_settings(guild_id=guild_id)
                if settings is None:
                    raise DiscordGuildSettingsUnavailableError("Discord guild runtime settings do not exist.")
                if settings.guild_id != guild_id:
                    raise DiscordGuildSettingsInvalidSourceError(
                        "Discord guild runtime settings identity does not match the query."
                    )
                return settings
            except DiscordGuildSettingsQueryError:
                raise
            except (TypeError, ValueError) as exc:
                raise DiscordGuildSettingsInvalidSourceError(
                    "Stored Discord guild runtime settings are malformed."
                ) from exc

        return self.query_runner.run(query)
