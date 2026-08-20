from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

import discord
from discord import app_commands

from umacircle_bot.adapters.discord.common import InteractionContext, run_blocking_application
from umacircle_bot.domain.errors import DomainError, MatchResultError
from umacircle_bot.logging_safety import log_sanitized_exception
from umacircle_bot.services.autocomplete_queries import (
    AutocompleteChoice,
    RoomRaceChoicePurpose,
)
from umacircle_bot.services.match_odds_snapshots import MatchOddsInput
from umacircle_bot.services.match_result_notifications import MatchResultNotificationDTO
from umacircle_bot.services.match_results import MatchResultOperationDTO
from umacircle_bot.services.match_settlement import MatchSettlementOperationDTO
from umacircle_bot.services.match_settlement_application import (
    ConfirmMatchSettlementApplicationCommand,
    PublishMatchResultApplicationCommand,
    RollbackMatchSettlementApplicationCommand,
)
from umacircle_bot.services.match_settlement_rollback import MatchSettlementRollbackDTO


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
        context: InteractionContext,
        *,
        command_name: str,
        defer: bool = True,
    ) -> object | None: ...


class AutocompleteAuthorizedPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> bool: ...


class QueryRaceChoicesPort(Protocol):
    def __call__(
        self,
        purpose: RoomRaceChoicePurpose,
        query: str,
        allowed_statuses: tuple[str, ...] | None,
    ) -> tuple[AutocompleteChoice, ...]: ...


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


@dataclass(frozen=True, slots=True)
class MatchStaffSettlementAdapterPorts:
    prepare_command: PrepareCommandPort
    prepare_component: PrepareComponentPort
    autocomplete_authorized: AutocompleteAuthorizedPort
    build_context: Callable[[discord.Interaction], InteractionContext]
    query_race_choices: QueryRaceChoicesPort
    query_result: Callable[[int, int | None], MatchResultOperationDTO]
    confirm_settlement: Callable[
        [ConfirmMatchSettlementApplicationCommand],
        MatchSettlementOperationDTO,
    ]
    rollback_settlement: Callable[
        [RollbackMatchSettlementApplicationCommand],
        MatchSettlementRollbackDTO,
    ]
    publish_result: Callable[
        [PublishMatchResultApplicationCommand],
        MatchResultNotificationDTO,
    ]
    send_user_error: SendUserErrorPort
    send_pre_modal_user_error: SendUserErrorPort
    send_internal_error: SendInternalErrorPort
    handle_modal_open_error: SendInternalErrorPort
    send_initial_response: SendInitialResponsePort
    send_followup: SendFollowupPort
    send_notification: SendNotificationPort
    correlation_id: Callable[[object], str]
    interaction_user_id: Callable[[object], str]
    safe_text: Callable[[str], str]
    bounded_message: Callable[[list[str]], str]
    logger: logging.Logger


class MatchStaffSettlementAdapter:
    def __init__(self, ports: MatchStaffSettlementAdapterPorts) -> None:
        self.ports = ports

    async def open_settlement(self, interaction: discord.Interaction) -> None:
        command_name = "match.staff.settlement"
        if await self.ports.prepare_command(interaction, command_name, defer=False) is None:
            return
        try:
            choices = await run_blocking_application(
                lambda: self.ports.query_race_choices(
                    RoomRaceChoicePurpose.RESULT,
                    "",
                    ("result_confirmed",),
                )
            )
            if not choices:
                await self.ports.send_initial_response(
                    interaction,
                    command_name,
                    "현재 이 결과 작업을 적용할 수 있는 룸매치 경기가 없습니다.",
                )
                return
            await interaction.response.send_modal(
                MatchSettlementModal(
                    adapter=self,
                    choices=choices,
                    context=self.ports.build_context(interaction),
                )
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_pre_modal_user_error(
                interaction,
                command_name,
                "경기 목록을 불러오지 못했습니다",
                exc,
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def show_rollback_preview(
        self,
        interaction: discord.Interaction,
        *,
        race_id: int,
        reason: str,
    ) -> None:
        command_name = "match.staff.settlement-rollback"
        if await self.ports.prepare_command(interaction, command_name) is None:
            return
        try:
            normalized_race_id = _positive_race_id(race_id)
            normalized_reason = _required_reason(reason)
            context = self.ports.build_context(interaction)
        except ValueError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "롤백 미리보기를 만들지 못했습니다",
                exc,
            )
            return
        await self.ports.send_followup(
            interaction,
            command_name,
            self.ports.bounded_message(
                [
                    "룸매치 정산 롤백 미리보기",
                    f"레이스: #{normalized_race_id}",
                    f"사유: {self.ports.safe_text(normalized_reason)}",
                    "확정하면 서클 포인트와 Rating을 보상 기록으로 되돌리고 레이스를 voided 처리합니다.",
                    "이미 공개한 Discord 결과는 자동 삭제되지 않으므로 필요하면 정정 공지를 게시하세요.",
                ]
            ),
            view=MatchSettlementRollbackConfirmView(
                adapter=self,
                race_id=normalized_race_id,
                reason=normalized_reason,
                context=context,
            ),
        )

    async def show_publish_preview(
        self,
        interaction: discord.Interaction,
        *,
        race_id: int,
    ) -> None:
        command_name = "match.staff.publish"
        if await self.ports.prepare_command(interaction, command_name) is None:
            return
        try:
            normalized_race_id = _positive_race_id(race_id)
            result = await run_blocking_application(lambda: self.ports.query_result(normalized_race_id, None))
            if result.race_status != "settled" or result.submission is None:
                raise MatchResultError("정산 완료된 현재 결과 revision이 필요합니다.")
            publication_status = result.publication.status if result.publication is not None else None
            if publication_status not in {None, "failed", "delivery_unknown"}:
                raise MatchResultError("현재 결과 공개가 이미 진행 중이거나 완료되었습니다.")
            context = self.ports.build_context(interaction)
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "공개 미리보기를 만들지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_followup(
            interaction,
            command_name,
            self.format_result(result, heading="룸매치 결과 공개 미리보기"),
            view=MatchPublishConfirmView(
                adapter=self,
                race_id=result.race_id,
                revision_number=result.submission.revision_number,
                authorize_delivery_unknown_retry=publication_status == "delivery_unknown",
                context=context,
            ),
        )

    async def autocomplete_publish(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        command_name = "match.staff.publish"
        if not await self.ports.autocomplete_authorized(interaction, command_name):
            return []
        try:
            choices = await run_blocking_application(
                lambda: self.ports.query_race_choices(
                    RoomRaceChoicePurpose.RESULT,
                    current,
                    ("settled",),
                )
            )
        except Exception:
            log_sanitized_exception(
                self.ports.logger,
                "discord autocomplete failed correlation_id=%s command=%s actor_id=%s",
                self.ports.correlation_id(interaction),
                command_name,
                self.ports.interaction_user_id(interaction),
            )
            return []
        return [app_commands.Choice(name=choice.label[:100], value=choice.value) for choice in choices[:25]]

    async def submit_settlement_preview(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        selected_values: tuple[str, ...],
        allowed_ids: frozenset[int],
        win_rate: object,
        quinella_rate: object,
        trio_rate: object,
        reason: object,
    ) -> None:
        command_name = "match.staff.settlement"
        if (
            await self.ports.prepare_component(
                interaction,
                context,
                command_name=command_name,
            )
            is None
        ):
            return
        try:
            race_id = _selected_modal_id(
                selected_values,
                allowed_ids=allowed_ids,
                field_name="대상 경기",
            )
            odds = parse_room_match_odds(
                win=win_rate,
                quinella=quinella_rate,
                trio=trio_rate,
            )
            view = MatchSettlementConfirmView(
                adapter=self,
                race_id=race_id,
                odds=odds,
                reason=optional_modal_text(reason),
                context=context,
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "정산 미리보기를 만들지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_followup(
            interaction,
            command_name,
            self.format_settlement_preview(race_id=race_id, odds=odds),
            view=view,
        )

    async def confirm_settlement(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        race_id: int,
        odds: tuple[MatchOddsInput, ...],
        reason: str | None,
    ) -> bool:
        command_name = "match.staff.settlement"
        if (
            await self.ports.prepare_component(
                interaction,
                context,
                command_name=command_name,
            )
            is None
        ):
            return False
        actor_id = str(interaction.user.id)
        interaction_id = self.ports.correlation_id(interaction)
        try:
            result = await run_blocking_application(
                lambda: self.ports.confirm_settlement(
                    ConfirmMatchSettlementApplicationCommand(
                        race_id=race_id,
                        odds=odds,
                        reason=reason,
                        actor_discord_user_id=actor_id,
                        interaction_id=interaction_id,
                    )
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "룸매치 정산을 확정하지 못했습니다",
                exc,
            )
            return False
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return False
        await self.send_settlement_result(interaction, command_name, result)
        return True

    async def confirm_rollback(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        race_id: int,
        reason: str,
    ) -> bool:
        command_name = "match.staff.settlement-rollback"
        if (
            await self.ports.prepare_component(
                interaction,
                context,
                command_name=command_name,
            )
            is None
        ):
            return False
        actor_id = str(interaction.user.id)
        interaction_id = self.ports.correlation_id(interaction)
        try:
            result = await run_blocking_application(
                lambda: self.ports.rollback_settlement(
                    RollbackMatchSettlementApplicationCommand(
                        race_id=race_id,
                        reason=reason,
                        actor_discord_user_id=actor_id,
                        interaction_id=interaction_id,
                    )
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "룸매치 정산을 롤백하지 못했습니다",
                exc,
            )
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
        else:
            await self.send_rollback_result(interaction, command_name, result)
        finally:
            await self.close_interaction_view(interaction, command_name)
        return True

    async def confirm_publish(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        race_id: int,
        revision_number: int,
        authorize_delivery_unknown_retry: bool,
    ) -> bool:
        command_name = "match.staff.publish"
        if (
            await self.ports.prepare_component(
                interaction,
                context,
                command_name=command_name,
            )
            is None
        ):
            return False
        actor_id = str(interaction.user.id)
        interaction_id = self.ports.correlation_id(interaction)
        try:
            result = await run_blocking_application(
                lambda: self.ports.publish_result(
                    PublishMatchResultApplicationCommand(
                        race_id=race_id,
                        revision_number=revision_number,
                        guild_id=str(context.guild_id),
                        actor_discord_user_id=actor_id,
                        interaction_id=interaction_id,
                        authorize_delivery_unknown_retry=authorize_delivery_unknown_retry,
                    )
                )
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "룸매치 결과를 공개하지 못했습니다",
                exc,
            )
            return False
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return False
        await self.ports.send_notification(interaction, command_name, result)
        return True

    async def cancel_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        command_name: str,
        content: str,
        close_view: bool = False,
    ) -> bool:
        if (
            await self.ports.prepare_component(
                interaction,
                context,
                command_name=command_name,
            )
            is None
        ):
            return False
        await self.ports.send_followup(interaction, command_name, content)
        if close_view:
            await self.close_interaction_view(interaction, command_name)
        return True

    async def send_settlement_result(
        self,
        interaction: discord.Interaction,
        command_name: str,
        result: MatchSettlementOperationDTO,
    ) -> None:
        self.ports.logger.info(
            "discord command succeeded correlation_id=%s command=%s audit_id=%s race_id=%s transaction_count=%s",
            self.ports.correlation_id(interaction),
            command_name,
            result.audit_id,
            result.race_id,
            len(result.transactions),
        )
        await self.ports.send_followup(
            interaction,
            command_name,
            self.ports.bounded_message(
                [
                    f"레이스 #{result.race_id} 정산 완료 / 상태 `{result.race_status}`",
                    f"odds snapshot #{result.odds_snapshot.id} / "
                    f"참가 인원 {result.odds_snapshot.settlement_participant_count}명 / "
                    f"배율 {result.odds_snapshot.payout_multiplier}",
                    f"Rating: {result.rating.grade} / 이벤트 {result.rating.event_count}건",
                    f"서클 포인트 정산 거래: {len(result.transactions)}건",
                ]
            ),
        )

    async def send_rollback_result(
        self,
        interaction: discord.Interaction,
        command_name: str,
        result: MatchSettlementRollbackDTO,
    ) -> None:
        self.ports.logger.info(
            "discord command succeeded correlation_id=%s command=%s audit_id=%s race_id=%s transaction_count=%s",
            self.ports.correlation_id(interaction),
            command_name,
            result.audit_id,
            result.race_id,
            len(result.transactions),
        )
        await self.ports.send_followup(
            interaction,
            command_name,
            self.ports.bounded_message(
                [
                    f"레이스 #{result.race_id} 정산 롤백 완료 / 상태 `{result.race_status}`",
                    f"서클 포인트 보상 거래: {len(result.transactions)}건",
                    "Rating 보상: "
                    f"기존 {len(result.reversed_rating_event_ids)}건 / "
                    f"보상 {len(result.compensating_rating_event_ids)}건",
                    "이미 공개한 결과가 있다면 정정 공지를 확인하세요.",
                ]
            ),
        )

    async def close_interaction_view(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> None:
        edit = getattr(getattr(interaction, "message", None), "edit", None)
        if not callable(edit):
            return
        try:
            await edit(view=None)
        except Exception:
            log_sanitized_exception(
                self.ports.logger,
                "discord interaction view close failed correlation_id=%s command=%s actor_id=%s",
                self.ports.correlation_id(interaction),
                command_name,
                self.ports.interaction_user_id(interaction),
            )

    def format_settlement_preview(
        self,
        *,
        race_id: int,
        odds: tuple[MatchOddsInput, ...],
    ) -> str:
        lines = [
            "룸매치 정산 확정 미리보기",
            f"레이스: #{race_id}",
            "아래 배율은 확정 시 immutable odds snapshot으로 저장됩니다.",
        ]
        if odds:
            lines.append("입력 배율:")
            lines.extend(f"- {item.bet_type}: {item.declared_payout_rate}" for item in odds)
        else:
            lines.append("입력 배율: 없음 (활성 베팅 승식이 없어야 합니다)")
        lines.append("확정하면 판정·Rating·서클 포인트 정산이 한 트랜잭션으로 처리됩니다.")
        return self.ports.bounded_message(lines)

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


class MatchSettlementModal(discord.ui.Modal, title="룸매치 정산 확정 미리보기"):
    def __init__(
        self,
        *,
        adapter: MatchStaffSettlementAdapter,
        choices: tuple[AutocompleteChoice, ...],
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._context = context
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = discord.ui.Select(
            custom_id="room-settlement-target",
            placeholder="정산할 결과 확정 경기 선택",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in choices],
            required=True,
        )
        self.win_rate = discord.ui.TextInput(
            custom_id="room-settlement-win-rate",
            label="단승 배율",
            placeholder="예: 4.00 (해당 승식에 베팅이 없으면 비움)",
            required=False,
            max_length=32,
        )
        self.quinella_rate = discord.ui.TextInput(
            custom_id="room-settlement-quinella-rate",
            label="연승 배율",
            placeholder="예: 8.50 (해당 승식에 베팅이 없으면 비움)",
            required=False,
            max_length=32,
        )
        self.trio_rate = discord.ui.TextInput(
            custom_id="room-settlement-trio-rate",
            label="삼쌍 배율",
            placeholder="예: 25.00 (해당 승식에 베팅이 없으면 비움)",
            required=False,
            max_length=32,
        )
        self.reason = discord.ui.TextInput(
            custom_id="room-settlement-reason",
            label="운영 메모 (선택)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 경기", component=self.target))
        self.add_item(self.win_rate)
        self.add_item(self.quinella_rate)
        self.add_item(self.trio_rate)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.submit_settlement_preview(
            interaction,
            context=self._context,
            selected_values=tuple(self.target.values),
            allowed_ids=self._allowed_ids,
            win_rate=self.win_rate.value,
            quinella_rate=self.quinella_rate.value,
            trio_rate=self.trio_rate.value,
            reason=self.reason.value,
        )


class MatchSettlementConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        adapter: MatchStaffSettlementAdapter,
        race_id: int,
        odds: tuple[MatchOddsInput, ...],
        reason: str | None,
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._race_id = race_id
        self._odds = odds
        self._reason = reason
        self._context = context

    @discord.ui.button(label="정산 확정", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await self._adapter.confirm_settlement(
            interaction,
            context=self._context,
            race_id=self._race_id,
            odds=self._odds,
            reason=self._reason,
        ):
            self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await self._adapter.cancel_confirmation(
            interaction,
            context=self._context,
            command_name="match.staff.settlement",
            content="룸매치 정산 확정을 취소했습니다.",
        ):
            self.stop()


class MatchSettlementRollbackConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        adapter: MatchStaffSettlementAdapter,
        race_id: int,
        reason: str,
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._race_id = race_id
        self._reason = reason
        self._context = context

    @discord.ui.button(label="정산 롤백 확정", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await self._adapter.confirm_rollback(
            interaction,
            context=self._context,
            race_id=self._race_id,
            reason=self._reason,
        ):
            self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await self._adapter.cancel_confirmation(
            interaction,
            context=self._context,
            command_name="match.staff.settlement-rollback",
            content="룸매치 정산 롤백을 취소했습니다.",
            close_view=True,
        ):
            self.stop()


class MatchPublishConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        adapter: MatchStaffSettlementAdapter,
        race_id: int,
        revision_number: int,
        authorize_delivery_unknown_retry: bool,
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._race_id = race_id
        self._revision_number = revision_number
        self._authorize_delivery_unknown_retry = authorize_delivery_unknown_retry
        self._context = context

    @discord.ui.button(label="결과 공개", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await self._adapter.confirm_publish(
            interaction,
            context=self._context,
            race_id=self._race_id,
            revision_number=self._revision_number,
            authorize_delivery_unknown_retry=self._authorize_delivery_unknown_retry,
        ):
            self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await self._adapter.cancel_confirmation(
            interaction,
            context=self._context,
            command_name="match.staff.publish",
            content="룸매치 결과 공개를 취소했습니다.",
        ):
            self.stop()


def optional_modal_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def parse_room_match_odds(
    *,
    win: object,
    quinella: object,
    trio: object,
) -> tuple[MatchOddsInput, ...]:
    values = (("win", win), ("quinella", quinella), ("trio", trio))
    odds: list[MatchOddsInput] = []
    for bet_type, value in values:
        text = optional_modal_text(value)
        if text is None:
            continue
        try:
            rate = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{bet_type} 배율은 0 이상의 Decimal 숫자여야 합니다.") from exc
        if not rate.is_finite() or rate < 0:
            raise ValueError(f"{bet_type} 배율은 0 이상의 유한 Decimal 숫자여야 합니다.")
        odds.append(MatchOddsInput(bet_type=bet_type, declared_payout_rate=rate))
    return tuple(odds)


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


def _positive_race_id(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("레이스 ID는 양의 정수여야 합니다.")
    return value


def _required_reason(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("롤백 사유를 입력해야 합니다.")
    normalized = value.strip()
    if len(normalized) > 255:
        raise ValueError("롤백 사유는 255자 이하여야 합니다.")
    return normalized
