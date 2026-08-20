from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Protocol

import discord

from umacircle_bot.adapters.discord.common import run_blocking_application
from umacircle_bot.adapters.discord.player_link_ui import (
    PlayerLinkCandidateDetailView,
    PlayerLinkCandidateSelectView,
    PlayerLinkInteractionContext,
    PlayerLinkNoCandidateView,
    PlayerLinkRequestModal,
    PlayerLinkRequestSelectView,
    PlayerLinkStaffNoteModal,
)
from umacircle_bot.domain.errors import DomainError
from umacircle_bot.logging_safety import log_sanitized_exception
from umacircle_bot.services.dtos import (
    PlayerLinkApprovalDTO,
    PlayerLinkCandidateDTO,
    PlayerLinkMutationDTO,
    PlayerLinkRequestDTO,
)


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


class ApprovePlayerLinkPort(Protocol):
    def __call__(
        self,
        *,
        request_id: int,
        candidate: PlayerLinkCandidateDTO,
        actor_discord_user_id: str,
        interaction_id: str,
    ) -> PlayerLinkApprovalDTO | PlayerLinkMutationDTO: ...


class ResolvePlayerLinkPort(Protocol):
    def __call__(
        self,
        *,
        request_id: int,
        actor_discord_user_id: str,
        note: str,
        interaction_id: str,
    ) -> PlayerLinkMutationDTO: ...


class SubmitStaffPlayerLinkPort(Protocol):
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
        submitted_by_discord_user_id: str,
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


class PublishResolutionHistoryPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        *,
        request: PlayerLinkRequestDTO,
        resolution: str,
    ) -> None: ...


class CloseViewPort(Protocol):
    async def __call__(self, source_message: object | None) -> None: ...


@dataclass(frozen=True, slots=True)
class StaffPlayerLinkAdapterPorts:
    prepare_command: PrepareCommandPort
    prepare_component: PrepareComponentPort
    query_active_request: Callable[[str, str], PlayerLinkRequestDTO | None]
    query_queue: Callable[[str], tuple[PlayerLinkRequestDTO, ...]]
    query_request: Callable[[str, int], PlayerLinkRequestDTO]
    query_candidates: Callable[[int], tuple[PlayerLinkCandidateDTO, ...]]
    query_candidates_for_terms: Callable[
        [str, str, str | None],
        tuple[PlayerLinkCandidateDTO, ...],
    ]
    submit_request: SubmitStaffPlayerLinkPort
    approve_request: ApprovePlayerLinkPort
    review_request: ResolvePlayerLinkPort
    reject_request: ResolvePlayerLinkPort
    publish_resolution_history: PublishResolutionHistoryPort
    close_view: CloseViewPort
    send_user_error: SendUserErrorPort
    send_pre_modal_user_error: SendUserErrorPort
    send_internal_error: SendCommandErrorPort
    handle_modal_open_error: SendCommandErrorPort
    send_followup: SendFollowupPort
    correlation_id: Callable[[object], str]
    display_name: Callable[[object], str]
    bounded_message: Callable[[list[str]], str]
    safe_text: Callable[[str], str]
    logger: logging.Logger


class StaffPlayerLinkAdapter:
    def __init__(self, ports: StaffPlayerLinkAdapterPorts) -> None:
        self.ports = ports

    async def link_player(
        self,
        interaction: discord.Interaction,
        *,
        target: discord.Member | None,
    ) -> None:
        if target is None:
            await self.open_queue(interaction)
            return
        command_name = "staff.link-player"
        if await self.ports.prepare_command(interaction, command_name, defer=False) is None:
            return
        context = interaction_context(interaction)
        target_discord_user_id = str(target.id)
        target_discord_nickname = self.ports.display_name(target)
        await self.open_direct_modal(
            interaction,
            context=context,
            target_discord_user_id=target_discord_user_id,
            target_discord_nickname=target_discord_nickname,
        )

    async def open_queue(self, interaction: discord.Interaction) -> None:
        command_name = "staff.link-player"
        if await self.ports.prepare_command(interaction, command_name) is None:
            return
        guild_id = str(interaction.guild_id)
        try:
            requests = await run_blocking_application(lambda: self.ports.query_queue(guild_id))
            if not requests:
                await self.ports.send_followup(
                    interaction,
                    command_name,
                    "처리할 기존 기록 연결 요청이 없습니다.",
                )
                return
            context = interaction_context(interaction)
            view = PlayerLinkRequestSelectView(
                context=context,
                requests=requests,
                on_selected=lambda component_interaction, request_id: self.select_request(
                    component_interaction,
                    context=context,
                    request_id=request_id,
                ),
            )
            await self.ports.send_followup(
                interaction,
                command_name,
                "기존 기록 연결 요청을 선택하세요. 목록에는 최대 25건이 표시됩니다.",
                view=view,
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "연결 요청 목록을 조회하지 못했습니다",
                exc,
            )
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)

    async def open_direct_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: PlayerLinkInteractionContext,
        target_discord_user_id: str,
        target_discord_nickname: str,
    ) -> None:
        command_name = "staff.link-player"
        try:
            existing_request = await run_blocking_application(
                lambda: self.ports.query_active_request(
                    str(context.guild_id),
                    target_discord_user_id,
                )
            )
        except DomainError as exc:
            await self.ports.send_pre_modal_user_error(
                interaction,
                command_name,
                "기존 연결 요청을 조회하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)
            return
        defaults = {
            "ingame_name": (
                existing_request.submitted_ingame_name if existing_request is not None else target_discord_nickname
            ),
            "uma_pid": existing_request.submitted_uma_pid if existing_request is not None else None,
            "nickname_chunk": (existing_request.submitted_nickname_chunk if existing_request is not None else None),
            "participation_hint": (
                existing_request.submitted_participation_hint if existing_request is not None else None
            ),
            "note": existing_request.requester_note if existing_request is not None else None,
        }
        try:
            await interaction.response.send_modal(
                PlayerLinkRequestModal(
                    title="운영자 직접 기존 기록 연결",
                    defaults=defaults,
                    on_submit_callback=lambda modal_interaction, values: self.submit_direct(
                        modal_interaction,
                        context=context,
                        target_discord_user_id=target_discord_user_id,
                        target_discord_nickname=target_discord_nickname,
                        expected_existing_request_id=(existing_request.id if existing_request is not None else None),
                        source_message=None,
                        values=values,
                    ),
                )
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def submit_direct(
        self,
        interaction: discord.Interaction,
        *,
        context: PlayerLinkInteractionContext,
        target_discord_user_id: str,
        target_discord_nickname: str,
        expected_existing_request_id: int | None,
        source_message: object | None,
        values: dict[str, str | None],
    ) -> None:
        command_name = "staff.link-player"
        if await self._prepare_component(interaction, context, command_name) is None:
            return
        actor_discord_user_id = str(interaction.user.id)
        interaction_id = self.ports.correlation_id(interaction)
        try:
            existing_request = await run_blocking_application(
                lambda: self.ports.query_active_request(
                    str(context.guild_id),
                    target_discord_user_id,
                )
            )
            if expected_existing_request_id is not None:
                if existing_request is None or existing_request.id != expected_existing_request_id:
                    raise ValueError("연결 요청이 변경되었습니다. 다시 열어 주세요.")
                if values["uma_pid"] != existing_request.submitted_uma_pid:
                    raise ValueError("기존 연결 요청의 PID는 직접 변경할 수 없습니다.")
                request = existing_request
            else:
                submission = await run_blocking_application(
                    lambda: self.ports.submit_request(
                        guild_id=str(context.guild_id),
                        requester_discord_user_id=target_discord_user_id,
                        discord_nickname_snapshot=target_discord_nickname,
                        submitted_ingame_name=values["ingame_name"] or "",
                        submitted_uma_pid=values["uma_pid"] or "",
                        submitted_nickname_chunk=values["nickname_chunk"],
                        submitted_participation_hint=values["participation_hint"],
                        requester_note=values["note"],
                        submitted_by_discord_user_id=actor_discord_user_id,
                        interaction_id=interaction_id,
                    )
                )
                request = submission.request
            candidates = await run_blocking_application(
                lambda: self.ports.query_candidates_for_terms(
                    values["ingame_name"] or "",
                    target_discord_nickname,
                    values["nickname_chunk"],
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "직접 연결 요청을 준비하지 못했습니다",
                exc,
            )
            return
        except ValueError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "직접 연결 검색을 진행하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        evidence = self.format_evidence(request) + "\n운영자가 수정한 검색어 기준으로 후보를 정렬했습니다."
        if not candidates:
            sent = await self.ports.send_followup(
                interaction,
                command_name,
                evidence + "\n\n일치 후보가 없습니다. 재검토 또는 거부 사유를 기록하세요.",
                view=self.no_candidate_view(context=context, request=request),
            )
        else:
            sent = await self.ports.send_followup(
                interaction,
                command_name,
                evidence + "\n\n후보는 수정한 검색어와의 유사도 높은 순입니다.",
                view=self.candidate_select_view(
                    context=context,
                    request=request,
                    candidates=candidates,
                ),
            )
        if sent and source_message is not None:
            await self.ports.close_view(source_message)

    async def select_request(
        self,
        interaction: discord.Interaction,
        *,
        context: PlayerLinkInteractionContext,
        request_id: int,
    ) -> None:
        command_name = "staff.link-player"
        if await self._prepare_component(interaction, context, command_name) is None:
            return
        source_message = getattr(interaction, "message", None)
        try:
            request, candidates = await asyncio.gather(
                run_blocking_application(
                    lambda: self.ports.query_request(
                        str(context.guild_id),
                        request_id,
                    )
                ),
                run_blocking_application(lambda: self.ports.query_candidates(request_id)),
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "연결 요청 또는 후보를 조회하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        if not candidates:
            sent = await self.ports.send_followup(
                interaction,
                command_name,
                self.format_evidence(request) + "\n\n일치 후보가 없습니다. 재검토 또는 거부 사유를 기록하세요.",
                view=self.no_candidate_view(context=context, request=request),
            )
            if sent:
                await self.ports.close_view(source_message)
            return
        sent = await self.ports.send_followup(
            interaction,
            command_name,
            self.format_evidence(request) + "\n\n후보를 선택하면 상세 정보와 처리 버튼이 표시됩니다.",
            view=self.candidate_select_view(
                context=context,
                request=request,
                candidates=candidates,
            ),
        )
        if sent:
            await self.ports.close_view(source_message)

    async def select_candidate(
        self,
        interaction: discord.Interaction,
        *,
        context: PlayerLinkInteractionContext,
        request: PlayerLinkRequestDTO,
        candidates: tuple[PlayerLinkCandidateDTO, ...],
        candidate_id: int,
    ) -> None:
        command_name = "staff.link-player"
        if await self._prepare_component(interaction, context, command_name) is None:
            return
        source_message = getattr(interaction, "message", None)
        candidate = next((value for value in candidates if value.game_account_id == candidate_id), None)
        if candidate is None:
            await self.ports.send_followup(
                interaction,
                command_name,
                "현재 화면에서 선택할 수 없는 후보입니다.",
            )
            return
        sent = await self.ports.send_followup(
            interaction,
            command_name,
            self.format_candidate_detail(request, candidate),
            view=self.candidate_detail_view(
                context=context,
                request=request,
                candidate=candidate,
            ),
        )
        if sent:
            await self.ports.close_view(source_message)

    async def approve(
        self,
        interaction: discord.Interaction,
        *,
        context: PlayerLinkInteractionContext,
        request: PlayerLinkRequestDTO,
        candidate: PlayerLinkCandidateDTO,
    ) -> None:
        command_name = "staff.link-player"
        if await self._prepare_component(interaction, context, command_name) is None:
            return
        actor_discord_user_id = str(interaction.user.id)
        interaction_id = self.ports.correlation_id(interaction)
        try:
            result = await run_blocking_application(
                lambda: self.ports.approve_request(
                    request_id=request.id,
                    candidate=candidate,
                    actor_discord_user_id=actor_discord_user_id,
                    interaction_id=interaction_id,
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "연결 요청을 승인하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        source_message = getattr(interaction, "message", None)
        if isinstance(result, PlayerLinkMutationDTO):
            await self.ports.close_view(source_message)
            await self.ports.send_followup(
                interaction,
                command_name,
                "후보의 서클 포인트 또는 원장 상태가 변경되어 요청을 재검토 상태로 전환했습니다. "
                "후보를 다시 확인하세요.",
            )
            return
        self.ports.logger.info(
            "player-link approved correlation_id=%s request_id=%s audit_id=%s game_account_id=%s",
            self.ports.correlation_id(interaction),
            result.request.id,
            result.audit_id,
            result.game_account_id,
        )
        await self.ports.publish_resolution_history(
            interaction,
            request=result.request,
            resolution="승인",
        )
        await self.ports.close_view(source_message)
        await self.ports.send_followup(
            interaction,
            command_name,
            f"연결 요청 #{result.request.id}을(를) 승인했습니다. "
            "기존 서클 포인트와 원장을 유지했고 초기 서클 포인트는 지급하지 않았습니다.",
        )

    async def open_search_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: PlayerLinkInteractionContext,
        request: PlayerLinkRequestDTO,
    ) -> None:
        command_name = "staff.link-player"
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
                    title="운영자 후보 검색 수정",
                    defaults=defaults,
                    on_submit_callback=lambda modal_interaction, values: self.submit_direct(
                        modal_interaction,
                        context=context,
                        target_discord_user_id=request.requester_discord_user_id,
                        target_discord_nickname=request.discord_nickname_snapshot,
                        expected_existing_request_id=request.id,
                        source_message=source_message,
                        values=values,
                    ),
                )
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def open_note_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: PlayerLinkInteractionContext,
        request_id: int,
        action: str,
    ) -> None:
        command_name = "staff.link-player"
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
        source_message = getattr(interaction, "message", None)
        try:
            await interaction.response.send_modal(
                PlayerLinkStaffNoteModal(
                    action=action,
                    on_submit_callback=lambda modal_interaction, values: self.submit_note(
                        modal_interaction,
                        context=context,
                        request_id=request_id,
                        source_message=source_message,
                        values=values,
                    ),
                )
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def submit_note(
        self,
        interaction: discord.Interaction,
        *,
        context: PlayerLinkInteractionContext,
        request_id: int,
        source_message: object | None,
        values: dict[str, str | None],
    ) -> None:
        command_name = "staff.link-player"
        if await self._prepare_component(interaction, context, command_name) is None:
            return
        action = values.get("action")
        note = values.get("note") or ""
        actor_discord_user_id = str(interaction.user.id)
        interaction_id = self.ports.correlation_id(interaction)
        try:
            if action == "review":
                operation = self.ports.review_request
            elif action == "reject":
                operation = self.ports.reject_request
            else:
                raise ValueError("처리 유형이 올바르지 않습니다.")
            result = await run_blocking_application(
                lambda: operation(
                    request_id=request_id,
                    actor_discord_user_id=actor_discord_user_id,
                    note=note,
                    interaction_id=interaction_id,
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "연결 요청을 처리하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        if action == "reject":
            await self.ports.publish_resolution_history(
                interaction,
                request=result.request,
                resolution="거절",
            )
        await self.ports.close_view(source_message)
        await self.ports.send_followup(
            interaction,
            command_name,
            f"연결 요청 #{result.request.id}을(를) `{result.request.status}` 상태로 처리했습니다.",
        )

    def candidate_select_view(
        self,
        *,
        context: PlayerLinkInteractionContext,
        request: PlayerLinkRequestDTO,
        candidates: tuple[PlayerLinkCandidateDTO, ...],
    ) -> PlayerLinkCandidateSelectView:
        return PlayerLinkCandidateSelectView(
            context=context,
            candidates=candidates,
            on_selected=lambda component_interaction, candidate_id: self.select_candidate(
                component_interaction,
                context=context,
                request=request,
                candidates=candidates,
                candidate_id=candidate_id,
            ),
        )

    def candidate_detail_view(
        self,
        *,
        context: PlayerLinkInteractionContext,
        request: PlayerLinkRequestDTO,
        candidate: PlayerLinkCandidateDTO,
    ) -> PlayerLinkCandidateDetailView:
        return PlayerLinkCandidateDetailView(
            context=context,
            on_approve=lambda component_interaction: self.approve(
                component_interaction,
                context=context,
                request=request,
                candidate=candidate,
            ),
            on_open_note=lambda component_interaction, action: self.open_note_modal(
                component_interaction,
                context=context,
                request_id=request.id,
                action=action,
            ),
            on_refine_search=lambda component_interaction: self.open_search_modal(
                component_interaction,
                context=context,
                request=request,
            ),
        )

    def no_candidate_view(
        self,
        *,
        context: PlayerLinkInteractionContext,
        request: PlayerLinkRequestDTO,
    ) -> PlayerLinkNoCandidateView:
        return PlayerLinkNoCandidateView(
            context=context,
            on_open_note=lambda component_interaction, action: self.open_note_modal(
                component_interaction,
                context=context,
                request_id=request.id,
                action=action,
            ),
            on_refine_search=lambda component_interaction: self.open_search_modal(
                component_interaction,
                context=context,
                request=request,
            ),
        )

    def format_evidence(self, request: PlayerLinkRequestDTO) -> str:
        lines = [
            f"연결 요청 #{request.id} / 상태 `{request.status}`",
            f"Discord 표시명 snapshot: {self.ports.safe_text(request.discord_nickname_snapshot)}",
            f"현재 인게임명: {self.ports.safe_text(request.submitted_ingame_name)}",
            f"제출 PID: `{request.submitted_uma_pid}`",
        ]
        if request.submitted_nickname_chunk is not None:
            lines.append(f"기억한 닉네임 일부: {self.ports.safe_text(request.submitted_nickname_chunk)}")
        if request.submitted_participation_hint is not None:
            lines.append(f"참가 참고: {self.ports.safe_text(request.submitted_participation_hint)}")
        if request.requester_note is not None:
            lines.append(f"추가 설명: {self.ports.safe_text(request.requester_note)}")
        return self.ports.bounded_message(lines)

    def format_candidate_detail(
        self,
        request: PlayerLinkRequestDTO,
        candidate: PlayerLinkCandidateDTO,
    ) -> str:
        lines = [
            f"요청 #{request.id}의 후보 상세",
            f"후보 내부 ID: {candidate.game_account_id}",
            f"레거시 닉네임: {self.ports.safe_text(candidate.legacy_nickname or '-')}",
            f"인게임명: {self.ports.safe_text(candidate.ingame_name or '-')}",
            f"현재 서클 포인트: {candidate.current_balance} / 원장: {candidate.ledger_count}건",
            (
                f"마지막 활동: {candidate.last_activity_at.astimezone(UTC):%Y-%m-%d %H:%M UTC}"
                if candidate.last_activity_at is not None
                else "마지막 활동: -"
            ),
            f"가져온 원본: {self.ports.safe_text(candidate.import_source)}",
            f"identity 상태: `{candidate.identity_status}`",
            "일치 근거: " + ", ".join(self.ports.safe_text(reason) for reason in candidate.match_reasons),
            "승인하면 기존 GameAccount·서클 포인트·원장은 유지되고 초기 서클 포인트는 지급되지 않습니다.",
        ]
        return self.ports.bounded_message(lines)

    async def _prepare_component(
        self,
        interaction: discord.Interaction,
        context: PlayerLinkInteractionContext,
        command_name: str,
    ) -> object | None:
        return await self.ports.prepare_component(
            interaction,
            context,
            command_name=command_name,
        )


def format_resolution_history(
    request: PlayerLinkRequestDTO,
    *,
    resolution: str,
    bounded_message: Callable[[list[str]], str],
    safe_text: Callable[[str], str],
) -> str:
    return bounded_message(
        [
            f"[기존 기록 연결 {resolution}] 요청 #{request.id}",
            f"PID: `{safe_text(request.submitted_uma_pid)}`",
            f"Discord 표시명: {safe_text(request.discord_nickname_snapshot)}",
            f"인게임명: {safe_text(request.submitted_ingame_name)}",
        ]
    )


async def publish_resolution_history(
    interaction: discord.Interaction,
    *,
    request: PlayerLinkRequestDTO,
    resolution: str,
    correlation_id: Callable[[object], str],
    bounded_message: Callable[[list[str]], str],
    safe_text: Callable[[str], str],
    logger: logging.Logger,
) -> None:
    channel = getattr(interaction, "channel", None)
    send = getattr(channel, "send", None)
    if not callable(send):
        logger.warning(
            "player-link resolution history skipped without channel correlation_id=%s request_id=%s resolution=%s",
            correlation_id(interaction),
            request.id,
            resolution,
        )
        return
    try:
        await send(
            format_resolution_history(
                request,
                resolution=resolution,
                bounded_message=bounded_message,
                safe_text=safe_text,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        log_sanitized_exception(
            logger,
            "player-link resolution history delivery failed correlation_id=%s request_id=%s resolution=%s",
            correlation_id(interaction),
            request.id,
            resolution,
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
