from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Protocol

import discord

from umacircle_bot.adapters.discord.common import run_blocking_application
from umacircle_bot.adapters.discord.player_link_ui import (
    PlayerLinkCancelModal,
    PlayerLinkInteractionContext,
    PlayerLinkOwnedStatusView,
    PlayerLinkRequestModal,
)
from umacircle_bot.domain.errors import DomainError
from umacircle_bot.logging_safety import log_sanitized_exception
from umacircle_bot.services.dtos import PlayerLinkMutationDTO, PlayerLinkRequestDTO


class PrepareCommandPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *,
        defer: bool = True,
    ) -> object | None: ...


class PrepareComponentPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        context: PlayerLinkInteractionContext,
        *,
        command_name: str,
        defer: bool = True,
    ) -> object | None: ...


class SubmitPlayerLinkPort(Protocol):
    def __call__(
        self,
        *,
        guild_id: str,
        requester_discord_user_id: str,
        discord_nickname_snapshot: str,
        submitted_ingame_name: str,
        submitted_uma_pid: str,
        submitted_nickname_chunk: str | None,
        submitted_participation_hint: str | None,
        requester_note: str | None,
        interaction_id: str,
    ) -> PlayerLinkMutationDTO: ...


class CancelPlayerLinkPort(Protocol):
    def __call__(
        self,
        *,
        request_id: int,
        guild_id: str,
        requester_discord_user_id: str,
        reason: str | None,
        interaction_id: str,
    ) -> PlayerLinkMutationDTO: ...


class RevisePlayerLinkPort(Protocol):
    def __call__(
        self,
        *,
        request_id: int,
        guild_id: str,
        requester_discord_user_id: str,
        discord_nickname_snapshot: str,
        submitted_ingame_name: str,
        submitted_uma_pid: str,
        submitted_nickname_chunk: str | None,
        submitted_participation_hint: str | None,
        requester_note: str | None,
        interaction_id: str,
    ) -> PlayerLinkMutationDTO: ...


class SendUserErrorPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        prefix: str,
        error: Exception,
    ) -> None: ...


class SendCommandErrorPort(Protocol):
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
        view: discord.ui.View | None = None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class PlayerLinkAdapterPorts:
    prepare_command: PrepareCommandPort
    prepare_component: PrepareComponentPort
    query_owned_request: Callable[[str, str], PlayerLinkRequestDTO | None]
    submit_request: SubmitPlayerLinkPort
    cancel_request: CancelPlayerLinkPort
    revise_request: RevisePlayerLinkPort
    send_user_error: SendUserErrorPort
    send_pre_modal_user_error: SendUserErrorPort
    send_internal_error: SendCommandErrorPort
    handle_modal_open_error: SendCommandErrorPort
    send_initial_response: SendInitialResponsePort
    send_followup: SendFollowupPort
    correlation_id: Callable[[object], str]
    display_name: Callable[[object], str]
    bounded_message: Callable[[list[str]], str]
    safe_text: Callable[[str], str]
    logger: logging.Logger


class PlayerLinkAdapter:
    def __init__(self, ports: PlayerLinkAdapterPorts) -> None:
        self.ports = ports

    async def open_member_request(self, interaction: discord.Interaction) -> None:
        command_name = "account.link-request"
        if await self.ports.prepare_command(interaction, command_name, defer=False) is None:
            return
        try:
            context = interaction_context(interaction)
            await interaction.response.send_modal(
                PlayerLinkRequestModal(
                    on_submit_callback=lambda modal_interaction, values: self.submit_member_request(
                        modal_interaction,
                        context=context,
                        values=values,
                    )
                )
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def show_member_status(self, interaction: discord.Interaction) -> None:
        command_name = "account.link-status"
        if await self.ports.prepare_command(interaction, command_name) is None:
            return
        guild_id = str(interaction.guild_id)
        requester_discord_user_id = str(interaction.user.id)
        try:
            request = await run_blocking_application(
                lambda: self.ports.query_owned_request(
                    guild_id,
                    requester_discord_user_id,
                )
            )
            view = self.owned_status_view(interaction, request)
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "연결 요청 상태를 조회하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_followup(
            interaction,
            command_name,
            self.format_owned_request(request),
            view=view,
        )

    async def open_member_cancel(self, interaction: discord.Interaction) -> None:
        command_name = "account.link-cancel"
        if await self.ports.prepare_command(interaction, command_name, defer=False) is None:
            return
        guild_id = str(interaction.guild_id)
        requester_discord_user_id = str(interaction.user.id)
        try:
            request = await run_blocking_application(
                lambda: self.ports.query_owned_request(
                    guild_id,
                    requester_discord_user_id,
                )
            )
            if request is None or request.status not in {"pending", "review_required"}:
                await self.ports.send_pre_modal_user_error(
                    interaction,
                    command_name,
                    "취소할 진행 중 요청이 없습니다",
                    ValueError(),
                )
                return
            context = interaction_context(interaction)
            await interaction.response.send_modal(
                PlayerLinkCancelModal(
                    on_submit_callback=lambda modal_interaction, values: self.submit_member_cancel(
                        modal_interaction,
                        context=context,
                        request_id=request.id,
                        values=values,
                    )
                )
            )
        except DomainError as exc:
            await self.ports.send_pre_modal_user_error(
                interaction,
                command_name,
                "연결 요청을 조회하지 못했습니다",
                exc,
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def submit_member_request(
        self,
        interaction: discord.Interaction,
        *,
        context: PlayerLinkInteractionContext,
        values: dict[str, str | None],
    ) -> None:
        command_name = "account.link-request"
        if not await self._prepare_bound_submission(interaction, context, command_name):
            return
        requester_discord_user_id = str(interaction.user.id)
        discord_nickname_snapshot = self.ports.display_name(interaction.user)
        interaction_id = self.ports.correlation_id(interaction)
        try:
            result = await run_blocking_application(
                lambda: self.ports.submit_request(
                    guild_id=str(context.guild_id),
                    requester_discord_user_id=requester_discord_user_id,
                    discord_nickname_snapshot=discord_nickname_snapshot,
                    submitted_ingame_name=values["ingame_name"] or "",
                    submitted_uma_pid=values["uma_pid"] or "",
                    submitted_nickname_chunk=values["nickname_chunk"],
                    submitted_participation_hint=values["participation_hint"],
                    requester_note=values["note"],
                    interaction_id=interaction_id,
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "연결 요청을 제출하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        self.ports.logger.info(
            "player-link request submitted correlation_id=%s request_id=%s audit_id=%s",
            self.ports.correlation_id(interaction),
            result.request.id,
            result.audit_id,
        )
        await self.ports.send_followup(
            interaction,
            command_name,
            f"기존 기록 연결 요청 #{result.request.id}을(를) 접수했습니다. "
            "운영자 승인 전에는 계정과 서클 포인트가 연결되지 않습니다.",
        )

    async def submit_member_cancel(
        self,
        interaction: discord.Interaction,
        *,
        context: PlayerLinkInteractionContext,
        request_id: int,
        values: dict[str, str | None],
    ) -> None:
        command_name = "account.link-cancel"
        if not await self._prepare_bound_submission(interaction, context, command_name):
            return
        requester_discord_user_id = str(interaction.user.id)
        interaction_id = self.ports.correlation_id(interaction)
        try:
            result = await run_blocking_application(
                lambda: self.ports.cancel_request(
                    request_id=request_id,
                    guild_id=str(context.guild_id),
                    requester_discord_user_id=requester_discord_user_id,
                    reason=values.get("reason"),
                    interaction_id=interaction_id,
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "연결 요청을 취소하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_followup(
            interaction,
            command_name,
            f"연결 요청 #{result.request.id}을(를) 취소했습니다.",
        )

    async def open_member_revision(
        self,
        interaction: discord.Interaction,
        *,
        context: PlayerLinkInteractionContext,
        request: PlayerLinkRequestDTO,
    ) -> None:
        command_name = "account.link-status"
        if (
            await self.ports.prepare_component(
                interaction,
                context,
                command_name=command_name,
                defer=False,
            )
            is None
        ):
            return
        if request.status != "review_required":
            await self.ports.send_initial_response(
                interaction,
                command_name,
                "정보를 보충할 수 있는 요청 상태가 아닙니다.",
            )
            return
        defaults = {
            "ingame_name": request.submitted_ingame_name,
            "uma_pid": request.submitted_uma_pid,
            "nickname_chunk": request.submitted_nickname_chunk,
            "participation_hint": request.submitted_participation_hint,
            "note": request.requester_note,
        }
        source_message = getattr(interaction, "message", None)
        try:
            await interaction.response.send_modal(
                PlayerLinkRequestModal(
                    title="기존 기록 연결 정보 보충",
                    defaults=defaults,
                    on_submit_callback=lambda modal_interaction, values: self.submit_member_revision(
                        modal_interaction,
                        context=context,
                        request_id=request.id,
                        source_message=source_message,
                        values=values,
                    ),
                )
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def submit_member_revision(
        self,
        interaction: discord.Interaction,
        *,
        context: PlayerLinkInteractionContext,
        request_id: int,
        source_message: object | None,
        values: dict[str, str | None],
    ) -> None:
        command_name = "account.link-status"
        if not await self._prepare_bound_submission(interaction, context, command_name):
            return
        requester_discord_user_id = str(interaction.user.id)
        discord_nickname_snapshot = self.ports.display_name(interaction.user)
        interaction_id = self.ports.correlation_id(interaction)
        try:
            result = await run_blocking_application(
                lambda: self.ports.revise_request(
                    request_id=request_id,
                    guild_id=str(context.guild_id),
                    requester_discord_user_id=requester_discord_user_id,
                    discord_nickname_snapshot=discord_nickname_snapshot,
                    submitted_ingame_name=values["ingame_name"] or "",
                    submitted_uma_pid=values["uma_pid"] or "",
                    submitted_nickname_chunk=values["nickname_chunk"],
                    submitted_participation_hint=values["participation_hint"],
                    requester_note=values["note"],
                    interaction_id=interaction_id,
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "연결 요청 정보를 보충하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await close_view(source_message, logger=self.ports.logger)
        await self.ports.send_followup(
            interaction,
            command_name,
            f"연결 요청 #{result.request.id}의 정보를 보충하고 운영자 검토 대기 상태로 다시 등록했습니다.",
        )

    def owned_status_view(
        self,
        interaction: discord.Interaction,
        request: PlayerLinkRequestDTO | None,
    ) -> PlayerLinkOwnedStatusView | None:
        if request is None or request.status != "review_required":
            return None
        context = interaction_context(interaction)
        return PlayerLinkOwnedStatusView(
            context=context,
            on_open_revision=lambda component_interaction: self.open_member_revision(
                component_interaction,
                context=context,
                request=request,
            ),
        )

    def format_owned_request(self, request: PlayerLinkRequestDTO | None) -> str:
        if request is None:
            return (
                "진행 중이거나 최근에 제출한 기존 기록 연결 요청이 없습니다. "
                "신규 계정이면 `/account register`를 사용하세요."
            )
        status = {
            "pending": "운영자 검토 대기",
            "review_required": "추가 검토 필요",
            "approved": "연결 승인됨",
            "rejected": "연결 요청 거부됨",
            "cancelled": "요청 취소됨",
        }.get(request.status, "알 수 없음")
        lines = [
            f"요청 #{request.id}",
            f"상태: {status}",
            f"인게임명: {self.ports.safe_text(request.submitted_ingame_name)}",
            f"PID: `{request.submitted_uma_pid}`",
            f"요청 시각: {request.created_at.astimezone(UTC):%Y-%m-%d %H:%M UTC}",
        ]
        if request.resolved_at is not None:
            lines.append(f"처리 시각: {request.resolved_at.astimezone(UTC):%Y-%m-%d %H:%M UTC}")
        return self.ports.bounded_message(lines)

    async def _prepare_bound_submission(
        self,
        interaction: discord.Interaction,
        context: PlayerLinkInteractionContext,
        command_name: str,
    ) -> bool:
        return (
            await self.ports.prepare_component(
                interaction,
                context,
                command_name=command_name,
            )
            is not None
        )


def interaction_context(interaction: discord.Interaction) -> PlayerLinkInteractionContext:
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    guild_id = getattr(interaction, "guild_id", None)
    channel_id = getattr(interaction, "channel_id", None)
    if not all(isinstance(value, int) and value > 0 for value in (user_id, guild_id, channel_id)):
        raise ValueError("연결 요청 화면에 필요한 Discord 요청 정보가 없습니다.")
    return PlayerLinkInteractionContext(
        user_id=user_id,
        guild_id=guild_id,
        channel_id=channel_id,
    )


async def close_view(source_message: object | None, *, logger: logging.Logger) -> None:
    edit = getattr(source_message, "edit", None)
    if not callable(edit):
        return
    try:
        await edit(view=None)
    except Exception:
        log_sanitized_exception(logger, "player-link interaction view close failed")
