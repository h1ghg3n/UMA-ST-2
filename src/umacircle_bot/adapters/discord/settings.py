from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

import discord
from discord import app_commands

from umacircle_bot.adapters.discord.common import InteractionContext, run_blocking_application
from umacircle_bot.domain.errors import DomainError
from umacircle_bot.domain.guild_discord_settings import GuildDiscordSettingsValues
from umacircle_bot.services.guild_discord_settings import (
    GuildDiscordSettingsDTO,
    GuildDiscordSettingsMutationDTO,
)

logger = logging.getLogger(__name__)


class RuntimeSettings(Protocol):
    owner_role_id: int


class PrepareCommandPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *,
        defer: bool = True,
    ) -> RuntimeSettings | None: ...


class PrepareComponentPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        context: InteractionContext,
        *,
        command_name: str,
        defer: bool = True,
    ) -> RuntimeSettings | None: ...


class SendUserErrorPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        prefix: str,
        error: Exception,
    ) -> None: ...


class SendInternalErrorPort(Protocol):
    async def __call__(self, interaction: discord.Interaction, command_name: str) -> None: ...


class SendFollowupPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        content: str,
        *,
        response_kind: str = "success",
        view: discord.ui.View | None = None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class SettingsAdapterPorts:
    """Common Discord and application ports required by the Settings adapter."""

    prepare_command: PrepareCommandPort
    prepare_component: PrepareComponentPort
    get_guild_settings: Callable[[str], GuildDiscordSettingsDTO]
    update_guild_settings: Callable[
        [GuildDiscordSettingsDTO, GuildDiscordSettingsValues, str, str, str],
        GuildDiscordSettingsMutationDTO,
    ]
    build_context: Callable[[discord.Interaction], InteractionContext]
    send_user_error: SendUserErrorPort
    send_internal_error: SendInternalErrorPort
    send_followup: SendFollowupPort
    correlation_id: Callable[[object], str]
    bounded_message: Callable[[list[str]], str]
    safe_text: Callable[[str], str]


class SettingsAdapter:
    def __init__(self, ports: SettingsAdapterPorts) -> None:
        self.ports = ports

    def create_command_group(self) -> SettingsCommandGroup:
        return SettingsCommandGroup(adapter=self)

    async def show_general(self, interaction: discord.Interaction) -> None:
        command_name = "settings.general"
        if await self.ports.prepare_command(interaction, command_name) is None:
            return
        try:
            settings = await run_blocking_application(lambda: self.ports.get_guild_settings(str(interaction.guild_id)))
            context = self.ports.build_context(interaction)
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "설정을 불러오지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_followup(
            interaction,
            command_name,
            self.format_settings_panel(settings),
            view=SettingsGeneralPanelView(adapter=self, settings=settings, context=context),
        )

    async def show_role(self, interaction: discord.Interaction) -> None:
        command_name = "settings.role"
        runtime_settings = await self.ports.prepare_command(interaction, command_name)
        if runtime_settings is None:
            return
        try:
            settings = await run_blocking_application(lambda: self.ports.get_guild_settings(str(interaction.guild_id)))
            context = self.ports.build_context(interaction)
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "역할 설정을 불러오지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_followup(
            interaction,
            command_name,
            self.format_role_settings_panel(
                settings,
                owner_role_id=runtime_settings.owner_role_id,
            ),
            view=SettingsRolePanelView(adapter=self, settings=settings, context=context),
        )

    async def send_preview(
        self,
        interaction: discord.Interaction,
        *,
        settings: GuildDiscordSettingsDTO,
        proposed: GuildDiscordSettingsValues,
        reason: str,
        context: InteractionContext,
        command_name: str,
    ) -> None:
        await self.ports.send_followup(
            interaction,
            command_name,
            self.format_settings_preview(settings, proposed),
            view=SettingsConfirmView(
                adapter=self,
                settings=settings,
                proposed=proposed,
                reason=reason,
                context=context,
                command_name=command_name,
            ),
        )

    @staticmethod
    def settings_values(settings: GuildDiscordSettingsDTO) -> GuildDiscordSettingsValues:
        return GuildDiscordSettingsValues(
            win5_announcement_channel_id=settings.win5_announcement_channel_id,
            room_match_announcement_channel_id=settings.room_match_announcement_channel_id,
            log_channel_id=settings.log_channel_id,
            operator_role_id=settings.operator_role_id,
            bot_manager_role_id=settings.bot_manager_role_id,
            default_timezone=settings.default_timezone,
            win5_announcements_enabled=settings.win5_announcements_enabled,
            room_match_announcements_enabled=settings.room_match_announcements_enabled,
        )

    def format_settings_panel(self, settings: GuildDiscordSettingsDTO) -> str:
        return self.ports.bounded_message(
            [
                "서버 운영 설정",
                f"설정 버전: {settings.revision_number}",
                f"기본 시간대: {settings.default_timezone}",
                f"WIN5 알림: {_enabled_label(settings.win5_announcements_enabled)}",
                f"WIN5 이용·알림 채널: {_configured_label(settings.win5_announcement_channel_id)}",
                f"룸매치 알림: {_enabled_label(settings.room_match_announcements_enabled)}",
                f"룸매치 베팅·알림 채널: {_configured_label(settings.room_match_announcement_channel_id)}",
                f"운영 로그 채널: {_configured_label(settings.log_channel_id)}",
                "",
                "아래 메뉴에서 변경할 항목을 선택해 주세요.",
            ]
        )

    def format_role_settings_panel(
        self,
        settings: GuildDiscordSettingsDTO,
        *,
        owner_role_id: int,
    ) -> str:
        return self.ports.bounded_message(
            [
                "서버 운영 역할 설정",
                f"설정 버전: {settings.revision_number}",
                f"소유자 역할 (환경변수): {_role_mention(str(owner_role_id) if owner_role_id else None)}",
                f"운영자 역할: {_role_mention(settings.operator_role_id)}",
                f"봇 관리 역할: {_role_mention(settings.bot_manager_role_id)}",
                "",
                "아래 메뉴에서 변경할 역할을 선택해 주세요.",
            ]
        )

    def format_settings_preview(
        self,
        current: GuildDiscordSettingsDTO,
        proposed: GuildDiscordSettingsValues,
    ) -> str:
        labels = {
            "win5_announcement_channel_id": "WIN5 이용·알림 채널",
            "room_match_announcement_channel_id": "룸매치 베팅·알림 채널",
            "log_channel_id": "운영 로그 채널",
            "operator_role_id": "운영자 역할",
            "bot_manager_role_id": "봇 관리 역할",
            "default_timezone": "기본 시간대",
            "win5_announcements_enabled": "WIN5 알림",
            "room_match_announcements_enabled": "룸매치 알림",
        }
        current_values = self.settings_values(current)
        lines = ["설정 변경 미리보기"]
        for field_name, label in labels.items():
            before = getattr(current_values, field_name)
            after = getattr(proposed, field_name)
            if before == after:
                continue
            lines.append(
                f"{label}: {self.settings_preview_value(field_name, before)} "
                f"→ {self.settings_preview_value(field_name, after)}"
            )
        if len(lines) == 1:
            lines.append("실제 변경되는 값이 없습니다.")
        lines.append("저장하면 현재 설정 버전을 기준으로 적용됩니다.")
        return self.ports.bounded_message(lines)

    def settings_preview_value(self, field_name: str, value: object) -> str:
        if isinstance(value, bool):
            return _enabled_label(value)
        if value is None:
            return "미설정"
        if isinstance(value, str) and value.isascii() and value.isdigit():
            if field_name.endswith("role_id"):
                return _role_mention(value)
            return f"<#{value}>"
        return self.ports.safe_text(str(value))


class SettingsChannelSelect(discord.ui.ChannelSelect):
    def __init__(
        self,
        *,
        adapter: SettingsAdapter,
        settings: GuildDiscordSettingsDTO,
        context: InteractionContext,
        field_name: str,
        placeholder: str,
    ) -> None:
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder=placeholder,
            min_values=1,
            max_values=1,
        )
        self._adapter = adapter
        self._settings = settings
        self._context = context
        self._field_name = field_name

    async def callback(self, interaction: discord.Interaction) -> None:
        if (
            await self._adapter.ports.prepare_component(
                interaction,
                self._context,
                command_name="settings.general",
                defer=False,
            )
            is None
        ):
            return
        channel = self.values[0]
        try:
            await interaction.response.send_modal(
                SettingsChannelReasonModal(
                    adapter=self._adapter,
                    settings=self._settings,
                    context=self._context,
                    field_name=self._field_name,
                    channel_id=str(channel.id),
                )
            )
        except Exception:
            await self._adapter.ports.send_internal_error(interaction, "settings.general")


class SettingsGeneralPanelView(discord.ui.View):
    def __init__(
        self,
        *,
        adapter: SettingsAdapter,
        settings: GuildDiscordSettingsDTO,
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._settings = settings
        self._context = context
        self.add_item(
            SettingsChannelSelect(
                adapter=adapter,
                settings=settings,
                context=context,
                field_name="win5_announcement_channel_id",
                placeholder="WIN5 이용·알림 채널 선택",
            )
        )
        self.add_item(
            SettingsChannelSelect(
                adapter=adapter,
                settings=settings,
                context=context,
                field_name="room_match_announcement_channel_id",
                placeholder="룸매치 베팅·알림 채널 선택",
            )
        )
        self.add_item(
            SettingsChannelSelect(
                adapter=adapter,
                settings=settings,
                context=context,
                field_name="log_channel_id",
                placeholder="운영 로그 채널 선택",
            )
        )

    @discord.ui.button(label="일반 설정 변경", style=discord.ButtonStyle.secondary)
    async def general_settings(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if (
            await self._adapter.ports.prepare_component(
                interaction,
                self._context,
                command_name="settings.general",
                defer=False,
            )
            is None
        ):
            return
        try:
            await interaction.response.send_modal(
                GeneralSettingsModal(
                    adapter=self._adapter,
                    settings=self._settings,
                    context=self._context,
                )
            )
        except Exception:
            await self._adapter.ports.send_internal_error(interaction, "settings.general")


class SettingsChannelReasonModal(discord.ui.Modal, title="설정 변경 사유"):
    def __init__(
        self,
        *,
        adapter: SettingsAdapter,
        settings: GuildDiscordSettingsDTO,
        context: InteractionContext,
        field_name: str,
        channel_id: str,
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._settings = settings
        self._context = context
        self._field_name = field_name
        self._channel_id = channel_id
        self.reason = discord.ui.TextInput(
            label="변경 사유",
            style=discord.TextStyle.paragraph,
            required=True,
            min_length=1,
            max_length=255,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            await self._adapter.ports.prepare_component(
                interaction,
                self._context,
                command_name="settings.general",
            )
            is None
        ):
            return
        proposed = replace(
            self._adapter.settings_values(self._settings),
            **{self._field_name: self._channel_id},
        )
        await self._adapter.send_preview(
            interaction,
            settings=self._settings,
            proposed=proposed,
            reason=str(self.reason.value),
            context=self._context,
            command_name="settings.general",
        )


class GeneralSettingsModal(discord.ui.Modal, title="일반 설정 변경"):
    def __init__(
        self,
        *,
        adapter: SettingsAdapter,
        settings: GuildDiscordSettingsDTO,
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._settings = settings
        self._context = context
        self.timezone_name = discord.ui.TextInput(
            label="기본 시간대 (KST 또는 UTC)",
            default=settings.default_timezone,
            required=True,
            max_length=3,
        )
        self.win5_enabled = discord.ui.TextInput(
            label="WIN5 알림 (true 또는 false)",
            default=str(settings.win5_announcements_enabled).lower(),
            required=True,
            max_length=5,
        )
        self.room_enabled = discord.ui.TextInput(
            label="룸매치 알림 (true 또는 false)",
            default=str(settings.room_match_announcements_enabled).lower(),
            required=True,
            max_length=5,
        )
        self.reason = discord.ui.TextInput(
            label="변경 사유",
            style=discord.TextStyle.paragraph,
            required=True,
            min_length=1,
            max_length=255,
        )
        self.add_item(self.timezone_name)
        self.add_item(self.win5_enabled)
        self.add_item(self.room_enabled)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            await self._adapter.ports.prepare_component(
                interaction,
                self._context,
                command_name="settings.general",
            )
            is None
        ):
            return
        try:
            timezone_name = str(self.timezone_name.value).strip().upper()
            if timezone_name not in {"KST", "UTC"}:
                raise ValueError("기본 시간대는 KST 또는 UTC여야 합니다.")
            proposed = replace(
                self._adapter.settings_values(self._settings),
                default_timezone=timezone_name,
                win5_announcements_enabled=_parse_settings_boolean(
                    str(self.win5_enabled.value),
                    label="WIN5 알림",
                ),
                room_match_announcements_enabled=_parse_settings_boolean(
                    str(self.room_enabled.value),
                    label="룸매치 알림",
                ),
            )
        except ValueError as exc:
            await self._adapter.ports.send_followup(
                interaction,
                "settings.general",
                str(exc),
                response_kind="validation-error",
            )
            return
        await self._adapter.send_preview(
            interaction,
            settings=self._settings,
            proposed=proposed,
            reason=str(self.reason.value),
            context=self._context,
            command_name="settings.general",
        )


class SettingsRoleSelect(discord.ui.RoleSelect):
    def __init__(
        self,
        *,
        adapter: SettingsAdapter,
        settings: GuildDiscordSettingsDTO,
        context: InteractionContext,
        field_name: str,
        placeholder: str,
    ) -> None:
        super().__init__(placeholder=placeholder, min_values=1, max_values=1)
        self._adapter = adapter
        self._settings = settings
        self._context = context
        self._field_name = field_name

    async def callback(self, interaction: discord.Interaction) -> None:
        if (
            await self._adapter.ports.prepare_component(
                interaction,
                self._context,
                command_name="settings.role",
                defer=False,
            )
            is None
        ):
            return
        role = self.values[0]
        if role.id == self._context.guild_id:
            await self._adapter.ports.send_followup(
                interaction,
                "settings.role",
                "`@everyone` 역할은 운영 권한으로 지정할 수 없습니다.",
                response_kind="validation-error",
            )
            return
        try:
            await interaction.response.send_modal(
                SettingsRoleReasonModal(
                    adapter=self._adapter,
                    settings=self._settings,
                    context=self._context,
                    field_name=self._field_name,
                    role_id=str(role.id),
                )
            )
        except Exception:
            await self._adapter.ports.send_internal_error(interaction, "settings.role")


class SettingsRolePanelView(discord.ui.View):
    def __init__(
        self,
        *,
        adapter: SettingsAdapter,
        settings: GuildDiscordSettingsDTO,
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self.add_item(
            SettingsRoleSelect(
                adapter=adapter,
                settings=settings,
                context=context,
                field_name="operator_role_id",
                placeholder="운영자 역할 선택",
            )
        )
        self.add_item(
            SettingsRoleSelect(
                adapter=adapter,
                settings=settings,
                context=context,
                field_name="bot_manager_role_id",
                placeholder="봇 관리 역할 선택",
            )
        )


class SettingsRoleReasonModal(discord.ui.Modal, title="역할 변경 사유"):
    def __init__(
        self,
        *,
        adapter: SettingsAdapter,
        settings: GuildDiscordSettingsDTO,
        context: InteractionContext,
        field_name: str,
        role_id: str,
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._settings = settings
        self._context = context
        self._field_name = field_name
        self._role_id = role_id
        self.reason = discord.ui.TextInput(
            label="변경 사유",
            style=discord.TextStyle.paragraph,
            required=True,
            min_length=1,
            max_length=255,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            await self._adapter.ports.prepare_component(
                interaction,
                self._context,
                command_name="settings.role",
            )
            is None
        ):
            return
        proposed = replace(
            self._adapter.settings_values(self._settings),
            **{self._field_name: self._role_id},
        )
        await self._adapter.send_preview(
            interaction,
            settings=self._settings,
            proposed=proposed,
            reason=str(self.reason.value),
            context=self._context,
            command_name="settings.role",
        )


class SettingsConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        adapter: SettingsAdapter,
        settings: GuildDiscordSettingsDTO,
        proposed: GuildDiscordSettingsValues,
        reason: str,
        context: InteractionContext,
        command_name: str = "settings.general",
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._settings = settings
        self._proposed = proposed
        self._reason = reason
        self._context = context
        self._command_name = command_name

    @discord.ui.button(label="저장", style=discord.ButtonStyle.success)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if (
            await self._adapter.ports.prepare_component(
                interaction,
                self._context,
                command_name=self._command_name,
            )
            is None
        ):
            return
        try:
            result = await run_blocking_application(
                lambda: self._adapter.ports.update_guild_settings(
                    self._settings,
                    self._proposed,
                    self._reason,
                    str(interaction.user.id),
                    str(interaction.id),
                )
            )
        except DomainError as exc:
            await self._adapter.ports.send_user_error(
                interaction,
                self._command_name,
                "설정을 저장하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self._adapter.ports.send_internal_error(interaction, self._command_name)
            return
        logger.info(
            "discord settings update succeeded correlation_id=%s audit_id=%s revision=%s",
            self._adapter.ports.correlation_id(interaction),
            result.audit_id,
            result.settings.revision_number,
        )
        await self._adapter.ports.send_followup(
            interaction,
            self._command_name,
            f"서버 설정을 저장했습니다. 설정 버전: {result.settings.revision_number}",
        )
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if (
            await self._adapter.ports.prepare_component(
                interaction,
                self._context,
                command_name=self._command_name,
            )
            is None
        ):
            return
        await self._adapter.ports.send_followup(
            interaction,
            self._command_name,
            "설정 변경을 취소했습니다.",
        )
        self.stop()


class SettingsCommandGroup(app_commands.Group):
    def __init__(self, *, adapter: SettingsAdapter) -> None:
        super().__init__(name="settings", description="서버 운영 설정을 확인하고 변경합니다.")
        self._adapter = adapter

    @app_commands.command(name="general", description="서버 일반 설정을 확인하고 변경합니다.")
    async def general(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_general(interaction)

    @app_commands.command(name="role", description="서버 운영 역할을 확인하고 변경합니다.")
    async def role(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_role(interaction)


def create_settings_command_group(ports: SettingsAdapterPorts) -> SettingsCommandGroup:
    return SettingsAdapter(ports).create_command_group()


def _parse_settings_boolean(value: str, *, label: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{label} 값은 true 또는 false여야 합니다.")


def _enabled_label(enabled: bool) -> str:
    return "사용" if enabled else "사용 안 함"


def _configured_label(channel_id: str | None) -> str:
    return "설정됨" if channel_id is not None else "미설정"


def _role_mention(role_id: str | None) -> str:
    return f"<@&{role_id}>" if role_id is not None else "미설정"
