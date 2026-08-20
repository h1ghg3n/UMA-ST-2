from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import discord

from umacircle_bot.adapters.discord.common import InteractionContext, run_blocking_application
from umacircle_bot.domain.errors import DomainError, MatchResultError
from umacircle_bot.domain.match_results import MatchResultInput
from umacircle_bot.logging_safety import log_sanitized_exception
from umacircle_bot.services.match_result_notifications import MatchResultNotificationDTO
from umacircle_bot.services.match_results import MatchResultOperationDTO


class RaceChoicePort(Protocol):
    value: int
    label: str


class PrepareCommandPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *,
        defer: bool = True,
    ) -> object | None: ...


class PrepareBoundComponentPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        context: InteractionContext,
        *,
        command_name: str,
        defer: bool = True,
    ) -> object | None: ...


class SendUserErrorPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        prefix: str,
        error: Exception,
    ) -> None: ...


class SendCommandErrorPort(Protocol):
    async def __call__(self, interaction: discord.Interaction, command_name: str) -> None: ...


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


class SendNotificationPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        result: MatchResultNotificationDTO,
    ) -> None: ...


class SubmitResultPort(Protocol):
    def __call__(
        self,
        *,
        race_id: int,
        match_type: str,
        results: tuple[MatchResultInput, ...],
        reason: str | None,
        guild_id: str,
        actor_discord_user_id: str,
        interaction_id: str,
    ) -> MatchResultNotificationDTO: ...


class ReviewResultPort(Protocol):
    def __call__(
        self,
        *,
        race_id: int,
        revision_number: int,
        reason: str | None,
        guild_id: str,
        actor_discord_user_id: str,
        interaction_id: str,
    ) -> MatchResultNotificationDTO: ...


class CorrectResultPort(Protocol):
    def __call__(
        self,
        *,
        race_id: int,
        revision_number: int,
        match_type: str,
        results: tuple[MatchResultInput, ...],
        reason: str | None,
        guild_id: str,
        actor_discord_user_id: str,
        interaction_id: str,
    ) -> MatchResultNotificationDTO: ...


class RejectResultPort(Protocol):
    def __call__(
        self,
        *,
        race_id: int,
        revision_number: int,
        reason: str,
        guild_id: str,
        actor_discord_user_id: str,
        interaction_id: str,
    ) -> MatchResultNotificationDTO: ...


class ConfirmResultPort(Protocol):
    def __call__(
        self,
        *,
        race_id: int,
        revision_number: int,
        reason: str | None,
        guild_id: str,
        actor_discord_user_id: str,
        interaction_id: str,
    ) -> MatchResultNotificationDTO: ...


class QueryResultPort(Protocol):
    def __call__(
        self,
        *,
        race_id: int,
        revision_number: int | None = None,
    ) -> MatchResultOperationDTO: ...


@dataclass(frozen=True, slots=True)
class MatchStaffResultAdapterPorts:
    prepare_command: PrepareCommandPort
    prepare_bound_component: PrepareBoundComponentPort
    build_context: Callable[[discord.Interaction], InteractionContext]
    query_race_choices: Callable[[tuple[str, ...]], tuple[RaceChoicePort, ...]]
    submit_result: SubmitResultPort
    review_result: ReviewResultPort
    correct_result: CorrectResultPort
    reject_result: RejectResultPort
    confirm_result: ConfirmResultPort
    query_result: QueryResultPort
    send_notification: SendNotificationPort
    send_user_error: SendUserErrorPort
    send_pre_modal_user_error: SendUserErrorPort
    send_internal_error: SendCommandErrorPort
    handle_modal_open_error: SendCommandErrorPort
    send_initial_response: SendInitialResponsePort
    send_followup: SendFollowupPort
    correlation_id: Callable[[object], str]
    interaction_user_id: Callable[[object], str]
    safe_text: Callable[[str], str]
    bounded_message: Callable[[list[str]], str]
    logger: logging.Logger


class MatchStaffResultAdapter:
    """Persistence-free Room Match staff result interaction owner."""

    def __init__(self, ports: MatchStaffResultAdapterPorts) -> None:
        self.ports = ports

    async def result_submit(self, interaction: discord.Interaction) -> None:
        await self._open_target_modal(interaction, action="submit")

    async def result_review(self, interaction: discord.Interaction) -> None:
        await self._open_target_modal(interaction, action="review")

    async def result_correct(self, interaction: discord.Interaction) -> None:
        await self._open_target_modal(interaction, action="correct")

    async def result_reject(self, interaction: discord.Interaction) -> None:
        await self._open_target_modal(interaction, action="reject")

    async def result_confirm(self, interaction: discord.Interaction) -> None:
        await self._open_target_modal(interaction, action="confirm")

    async def result_show(
        self,
        interaction: discord.Interaction,
        race_id: int,
        revision_number: int | None,
    ) -> None:
        command_name = "match.staff.result-show"
        if await self.ports.prepare_command(interaction, command_name) is None:
            return
        try:
            result = await run_blocking_application(
                lambda: self.ports.query_result(
                    race_id=int(race_id),
                    revision_number=revision_number,
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "룸매치 결과를 조회하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        self.ports.logger.info(
            "discord command succeeded correlation_id=%s command=%s race_id=%s submission_id=%s publication_id=%s",
            self.ports.correlation_id(interaction),
            command_name,
            result.race_id,
            result.submission.id if result.submission is not None else None,
            result.publication.id if result.publication is not None else None,
        )
        await self.ports.send_followup(
            interaction,
            command_name,
            self.format_result(result),
        )

    async def submit_entry(
        self,
        interaction: discord.Interaction,
        *,
        command_name: str,
        context: InteractionContext,
        selected_values: tuple[str, ...],
        allowed_ids: frozenset[int],
        match_type_values: tuple[str, ...],
        entry_order: str,
        reason_value: str,
        correcting: bool,
    ) -> None:
        if not await self._prepare_component(interaction, context, command_name):
            return
        try:
            race_id = _selected_modal_id(
                selected_values,
                allowed_ids=allowed_ids,
                field_name="대상 경기",
            )
            if len(match_type_values) != 1:
                raise ValueError("경기 유형을 하나 선택해야 합니다.")
            match_type = match_type_values[0]
            results = parse_match_result_order(entry_order)
            reason = _optional_modal_text(reason_value)
            scalar = self._scalar_context(interaction, context)
            if correcting:
                current = await run_blocking_application(
                    lambda: self.ports.query_result(race_id=race_id, revision_number=None)
                )
                if current.submission is None:
                    raise MatchResultError("정정할 룸매치 결과 revision이 없습니다.")
                revision_number = current.submission.revision_number
                result = await run_blocking_application(
                    lambda: self.ports.correct_result(
                        race_id=race_id,
                        revision_number=revision_number,
                        match_type=match_type,
                        results=results,
                        reason=reason,
                        guild_id=scalar.guild_id,
                        actor_discord_user_id=scalar.actor_id,
                        interaction_id=scalar.interaction_id,
                    )
                )
            else:
                result = await run_blocking_application(
                    lambda: self.ports.submit_result(
                        race_id=race_id,
                        match_type=match_type,
                        results=results,
                        reason=reason,
                        guild_id=scalar.guild_id,
                        actor_discord_user_id=scalar.actor_id,
                        interaction_id=scalar.interaction_id,
                    )
                )
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "룸매치 결과를 저장하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_notification(interaction, command_name, result)

    async def submit_order(
        self,
        interaction: discord.Interaction,
        *,
        command_name: str,
        context: InteractionContext,
        race_id: int,
        match_type: str,
        reason: str | None,
        revision_number: int | None,
        entry_order: str,
    ) -> None:
        if not await self._prepare_component(interaction, context, command_name):
            return
        try:
            results = parse_match_result_order(entry_order)
            scalar = self._scalar_context(interaction, context)
            if revision_number is None:
                result = await run_blocking_application(
                    lambda: self.ports.submit_result(
                        race_id=race_id,
                        match_type=match_type,
                        results=results,
                        reason=reason,
                        guild_id=scalar.guild_id,
                        actor_discord_user_id=scalar.actor_id,
                        interaction_id=scalar.interaction_id,
                    )
                )
            else:
                result = await run_blocking_application(
                    lambda: self.ports.correct_result(
                        race_id=race_id,
                        revision_number=revision_number,
                        match_type=match_type,
                        results=results,
                        reason=reason or "",
                        guild_id=scalar.guild_id,
                        actor_discord_user_id=scalar.actor_id,
                        interaction_id=scalar.interaction_id,
                    )
                )
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "룸매치 결과를 저장하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_notification(interaction, command_name, result)

    async def submit_decision(
        self,
        interaction: discord.Interaction,
        *,
        action: Literal["review", "reject", "confirm"],
        context: InteractionContext,
        selected_values: tuple[str, ...],
        allowed_ids: frozenset[int],
        reason_value: str,
    ) -> None:
        command_name = f"match.staff.result-{action}"
        if not await self._prepare_component(interaction, context, command_name):
            return
        try:
            race_id = _selected_modal_id(
                selected_values,
                allowed_ids=allowed_ids,
                field_name="대상 경기",
            )
            reason = reason_value if action == "reject" else _optional_modal_text(reason_value)
            current = await run_blocking_application(
                lambda: self.ports.query_result(race_id=race_id, revision_number=None)
            )
            if current.submission is None:
                raise MatchResultError("처리할 룸매치 결과 revision이 없습니다.")
            revision_number = current.submission.revision_number
            if action == "confirm":
                await self.ports.send_followup(
                    interaction,
                    command_name,
                    self.format_result(current, heading="룸매치 결과 확정 미리보기"),
                    view=MatchResultConfirmView(
                        adapter=self,
                        result=current,
                        reason=reason,
                        context=context,
                    ),
                )
                return
            scalar = self._scalar_context(interaction, context)
            if action == "review":
                result = await run_blocking_application(
                    lambda: self.ports.review_result(
                        race_id=race_id,
                        revision_number=revision_number,
                        reason=reason,
                        guild_id=scalar.guild_id,
                        actor_discord_user_id=scalar.actor_id,
                        interaction_id=scalar.interaction_id,
                    )
                )
            else:
                result = await run_blocking_application(
                    lambda: self.ports.reject_result(
                        race_id=race_id,
                        revision_number=revision_number,
                        reason=reason,
                        guild_id=scalar.guild_id,
                        actor_discord_user_id=scalar.actor_id,
                        interaction_id=scalar.interaction_id,
                    )
                )
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "룸매치 결과를 처리하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_notification(interaction, command_name, result)

    async def confirm_preview(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        race_id: int,
        revision_number: int,
        reason: str | None,
    ) -> bool:
        command_name = "match.staff.result-confirm"
        if not await self._prepare_component(interaction, context, command_name):
            return False
        try:
            scalar = self._scalar_context(interaction, context)
            result = await run_blocking_application(
                lambda: self.ports.confirm_result(
                    race_id=race_id,
                    revision_number=revision_number,
                    reason=reason,
                    guild_id=scalar.guild_id,
                    actor_discord_user_id=scalar.actor_id,
                    interaction_id=scalar.interaction_id,
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "룸매치 결과를 확정하지 못했습니다",
                exc,
            )
            return False
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return False
        await self.ports.send_notification(interaction, command_name, result)
        return True

    async def cancel_preview(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
    ) -> bool:
        command_name = "match.staff.result-confirm"
        if not await self._prepare_component(interaction, context, command_name):
            return False
        await self.ports.send_followup(interaction, command_name, "룸매치 결과 확정을 취소했습니다.")
        return True

    def format_result(
        self,
        result: MatchResultOperationDTO,
        *,
        heading: str = "룸매치 결과",
    ) -> str:
        lines = [
            heading,
            f"레이스: #{result.race_id} {self.ports.safe_text(result.race_name)}",
            f"레이스 상태: {result.race_status}",
        ]
        if result.submission is None:
            lines.append("결과 제출: 없음")
        else:
            submission = result.submission
            lines.extend(
                [
                    f"결과 제출: #{submission.id}",
                    f"revision: {submission.revision_number}",
                    f"매치 유형: {submission.match_type}",
                    f"제출 상태: {submission.status}",
                ]
            )
        if result.results:
            lines.append("도착 순서:")
            for item in sorted(result.results, key=lambda value: value.rank):
                character = f" / {self.ports.safe_text(item.character_name)}" if item.character_name is not None else ""
                lines.append(f"{item.rank}위: 엔트리 #{item.entry_number}{character}")
                details: list[str] = []
                if item.character_evaluation_rank is not None:
                    details.append(f"평가 {self.ports.safe_text(item.character_evaluation_rank)}")
                if item.popularity_rank is not None:
                    details.append(f"인기 {item.popularity_rank}위")
                if item.finish_time_ms is not None:
                    details.append(f"{item.finish_time_ms}ms")
                if item.finish_margin_text is not None:
                    details.append(f"착차 {self.ports.safe_text(item.finish_margin_text)}")
                if details:
                    lines.append("  " + " · ".join(details))
        else:
            lines.append("도착 순서: 없음")
        if result.publication is not None:
            lines.append(f"게시 intent: #{result.publication.id} / 상태 {result.publication.status}")
        return self.ports.bounded_message(lines)

    async def _open_target_modal(
        self,
        interaction: discord.Interaction,
        *,
        action: Literal["submit", "review", "correct", "reject", "confirm"],
    ) -> None:
        command_name = f"match.staff.result-{action}"
        if await self.ports.prepare_command(interaction, command_name, defer=False) is None:
            return
        allowed_statuses = ("betting_closed",) if action == "submit" else ("result_review",)
        try:
            choices = await run_blocking_application(lambda: self.ports.query_race_choices(allowed_statuses))
            if not choices:
                await self.ports.send_initial_response(
                    interaction,
                    command_name,
                    "현재 이 결과 작업을 적용할 수 있는 룸매치 경기가 없습니다.",
                )
                return
            context = self.ports.build_context(interaction)
            if action in {"submit", "correct"}:
                modal: discord.ui.Modal = MatchResultEntryModal(
                    adapter=self,
                    command_name=command_name,
                    choices=choices,
                    context=context,
                    correcting=action == "correct",
                )
            else:
                modal = MatchResultDecisionModal(
                    adapter=self,
                    action=action,
                    choices=choices,
                    context=context,
                )
            await interaction.response.send_modal(modal)
        except (DomainError, ValueError) as exc:
            await self.ports.send_pre_modal_user_error(
                interaction,
                command_name,
                "경기 목록을 불러오지 못했습니다",
                exc,
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def _prepare_component(
        self,
        interaction: discord.Interaction,
        context: InteractionContext,
        command_name: str,
    ) -> bool:
        return (
            await self.ports.prepare_bound_component(
                interaction,
                context,
                command_name=command_name,
            )
            is not None
        )

    def _scalar_context(
        self,
        interaction: discord.Interaction,
        context: InteractionContext,
    ) -> _ScalarApplicationContext:
        return _ScalarApplicationContext(
            guild_id=str(context.guild_id),
            actor_id=str(interaction.user.id),
            interaction_id=self.ports.correlation_id(interaction),
        )


@dataclass(frozen=True, slots=True)
class _ScalarApplicationContext:
    guild_id: str
    actor_id: str
    interaction_id: str


class MatchResultOrderModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        adapter: MatchStaffResultAdapter,
        command_name: str,
        race_id: int,
        match_type: str,
        reason: str | None,
        context: InteractionContext,
        revision_number: int | None = None,
    ) -> None:
        title = "룸매치 결과 정정" if revision_number is not None else "룸매치 결과 제출"
        super().__init__(title=title, timeout=600)
        self._adapter = adapter
        self._command_name = command_name
        self._race_id = race_id
        self._match_type = match_type
        self._reason = reason
        self._context = context
        self._revision_number = revision_number
        self.entry_order = discord.ui.TextInput(
            label="도착 순서와 결과 상세",
            placeholder="번호 | 평가 | 인기 | 기록ms | 착차\n3 | UE5 | 1 | 121234 | -",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(self.entry_order)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.submit_order(
            interaction,
            command_name=self._command_name,
            context=self._context,
            race_id=self._race_id,
            match_type=self._match_type,
            reason=self._reason,
            revision_number=self._revision_number,
            entry_order=str(self.entry_order.value),
        )

    async def on_error(self, interaction: discord.Interaction, _error: Exception) -> None:
        _log_modal_error(self._adapter, interaction, self._command_name)
        if not interaction.response.is_done():
            await self._adapter.ports.send_initial_response(
                interaction,
                self._command_name,
                f"내부 오류가 발생했습니다. 요청 ID: `{self._adapter.ports.correlation_id(interaction)}`",
            )


class MatchResultEntryModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        adapter: MatchStaffResultAdapter,
        command_name: str,
        choices: tuple[RaceChoicePort, ...],
        context: InteractionContext,
        correcting: bool,
    ) -> None:
        super().__init__(title="룸매치 결과 정정" if correcting else "룸매치 결과 제출", timeout=600)
        self._adapter = adapter
        self._command_name = command_name
        self._context = context
        self._correcting = correcting
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = discord.ui.Select(
            custom_id="room-result-entry-target",
            placeholder="결과를 입력할 경기 선택",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in choices],
            required=True,
        )
        self.match_type = discord.ui.Select(
            custom_id="room-result-entry-match-type",
            placeholder="경기 유형 선택",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="정기 룸매치", value="regular_room_match"),
                discord.SelectOption(label="비정기 룸매치", value="irregular_room_match"),
            ],
            required=True,
        )
        self.entry_order = discord.ui.TextInput(
            custom_id="room-result-entry-order",
            placeholder="번호 | 평가 | 인기 | 기록ms | 착차\n3 | UE5 | 1 | 121234 | -",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.reason = discord.ui.TextInput(
            custom_id="room-result-entry-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 정정 관련 특이사항" if correcting else "선택: 결과 입력 관련 특이사항",
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 경기", component=self.target))
        self.add_item(discord.ui.Label(text="경기 유형", component=self.match_type))
        self.add_item(discord.ui.Label(text="도착 순서와 결과 상세", component=self.entry_order))
        self.add_item(
            discord.ui.Label(
                text="운영 메모 (선택)",
                component=self.reason,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.submit_entry(
            interaction,
            command_name=self._command_name,
            context=self._context,
            selected_values=tuple(self.target.values),
            allowed_ids=self._allowed_ids,
            match_type_values=tuple(self.match_type.values),
            entry_order=str(self.entry_order.value),
            reason_value=str(self.reason.value),
            correcting=self._correcting,
        )

    async def on_error(self, interaction: discord.Interaction, _error: Exception) -> None:
        _log_modal_error(self._adapter, interaction, self._command_name)
        if not interaction.response.is_done():
            await self._adapter.ports.send_initial_response(
                interaction,
                self._command_name,
                f"내부 오류가 발생했습니다. 요청 ID: `{self._adapter.ports.correlation_id(interaction)}`",
            )


class MatchResultDecisionModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        adapter: MatchStaffResultAdapter,
        action: Literal["review", "reject", "confirm"],
        choices: tuple[RaceChoicePort, ...],
        context: InteractionContext,
    ) -> None:
        action_label = {"review": "검토 완료", "reject": "반려", "confirm": "확정 미리보기"}[action]
        super().__init__(title=f"룸매치 결과 {action_label}", timeout=600)
        self._adapter = adapter
        self._action = action
        self._context = context
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = discord.ui.Select(
            custom_id=f"room-result-{action}-target",
            placeholder=f"{action_label}할 경기 선택",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in choices],
            required=True,
        )
        self.reason = discord.ui.TextInput(
            custom_id=f"room-result-{action}-reason",
            style=discord.TextStyle.paragraph,
            placeholder="반려 사유" if action == "reject" else f"선택: {action_label} 관련 특이사항",
            required=action == "reject",
            min_length=1 if action == "reject" else None,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 경기", component=self.target))
        self.add_item(
            discord.ui.Label(
                text="반려 사유 (필수)" if action == "reject" else "운영 메모 (선택)",
                component=self.reason,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.submit_decision(
            interaction,
            action=self._action,
            context=self._context,
            selected_values=tuple(self.target.values),
            allowed_ids=self._allowed_ids,
            reason_value=str(self.reason.value),
        )


class MatchResultConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        adapter: MatchStaffResultAdapter,
        result: MatchResultOperationDTO,
        reason: str | None,
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        if result.submission is None:
            raise ValueError("확인할 결과 revision이 없습니다.")
        self._adapter = adapter
        self._race_id = result.race_id
        self._revision_number = result.submission.revision_number
        self._reason = reason
        self._context = context

    @discord.ui.button(label="결과 확정", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await self._adapter.confirm_preview(
            interaction,
            context=self._context,
            race_id=self._race_id,
            revision_number=self._revision_number,
            reason=self._reason,
        ):
            self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await self._adapter.cancel_preview(interaction, context=self._context):
            self.stop()


def parse_match_result_order(value: str) -> tuple[MatchResultInput, ...]:
    if not isinstance(value, str):
        raise ValueError("결과 순서는 줄 단위 텍스트여야 합니다.")
    raw_lines = value.splitlines()
    if not raw_lines or any(not line.strip() for line in raw_lines):
        raise ValueError("1위부터 엔트리 번호를 빈 줄 없이 입력해 주세요.")
    results: list[MatchResultInput] = []
    for rank, line in enumerate(raw_lines, start=1):
        parts = tuple(part.strip() for part in line.split("|"))
        if len(parts) not in {1, 5}:
            raise ValueError("결과는 '번호 | 평가 | 인기 | 기록ms | 착차' 형식이어야 합니다.")
        if not re.fullmatch(r"[1-9][0-9]*", parts[0]):
            raise ValueError("엔트리 번호는 0으로 시작하지 않는 양의 정수여야 합니다.")
        details: tuple[str, ...] = ("", "", "", "") if len(parts) == 1 else parts[1:]
        evaluation, popularity, finish_time, margin = (None if part in {"", "-"} else part for part in details)
        if popularity is not None and not re.fullmatch(r"[1-9][0-9]*", popularity):
            raise ValueError("인기 순위는 양의 정수 또는 - 이어야 합니다.")
        if finish_time is not None and not re.fullmatch(r"[1-9][0-9]*", finish_time):
            raise ValueError("기록은 양의 정수 millisecond 또는 - 이어야 합니다.")
        results.append(
            MatchResultInput(
                entry_number=int(parts[0]),
                rank=rank,
                character_evaluation_rank=evaluation,
                popularity_rank=int(popularity) if popularity is not None else None,
                finish_time_ms=int(finish_time) if finish_time is not None else None,
                finish_margin_text=margin,
            )
        )
    entry_numbers = [item.entry_number for item in results]
    if len(entry_numbers) != len(set(entry_numbers)):
        raise ValueError("결과 순서에 중복 엔트리 번호가 있습니다.")
    return tuple(results)


def create_match_staff_result_adapter(
    ports: MatchStaffResultAdapterPorts,
) -> MatchStaffResultAdapter:
    return MatchStaffResultAdapter(ports)


def _optional_modal_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _selected_modal_id(
    selected_values: tuple[str, ...],
    *,
    allowed_ids: frozenset[int],
    field_name: str,
) -> int:
    if len(selected_values) != 1:
        raise ValueError(f"{field_name}을(를) 하나 선택해야 합니다.")
    try:
        selected_id = int(selected_values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 선택값이 올바르지 않습니다.") from exc
    if selected_id not in allowed_ids:
        raise ValueError(f"{field_name} 선택값이 현재 허용되지 않습니다.")
    return selected_id


def _log_modal_error(
    adapter: MatchStaffResultAdapter,
    interaction: discord.Interaction,
    command_name: str,
) -> None:
    log_sanitized_exception(
        adapter.ports.logger,
        "discord modal failed correlation_id=%s command=%s actor_id=%s",
        adapter.ports.correlation_id(interaction),
        command_name,
        adapter.ports.interaction_user_id(interaction),
    )
