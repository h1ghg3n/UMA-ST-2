from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Protocol

import discord
from discord import app_commands

from umacircle_bot.adapters.discord.common import run_blocking_application
from umacircle_bot.domain.errors import DomainError
from umacircle_bot.logging_safety import log_sanitized_exception
from umacircle_bot.services.dtos import (
    AccountOverviewDTO,
    AccountRegistrationMutationDTO,
    AccountRegistrationRequestDTO,
)


class PrepareCommandPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *,
        defer: bool = True,
    ) -> object | None: ...


class RequireGuildIdPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> str | None: ...


class SubmitRegistrationPort(Protocol):
    def __call__(
        self,
        *,
        guild_id: str,
        requester_discord_user_id: str,
        discord_nickname_snapshot: str,
        submitted_uma_pid: str,
        submitted_nickname: str | None,
        submitted_ingame_name: str | None,
        interaction_id: str,
    ) -> AccountRegistrationMutationDTO: ...


class QueryOwnedRegistrationPort(Protocol):
    def __call__(
        self,
        *,
        guild_id: str,
        requester_discord_user_id: str,
    ) -> AccountRegistrationRequestDTO | None: ...


class CancelRegistrationPort(Protocol):
    def __call__(
        self,
        *,
        request_id: int,
        guild_id: str,
        requester_discord_user_id: str,
        reason: str | None,
        interaction_id: str,
    ) -> AccountRegistrationMutationDTO: ...


class SendUserErrorPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        prefix: str,
        error: Exception,
    ) -> None: ...


class InteractionCommandPort(Protocol):
    async def __call__(self, interaction: discord.Interaction) -> None: ...


class SendInteractionCommandErrorPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> None: ...


class SendInitialResponsePort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        content: str,
    ) -> bool: ...


class SendFollowupPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        content: str,
        *,
        response_kind: str = "success",
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class AccountAdapterPorts:
    prepare_command: PrepareCommandPort
    require_guild_id: RequireGuildIdPort
    submit_registration: SubmitRegistrationPort
    query_owned_registration: QueryOwnedRegistrationPort
    cancel_registration: CancelRegistrationPort
    query_account_overview: Callable[[str], AccountOverviewDTO]
    registration_modal_factory: Callable[[AccountAdapter], discord.ui.Modal]
    legacy_link_request: InteractionCommandPort
    legacy_link_status: InteractionCommandPort
    legacy_link_cancel: InteractionCommandPort
    send_user_error: SendUserErrorPort
    send_internal_error: SendInteractionCommandErrorPort
    handle_modal_open_error: SendInteractionCommandErrorPort
    send_initial_response: SendInitialResponsePort
    send_followup: SendFollowupPort
    correlation_id: Callable[[object], str]
    interaction_user_id: Callable[[object], str]
    display_name: Callable[[object], str]
    bounded_message: Callable[[list[str]], str]
    safe_text: Callable[[str], str]
    logger: logging.Logger


class AccountAdapter:
    def __init__(self, ports: AccountAdapterPorts) -> None:
        self.ports = ports

    def create_command_group(self) -> AccountCommandGroup:
        return AccountCommandGroup(adapter=self)

    async def register(self, interaction: discord.Interaction) -> None:
        command_name = "account.register"
        if await self.ports.require_guild_id(interaction, command_name) is None:
            return
        if await self.ports.prepare_command(interaction, command_name, defer=False) is None:
            return
        try:
            await interaction.response.send_modal(self.ports.registration_modal_factory(self))
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def registration_status(self, interaction: discord.Interaction) -> None:
        command_name = "account.registration-status"
        guild_id = await self.ports.require_guild_id(interaction, command_name)
        if guild_id is None:
            return
        if await self.ports.prepare_command(interaction, command_name) is None:
            return
        requester_discord_user_id = str(interaction.user.id)
        try:
            request = await run_blocking_application(
                lambda: self.ports.query_owned_registration(
                    guild_id=guild_id,
                    requester_discord_user_id=requester_discord_user_id,
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "계정 등록 요청 상태를 조회하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_followup(
            interaction,
            command_name,
            self.format_registration_request(request),
        )

    async def info(self, interaction: discord.Interaction) -> None:
        command_name = "account.info"
        if await self.ports.prepare_command(interaction, command_name) is None:
            return
        discord_user_id = str(interaction.user.id)
        try:
            overview = await run_blocking_application(lambda: self.ports.query_account_overview(discord_user_id))
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "등록 계정 정보를 찾지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_followup(
            interaction,
            command_name,
            self.format_account_info(overview),
        )

    async def cancel_registration(
        self,
        interaction: discord.Interaction,
        *,
        confirm: str,
    ) -> None:
        command_name = "account.cancel-registration"
        guild_id = await self.ports.require_guild_id(interaction, command_name)
        if guild_id is None:
            return
        if await self.ports.prepare_command(interaction, command_name) is None:
            return
        if confirm.strip() != "취소":
            await self.ports.send_followup(
                interaction,
                command_name,
                "취소하려면 confirm에 `취소`를 입력하세요.",
            )
            return
        requester_discord_user_id = str(interaction.user.id)
        try:
            request = await run_blocking_application(
                lambda: self.ports.query_owned_registration(
                    guild_id=guild_id,
                    requester_discord_user_id=requester_discord_user_id,
                )
            )
            if request is None:
                await self.ports.send_followup(
                    interaction,
                    command_name,
                    "취소할 계정 등록 요청이 없습니다.",
                )
                return
            result = await run_blocking_application(
                lambda: self.ports.cancel_registration(
                    request_id=request.id,
                    guild_id=guild_id,
                    requester_discord_user_id=requester_discord_user_id,
                    reason="사용자 취소",
                    interaction_id=self.ports.correlation_id(interaction),
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "계정 등록 요청을 취소하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_followup(
            interaction,
            command_name,
            f"계정 등록 요청 #{result.request.id}을(를) 취소했습니다. "
            "계정과 서클 포인트는 생성되거나 지급되지 않습니다.",
        )

    async def submit_registration_modal(
        self,
        interaction: discord.Interaction,
        *,
        submitted_uma_pid: str,
        submitted_nickname: str | None,
        submitted_ingame_name: str | None,
    ) -> None:
        command_name = "account.register"
        guild_id = await self.ports.require_guild_id(interaction, command_name)
        if guild_id is None:
            return
        if await self.ports.prepare_command(interaction, command_name) is None:
            return
        requester_discord_user_id = str(interaction.user.id)
        discord_nickname_snapshot = self.ports.display_name(interaction.user)
        interaction_id = self.ports.correlation_id(interaction)
        try:
            result = await run_blocking_application(
                lambda: self.ports.submit_registration(
                    guild_id=guild_id,
                    requester_discord_user_id=requester_discord_user_id,
                    discord_nickname_snapshot=discord_nickname_snapshot,
                    submitted_uma_pid=submitted_uma_pid,
                    submitted_nickname=submitted_nickname,
                    submitted_ingame_name=submitted_ingame_name,
                    interaction_id=interaction_id,
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "계정 등록 요청을 제출하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        self.ports.logger.info(
            "account registration request submitted correlation_id=%s request_id=%s audit_id=%s",
            self.ports.correlation_id(interaction),
            result.request.id,
            result.audit_id,
        )
        await self.ports.send_followup(
            interaction,
            command_name,
            f"계정 등록 요청 #{result.request.id}을(를) 접수했습니다. "
            "운영자 승인 전에는 계정과 서클 포인트가 생성되거나 지급되지 않습니다.",
        )

    def format_account_info(self, overview: AccountOverviewDTO) -> str:
        account = overview.account
        identity_status_label = {
            "confirmed_identity": "확인됨",
            "pending_identity": "확인 대기",
            "identity_conflict": "확인 필요",
            "registration_cancelled": "등록 취소됨",
        }.get(account.identity_status, "알 수 없음")
        registered_at = account.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        active_season_line = (
            f"활성 시즌: {overview.win5.season.season_number} · {self.ports.safe_text(overview.win5.season.name)}"
            if overview.win5.season is not None
            else "활성 시즌 없음"
        )
        return self.ports.bounded_message(
            [
                "[개인정보]",
                f"PID: `{account.uma_pid or '-'}`",
                f"표시명: {self.ports.safe_text(account.nickname) if account.nickname else '-'}",
                f"인게임명: {self.ports.safe_text(account.ingame_name) if account.ingame_name else '-'}",
                f"인증 상태: {identity_status_label}",
                f"등록 시각: {registered_at}",
                "",
                "[룸매치 정보]",
                f"보유 서클 포인트: {account.circle_point_balance}",
                "",
                "[WIN5 정보]",
                active_season_line,
                f"내 시즌 승점: {overview.win5.season_score}점",
                f"내 TOP1 승점: {overview.win5.top1_score}점",
            ]
        )

    def format_registration_request(
        self,
        request: AccountRegistrationRequestDTO | None,
    ) -> str:
        if request is None:
            return "계정 등록 요청이 없습니다. `/account register`로 등록을 요청할 수 있습니다."
        status_label = {
            "pending": "운영자 승인 대기",
            "approved": "승인됨",
            "rejected": "반려됨",
            "cancelled": "취소됨",
        }.get(request.status, "알 수 없음")
        lines = [
            f"계정 등록 요청 #{request.id}",
            f"상태: {status_label}",
            f"PID: `{self.ports.safe_text(request.submitted_uma_pid)}`",
            f"표시명: {self.ports.safe_text(request.submitted_nickname or '-')}",
            f"인게임명: {self.ports.safe_text(request.submitted_ingame_name or '-')}",
            f"요청 시각: {request.created_at.astimezone(UTC):%Y-%m-%d %H:%M UTC}",
        ]
        if request.status == "pending":
            lines.append("운영자 승인 전에는 계정과 서클 포인트가 생성되거나 지급되지 않습니다.")
        if request.review_note:
            lines.append(f"처리 메모: {self.ports.safe_text(request.review_note)}")
        if request.resolved_at is not None:
            lines.append(f"처리 시각: {request.resolved_at.astimezone(UTC):%Y-%m-%d %H:%M UTC}")
        if request.status == "approved":
            lines.extend(
                [
                    f"참가자: {self.ports.safe_text(request.accepted_persona_display_name or '-')} "
                    f"(`{request.accepted_persona_short_id or '-'}`)",
                    f"연결 게임 계정: {self.ports.safe_text(request.accepted_game_account_name or '-')}",
                    f"초기 서클 포인트 지급량: {request.initial_grant_amount}",
                ]
            )
        return self.ports.bounded_message(lines)


class AccountRegistrationModal(discord.ui.Modal, title="계정 등록"):
    def __init__(self, *, adapter: AccountAdapter) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self.uma_pid = discord.ui.TextInput(
            custom_id="account-registration-pid",
            placeholder="숫자로 된 우마무스메 PID",
            required=True,
            min_length=1,
            max_length=32,
        )
        self.nickname = discord.ui.TextInput(
            custom_id="account-registration-nickname",
            placeholder="선택 입력",
            required=False,
            max_length=100,
        )
        self.ingame_name = discord.ui.TextInput(
            custom_id="account-registration-ingame-name",
            placeholder="선택 입력",
            required=False,
            max_length=100,
        )
        self.add_item(discord.ui.Label(text="PID", component=self.uma_pid))
        self.add_item(discord.ui.Label(text="표시명", component=self.nickname))
        self.add_item(discord.ui.Label(text="인게임명", component=self.ingame_name))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.submit_registration_modal(
            interaction,
            submitted_uma_pid=str(self.uma_pid.value),
            submitted_nickname=_optional_modal_text(self.nickname.value),
            submitted_ingame_name=_optional_modal_text(self.ingame_name.value),
        )

    async def on_error(self, interaction: discord.Interaction, _error: Exception) -> None:
        log_sanitized_exception(
            self._adapter.ports.logger,
            "discord account registration modal failed correlation_id=%s actor_id=%s",
            self._adapter.ports.correlation_id(interaction),
            self._adapter.ports.interaction_user_id(interaction),
        )
        if not interaction.response.is_done():
            await self._adapter.ports.send_initial_response(
                interaction,
                "account.register",
                f"내부 오류가 발생했습니다. 요청 ID: `{self._adapter.ports.correlation_id(interaction)}`",
            )


class AccountCommandGroup(app_commands.Group):
    def __init__(self, *, adapter: AccountAdapter) -> None:
        super().__init__(name="account", description="우마무스메 PID 계정을 관리합니다.")
        self._adapter = adapter

    @app_commands.command(name="register", description="PID 계정 등록을 운영자에게 요청합니다.")
    async def register(self, interaction: discord.Interaction) -> None:
        await self._adapter.register(interaction)

    @app_commands.command(name="registration-status", description="내 계정 등록 요청 상태를 확인합니다.")
    async def registration_status(self, interaction: discord.Interaction) -> None:
        await self._adapter.registration_status(interaction)

    @app_commands.command(name="info", description="등록 계정과 서클 포인트 정보를 확인합니다.")
    async def info(self, interaction: discord.Interaction) -> None:
        await self._adapter.info(interaction)

    @app_commands.command(name="cancel-registration", description="대기 중인 계정 등록 요청을 취소합니다.")
    @app_commands.describe(confirm="취소하려면 취소를 입력")
    async def cancel_registration(self, interaction: discord.Interaction, confirm: str) -> None:
        await self._adapter.cancel_registration(interaction, confirm=confirm)

    @app_commands.command(name="link-request", description="기존 계정·서클 포인트 기록의 연결을 요청합니다.")
    async def link_request(self, interaction: discord.Interaction) -> None:
        await self._adapter.ports.legacy_link_request(interaction)

    @app_commands.command(name="link-status", description="내 기존 기록 연결 요청 상태를 확인합니다.")
    async def link_status(self, interaction: discord.Interaction) -> None:
        await self._adapter.ports.legacy_link_status(interaction)

    @app_commands.command(name="link-cancel", description="진행 중인 기존 기록 연결 요청을 취소합니다.")
    async def link_cancel(self, interaction: discord.Interaction) -> None:
        await self._adapter.ports.legacy_link_cancel(interaction)


def create_account_command_group(ports: AccountAdapterPorts) -> AccountCommandGroup:
    return AccountAdapter(ports).create_command_group()


def _optional_modal_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
