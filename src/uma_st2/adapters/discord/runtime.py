"""Concrete Discord runtime authorization, preflight, and scheduling."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

import discord
from discord import app_commands

from uma_st2.application.discord import (
    DiscordGuildRuntimeSettings,
    DiscordGuildSettingsQueries,
)

from .common import BlockingApplicationRunner, run_blocking_application
from .localization import KoreanParameterNameTranslator
from .publication_delivery import (
    PublicationDeliveryRun,
)
from .strings.runtime import (
    BOT_MANAGER_ROLE_REQUIRED,
    MISSING_CHANNEL,
    SETTINGS_UNAVAILABLE,
    STAFF_ROLE_REQUIRED,
    UNREGISTERED_COMMAND_BOUNDARY,
    WRONG_GUILD,
)

logger = logging.getLogger(__name__)

_STAFF_COMMAND_PREFIXES = ("staff.", "win5.staff.", "match.staff.")
_SETTINGS_COMMAND = "settings"
_EXPORT_COMMAND_PREFIX = "export."
_WIN5_COMMAND_PREFIX = "win5."
_MATCH_COMMAND_PREFIX = "match."
_ACCOUNT_COMMAND_PREFIX = "account."


def _positive_snowflake(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _interaction_id(interaction: object, attribute: str) -> str:
    value = _positive_snowflake(getattr(interaction, attribute, None))
    return "unavailable" if value is None else str(value)


def _actor_id(interaction: object) -> str:
    value = _positive_snowflake(getattr(getattr(interaction, "user", None), "id", None))
    return "unavailable" if value is None else str(value)


def _member_role_ids(user: object) -> tuple[int, ...]:
    roles = getattr(user, "roles", ())
    return tuple(role_id for role in roles if (role_id := _positive_snowflake(getattr(role, "id", None))) is not None)


class DiscordRuntimePreflightError(RuntimeError):
    """The configured Discord runtime is unsafe to start."""


def validate_discord_runtime_settings(
    settings: DiscordGuildRuntimeSettings,
    *,
    configured_guild_id: int,
) -> None:
    """Validate DB-backed settings needed before exposing the runtime."""

    if settings.guild_id != str(configured_guild_id):
        raise DiscordRuntimePreflightError("Discord guild settings do not match the configured guild.")
    if not settings.staff_role_ids:
        raise DiscordRuntimePreflightError("At least one staff Role ID must be configured.")
    if configured_guild_id in settings.staff_role_ids:
        raise DiscordRuntimePreflightError("A staff Role ID must not use the guild @everyone Role ID.")


@dataclass(frozen=True, slots=True)
class DiscordCommandGate:
    """Revalidate configured-guild and current Role access for every interaction step."""

    settings_queries: DiscordGuildSettingsQueries
    configured_guild_id: int
    run_application: BlockingApplicationRunner = run_blocking_application

    def __post_init__(self) -> None:
        if _positive_snowflake(self.configured_guild_id) is None:
            raise ValueError("configured_guild_id must be a positive Discord snowflake.")

    async def prepare_command(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *,
        ephemeral: bool,
    ) -> bool:
        """Authorize a slash command, then establish its response visibility."""

        error = await self._access_error(interaction, command_name)
        if error is not None:
            await self._send_private_rejection(interaction, command_name, error)
            return False
        try:
            await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        except Exception as exc:
            logger.error(
                "Discord interaction defer failed command=%s actor_id=%s guild_id=%s channel_id=%s error_type=%s",
                command_name,
                _actor_id(interaction),
                _interaction_id(interaction, "guild_id"),
                _interaction_id(interaction, "channel_id"),
                type(exc).__name__,
            )
            return False
        return True

    async def authorize_autocomplete(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> bool:
        """Authorize autocomplete without acknowledging the interaction."""

        error = await self._access_error(interaction, command_name)
        if error is not None:
            self._log_denied(interaction, command_name)
            return False
        return True

    async def authorize_interaction(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> bool:
        """Authorize one Modal/View step without acknowledging success."""

        error = await self._access_error(interaction, command_name)
        if error is not None:
            await self._send_private_rejection(interaction, command_name, error)
            return False
        return True

    async def _access_error(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> str | None:
        guild_id = _positive_snowflake(getattr(interaction, "guild_id", None))
        if guild_id != self.configured_guild_id:
            return WRONG_GUILD
        channel_id = _positive_snowflake(getattr(interaction, "channel_id", None))
        if channel_id is None:
            return MISSING_CHANNEL

        try:
            settings = await self.run_application(
                lambda: self.settings_queries.get_guild_settings(guild_id=str(guild_id))
            )
            validate_discord_runtime_settings(
                settings,
                configured_guild_id=self.configured_guild_id,
            )
        except Exception as exc:
            logger.error(
                "Discord authorization settings lookup failed command=%s actor_id=%s guild_id=%s "
                "channel_id=%s error_type=%s",
                command_name,
                _actor_id(interaction),
                str(guild_id),
                str(channel_id),
                type(exc).__name__,
            )
            return SETTINGS_UNAVAILABLE

        if command_name.startswith(_EXPORT_COMMAND_PREFIX):
            if not self._is_current_bot_manager(interaction, settings):
                return BOT_MANAGER_ROLE_REQUIRED
            return None
        if command_name == _SETTINGS_COMMAND:
            if not self._is_current_staff(interaction, settings):
                return STAFF_ROLE_REQUIRED
            return None
        if command_name.startswith(_STAFF_COMMAND_PREFIXES):
            if not self._is_current_staff(interaction, settings):
                return STAFF_ROLE_REQUIRED
            return None
        if command_name.startswith(_MATCH_COMMAND_PREFIX):
            return None
        if command_name.startswith(_WIN5_COMMAND_PREFIX):
            return None
        if command_name.startswith(_ACCOUNT_COMMAND_PREFIX):
            return None
        return UNREGISTERED_COMMAND_BOUNDARY

    @staticmethod
    def _is_current_staff(
        interaction: discord.Interaction,
        settings: DiscordGuildRuntimeSettings,
    ) -> bool:
        user = getattr(interaction, "user", None)
        user_id = _positive_snowflake(getattr(user, "id", None))
        guild_owner_id = _positive_snowflake(getattr(getattr(interaction, "guild", None), "owner_id", None))
        if user_id is not None and user_id == guild_owner_id:
            return True
        return not set(_member_role_ids(user)).isdisjoint(settings.staff_role_ids)

    @staticmethod
    def _is_current_bot_manager(
        interaction: discord.Interaction,
        settings: DiscordGuildRuntimeSettings,
    ) -> bool:
        user = getattr(interaction, "user", None)
        user_id = _positive_snowflake(getattr(user, "id", None))
        guild_owner_id = _positive_snowflake(getattr(getattr(interaction, "guild", None), "owner_id", None))
        if user_id is not None and user_id == guild_owner_id:
            return True
        if settings.bot_manager_role_id is None:
            return False
        return int(settings.bot_manager_role_id) in _member_role_ids(user)

    async def _send_private_rejection(
        self,
        interaction: discord.Interaction,
        command_name: str,
        content: str,
    ) -> None:
        self._log_denied(interaction, command_name)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    content,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    content,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception as exc:
            logger.error(
                "Discord authorization rejection delivery failed command=%s actor_id=%s error_type=%s",
                command_name,
                _actor_id(interaction),
                type(exc).__name__,
            )

    @staticmethod
    def _log_denied(interaction: discord.Interaction, command_name: str) -> None:
        logger.warning(
            "Discord command access denied command=%s actor_id=%s guild_id=%s channel_id=%s",
            command_name,
            _actor_id(interaction),
            _interaction_id(interaction, "guild_id"),
            _interaction_id(interaction, "channel_id"),
        )


class DiscordRuntimeClient(Protocol):
    """Public discord.py lookup surface needed by startup preflight."""

    def get_guild(self, guild_id: int) -> object | None: ...

    def get_channel(self, channel_id: int) -> object | None: ...

    async def fetch_channel(self, channel_id: int) -> object: ...


@dataclass(frozen=True, slots=True)
class DiscordRuntimePreflight:
    """Verify current guild and configured delivery destinations before startup."""

    client: DiscordRuntimeClient
    settings_queries: DiscordGuildSettingsQueries
    configured_guild_id: int
    run_application: BlockingApplicationRunner = run_blocking_application

    async def run(self) -> None:
        settings = await self.run_application(
            lambda: self.settings_queries.get_guild_settings(guild_id=str(self.configured_guild_id))
        )
        validate_discord_runtime_settings(
            settings,
            configured_guild_id=self.configured_guild_id,
        )
        guild = self.client.get_guild(self.configured_guild_id)
        if guild is None:
            raise DiscordRuntimePreflightError("The configured Discord guild is not available.")
        bot_member = getattr(guild, "me", None)
        if bot_member is None:
            raise DiscordRuntimePreflightError("The Discord bot guild membership is unavailable.")

        channel_ids = tuple(
            dict.fromkeys(
                int(channel_id)
                for channel_id in (
                    settings.win5_announcement_channel_id,
                    settings.match_announcement_channel_id,
                )
                if channel_id is not None
            )
        )
        for channel_id in channel_ids:
            channel = await self._resolve_channel(channel_id)
            channel_guild_id = _positive_snowflake(getattr(getattr(channel, "guild", None), "id", None))
            if channel_guild_id != self.configured_guild_id:
                raise DiscordRuntimePreflightError("A configured Discord channel belongs to another guild.")
            if not callable(getattr(channel, "send", None)):
                raise DiscordRuntimePreflightError("A configured Discord channel is not messageable.")
            permissions_for = getattr(channel, "permissions_for", None)
            if not callable(permissions_for):
                raise DiscordRuntimePreflightError("A configured Discord channel cannot provide permission state.")
            permissions = permissions_for(bot_member)
            if not all(
                bool(getattr(permissions, permission, False))
                for permission in (
                    "view_channel",
                    "send_messages",
                )
            ):
                raise DiscordRuntimePreflightError(
                    "The Discord bot lacks required permissions in a configured channel."
                )

    async def _resolve_channel(self, channel_id: int) -> object:
        channel = self.client.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.client.fetch_channel(channel_id)
        except Exception as exc:
            raise DiscordRuntimePreflightError("A configured Discord channel could not be resolved.") from exc


class DeliveryWorker(Protocol):
    """Bounded asynchronous delivery operation scheduled by the runtime."""

    async def run_once(self) -> PublicationDeliveryRun: ...


@dataclass(slots=True)
class PublicationDeliveryScheduler:
    """Run one non-overlapping delivery pass per configured interval."""

    worker: DeliveryWorker
    poll_interval: timedelta
    _stop_event: asyncio.Event = field(init=False, default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if not isinstance(self.poll_interval, timedelta) or self.poll_interval <= timedelta(0):
            raise ValueError("poll_interval must be a positive duration.")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        if self._stop_event.is_set():
            raise RuntimeError("A stopped publication scheduler cannot be restarted.")
        self._task = asyncio.create_task(
            self._run(),
            name="publication-delivery",
        )

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is not None:
            await task

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = await self.worker.run_once()
                self._log_result(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Publication delivery pass failed error_type=%s",
                    type(exc).__name__,
                )
            if self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.poll_interval.total_seconds(),
                )
            except TimeoutError:
                pass

    @staticmethod
    def _log_result(result: PublicationDeliveryRun) -> None:
        logger.info(
            "Publication delivery pass completed stale_recovered=%s claimed=%s sent=%s failed=%s delivery_unknown=%s",
            result.stale_recovered,
            result.claimed,
            result.sent,
            result.failed,
            result.delivery_unknown,
        )


class RuntimeScheduler(Protocol):
    """Lifecycle surface bound to the concrete Discord client."""

    def start(self) -> None: ...

    async def stop(self) -> None: ...


class UmaSt2DiscordClient(discord.Client):
    """Guild-scoped V2 command client with preflight-gated delivery startup."""

    def __init__(
        self,
        *,
        configured_guild_id: int,
        command_groups: tuple[app_commands.Command | app_commands.Group, ...],
    ) -> None:
        super().__init__(intents=discord.Intents.default())
        if not command_groups:
            raise ValueError("command_groups must contain at least one root command.")
        if len({command.name for command in command_groups}) != len(command_groups):
            raise ValueError("command_groups must contain unique root command names.")
        self.configured_guild = discord.Object(id=configured_guild_id)
        self.tree = app_commands.CommandTree(self)
        self._parameter_name_translator = KoreanParameterNameTranslator()
        for command in command_groups:
            self.tree.add_command(command, guild=self.configured_guild)
        self._preflight: DiscordRuntimePreflight | None = None
        self._scheduler: RuntimeScheduler | None = None
        self._startup_lock = asyncio.Lock()
        self._runtime_started = False
        self._runtime_closed = False
        self._startup_error: Exception | None = None

    @property
    def startup_error(self) -> Exception | None:
        return self._startup_error

    def bind_runtime(
        self,
        *,
        preflight: DiscordRuntimePreflight,
        scheduler: RuntimeScheduler,
    ) -> None:
        if self._preflight is not None or self._scheduler is not None:
            raise RuntimeError("Discord runtime components are already bound.")
        self._preflight = preflight
        self._scheduler = scheduler

    async def setup_hook(self) -> None:
        await self.tree.set_translator(self._parameter_name_translator)
        logger.info(
            "Discord guild command sync started guild_id=%s",
            self.configured_guild.id,
        )
        commands = await self.tree.sync(guild=self.configured_guild)
        logger.info(
            "Discord guild command sync completed guild_id=%s command_count=%s",
            self.configured_guild.id,
            len(commands),
        )

    async def on_ready(self) -> None:
        async with self._startup_lock:
            if self._runtime_started or self._runtime_closed:
                return
            if self._preflight is None or self._scheduler is None:
                self._startup_error = DiscordRuntimePreflightError("Discord runtime components were not bound.")
                await self.close()
                return
            try:
                await self._preflight.run()
            except Exception as exc:
                self._startup_error = exc
                logger.error(
                    "Discord runtime preflight failed guild_id=%s error_type=%s",
                    self.configured_guild.id,
                    type(exc).__name__,
                )
                await self.close()
                return
            self._scheduler.start()
            self._runtime_started = True
            logger.info(
                "Discord runtime ready guild_id=%s user_id=%s",
                self.configured_guild.id,
                getattr(getattr(self, "user", None), "id", "unavailable"),
            )

    async def close(self) -> None:
        if self._runtime_closed:
            return
        self._runtime_closed = True
        try:
            if self._scheduler is not None:
                await self._scheduler.stop()
        finally:
            await super().close()
