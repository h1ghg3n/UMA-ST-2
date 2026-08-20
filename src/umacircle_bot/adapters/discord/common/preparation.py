from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import discord

from umacircle_bot.adapters.discord.common.bridge import run_blocking_application
from umacircle_bot.adapters.discord.common.context import InteractionContext
from umacircle_bot.adapters.discord.common.permissions import (
    COMMAND_ACCESS_MATRIX,
    COMMAND_CHANNEL_SCOPE_MATRIX,
    LEGACY_PLAYER_LINK_COMMAND_NAMES,
    CommandAccess,
    CommandChannelScope,
    GuildChannelSettings,
    GuildRoleAuthorization,
    interaction_access_error,
)
from umacircle_bot.config import Settings
from umacircle_bot.logging_safety import log_sanitized_exception


class GuildCommandSettings(GuildChannelSettings, Protocol):
    operator_role_id: str | None
    bot_manager_role_id: str | None


class SendInitialResponsePort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        content: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class CommandPreparationPorts:
    """Runtime and delivery boundaries required before a Discord command runs."""

    get_runtime_settings: Callable[[], Settings]
    get_guild_settings: Callable[[str], GuildCommandSettings]
    refresh_discord_nickname: Callable[[str, str], bool]
    display_name: Callable[[object], str]
    send_initial_response: SendInitialResponsePort
    correlation_id: Callable[[object], str]
    interaction_user_id: Callable[[object], str]
    logger: logging.Logger


class CommandPreparation:
    def __init__(self, ports: CommandPreparationPorts) -> None:
        self.ports = ports

    async def require_guild_id(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> str | None:
        guild_id = getattr(interaction, "guild_id", None)
        if isinstance(guild_id, bool) or not isinstance(guild_id, int) or guild_id <= 0:
            await self.ports.send_initial_response(
                interaction,
                command_name,
                "이 명령은 Discord 서버에서만 사용할 수 있습니다.",
            )
            return None
        return str(guild_id)

    async def prepare_command(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *,
        defer: bool = True,
    ) -> Settings | None:
        settings = self.ports.get_runtime_settings()
        access = COMMAND_ACCESS_MATRIX[command_name]
        channel_scope = COMMAND_CHANNEL_SCOPE_MATRIX[command_name]
        try:
            authorization = await self.interaction_role_authorization(
                interaction,
                settings=settings,
                access=access,
            )
        except Exception:
            self._log_exception(
                "discord authorization lookup failed correlation_id=%s command=%s actor_id=%s",
                interaction,
                command_name,
            )
            await self.ports.send_initial_response(
                interaction,
                command_name,
                "권한 설정을 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            )
            return None
        try:
            channel_settings = await self.interaction_channel_settings(
                interaction,
                settings=settings,
                channel_scope=channel_scope,
            )
        except Exception:
            self._log_exception(
                "discord channel settings lookup failed correlation_id=%s command=%s actor_id=%s",
                interaction,
                command_name,
            )
            await self.ports.send_initial_response(
                interaction,
                command_name,
                "채널 설정을 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            )
            return None
        error_message = interaction_access_error(
            interaction,
            settings=settings,
            access=access,
            channel_scope=channel_scope,
            channel_settings=channel_settings,
            role_authorization=authorization,
        )
        if error_message is not None:
            self.ports.logger.warning(
                "discord command access denied correlation_id=%s command=%s actor_id=%s guild_id=%s channel_id=%s "
                "required_access=%s",
                self.ports.correlation_id(interaction),
                command_name,
                self.ports.interaction_user_id(interaction),
                getattr(interaction, "guild_id", None),
                getattr(interaction, "channel_id", None),
                access.value,
            )
            await self.ports.send_initial_response(interaction, command_name, error_message)
            return None
        if command_name in LEGACY_PLAYER_LINK_COMMAND_NAMES and not settings.legacy_player_link_enabled:
            self.ports.logger.warning(
                "discord command disabled correlation_id=%s command=%s actor_id=%s guild_id=%s",
                self.ports.correlation_id(interaction),
                command_name,
                self.ports.interaction_user_id(interaction),
                getattr(interaction, "guild_id", None),
            )
            await self.ports.send_initial_response(
                interaction,
                command_name,
                "기존 기록 연결 기능은 레거시 이관 검증이 완료된 뒤에 열립니다.",
            )
            return None
        try:
            user = getattr(interaction, "user", None)
            discord_user_id = str(user.id)
            discord_nickname = self.ports.display_name(user)
            await run_blocking_application(
                lambda: self.ports.refresh_discord_nickname(
                    discord_user_id,
                    discord_nickname,
                )
            )
        except Exception:
            self._log_exception(
                "discord display-name refresh failed correlation_id=%s command=%s actor_id=%s",
                interaction,
                command_name,
            )
        if defer:
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except Exception:
                self._log_exception(
                    "discord interaction defer failed correlation_id=%s command=%s actor_id=%s",
                    interaction,
                    command_name,
                )
                return None
        return settings

    async def prepare_bound_component(
        self,
        interaction: discord.Interaction,
        context: InteractionContext,
        *,
        command_name: str,
        defer: bool = True,
    ) -> Settings | None:
        if (
            getattr(getattr(interaction, "user", None), "id", None) != context.user_id
            or getattr(interaction, "guild_id", None) != context.guild_id
            or getattr(interaction, "channel_id", None) != context.channel_id
        ):
            await self.ports.send_initial_response(
                interaction,
                command_name,
                "이 화면을 연 사용자와 서버·채널에서만 계속할 수 있습니다.",
            )
            return None
        return await self.prepare_command(interaction, command_name, defer=defer)

    @staticmethod
    def interaction_context(interaction: discord.Interaction) -> InteractionContext:
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        guild_id = getattr(interaction, "guild_id", None)
        channel_id = getattr(interaction, "channel_id", None)
        if not all(isinstance(value, int) and value > 0 for value in (user_id, guild_id, channel_id)):
            raise ValueError("설정 화면에 필요한 Discord 요청 정보가 없습니다.")
        return InteractionContext(
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
        )

    async def interaction_channel_settings(
        self,
        interaction: discord.Interaction,
        *,
        settings: Settings,
        channel_scope: CommandChannelScope,
    ) -> GuildCommandSettings | None:
        if channel_scope not in {CommandChannelScope.MATCH, CommandChannelScope.WIN5}:
            return None
        guild_id = getattr(interaction, "guild_id", None)
        if guild_id != settings.discord_guild_id:
            return None
        if not isinstance(guild_id, int) or isinstance(guild_id, bool) or guild_id <= 0:
            return None
        return await run_blocking_application(lambda: self.ports.get_guild_settings(str(guild_id)))

    async def interaction_role_authorization(
        self,
        interaction: discord.Interaction,
        *,
        settings: Settings,
        access: CommandAccess,
    ) -> GuildRoleAuthorization:
        if access is CommandAccess.MEMBER or access is CommandAccess.OWNER:
            return GuildRoleAuthorization(owner_role_id=settings.owner_role_id)
        guild_id = getattr(interaction, "guild_id", None)
        if not isinstance(guild_id, int) or isinstance(guild_id, bool) or guild_id <= 0:
            return GuildRoleAuthorization(owner_role_id=settings.owner_role_id)
        guild_settings = await run_blocking_application(lambda: self.ports.get_guild_settings(str(guild_id)))
        return GuildRoleAuthorization(
            operator_role_id=int(guild_settings.operator_role_id or 0),
            bot_manager_role_id=int(guild_settings.bot_manager_role_id or 0),
            owner_role_id=settings.owner_role_id,
        )

    def _log_exception(
        self,
        message: str,
        interaction: discord.Interaction,
        command_name: str,
    ) -> None:
        log_sanitized_exception(
            self.ports.logger,
            message,
            self.ports.correlation_id(interaction),
            command_name,
            self.ports.interaction_user_id(interaction),
        )
