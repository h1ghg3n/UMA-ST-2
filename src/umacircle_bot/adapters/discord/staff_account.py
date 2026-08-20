from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import discord

from umacircle_bot.adapters.discord.common import InteractionContext, run_blocking_application
from umacircle_bot.domain.errors import DomainError
from umacircle_bot.logging_safety import log_sanitized_exception
from umacircle_bot.services.dtos import CirclePointTransactionDTO, GameAccountDTO


class AutocompleteChoicePort(Protocol):
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


class PrepareComponentPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        context: InteractionContext,
        *,
        command_name: str,
        defer: bool = True,
    ) -> object | None: ...


class CirclePointMutationPort(Protocol):
    def __call__(
        self,
        game_account_id: int,
        amount: int,
        reason: str,
        actor_discord_user_id: str,
        interaction_id: str,
    ) -> tuple[CirclePointTransactionDTO, str]: ...


class IngameNameUpdatePort(Protocol):
    def __call__(
        self,
        game_account_id: int,
        ingame_name: str,
    ) -> GameAccountDTO: ...


class SendUserErrorPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        prefix: str,
        error: Exception,
    ) -> None: ...


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
class StaffAccountAdapterPorts:
    prepare_command: PrepareCommandPort
    prepare_component: PrepareComponentPort
    build_context: Callable[[discord.Interaction], InteractionContext]
    query_registered_accounts: Callable[[], tuple[AutocompleteChoicePort, ...]]
    grant_circle_points: CirclePointMutationPort
    adjust_circle_points: CirclePointMutationPort
    update_ingame_name: IngameNameUpdatePort
    circle_point_grant_modal_factory: Callable[
        [StaffAccountAdapter, tuple[AutocompleteChoicePort, ...], InteractionContext],
        discord.ui.Modal,
    ]
    circle_point_adjustment_modal_factory: Callable[
        [StaffAccountAdapter, int, str, InteractionContext], discord.ui.Modal
    ]
    ingame_name_modal_factory: Callable[[StaffAccountAdapter, int, str, InteractionContext], discord.ui.Modal]
    send_user_error: SendUserErrorPort
    send_pre_modal_user_error: SendUserErrorPort
    send_internal_error: SendInteractionCommandErrorPort
    handle_modal_open_error: SendInteractionCommandErrorPort
    send_initial_response: SendInitialResponsePort
    send_followup: SendFollowupPort
    correlation_id: Callable[[object], str]
    interaction_user_id: Callable[[object], str]
    safe_text: Callable[[str], str]
    logger: logging.Logger


class StaffAccountAdapter:
    def __init__(self, ports: StaffAccountAdapterPorts) -> None:
        self.ports = ports

    async def grant_circle_points(self, interaction: discord.Interaction) -> None:
        command_name = "staff.grant-circle-points"
        if await self.ports.prepare_command(interaction, command_name, defer=False) is None:
            return
        try:
            choices = await run_blocking_application(self.ports.query_registered_accounts)
            if not choices:
                await self.ports.send_initial_response(
                    interaction,
                    command_name,
                    "서클 포인트를 지급할 수 있는 등록 계정이 없습니다.",
                )
                return
            context = self.ports.build_context(interaction)
            await interaction.response.send_modal(
                self.ports.circle_point_grant_modal_factory(
                    self,
                    choices,
                    context,
                )
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_pre_modal_user_error(
                interaction,
                command_name,
                "지급 대상을 불러오지 못했습니다",
                exc,
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def adjust_circle_points(
        self,
        interaction: discord.Interaction,
        *,
        target_account_id: int,
    ) -> None:
        command_name = "staff.adjust-circle-points"
        if await self.ports.prepare_command(interaction, command_name, defer=False) is None:
            return
        try:
            game_account_id = _validated_game_account_id(target_account_id)
            context = self.ports.build_context(interaction)
            await interaction.response.send_modal(
                self.ports.circle_point_adjustment_modal_factory(
                    self,
                    game_account_id,
                    f"계정 #{game_account_id}",
                    context,
                )
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_pre_modal_user_error(
                interaction,
                command_name,
                "조정 대상을 확인하지 못했습니다",
                exc,
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def change_name(
        self,
        interaction: discord.Interaction,
        *,
        target_account_id: int,
    ) -> None:
        command_name = "staff.change-name"
        if await self.ports.prepare_command(interaction, command_name, defer=False) is None:
            return
        try:
            game_account_id = _validated_game_account_id(target_account_id)
            context = self.ports.build_context(interaction)
            await interaction.response.send_modal(
                self.ports.ingame_name_modal_factory(
                    self,
                    game_account_id,
                    f"계정 #{game_account_id}",
                    context,
                )
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_pre_modal_user_error(
                interaction,
                command_name,
                "변경 대상을 확인하지 못했습니다",
                exc,
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def submit_circle_point_grant(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        selected_values: tuple[str, ...],
        allowed_ids: frozenset[int],
        amount: str,
        reason: str,
    ) -> None:
        command_name = "staff.grant-circle-points"
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
            game_account_id = _selected_modal_id(
                selected_values,
                allowed_ids=allowed_ids,
                field_name="지급 대상",
            )
            actor_discord_user_id = str(interaction.user.id)
            interaction_id = self.ports.correlation_id(interaction)
            transaction, uma_pid = await run_blocking_application(
                lambda: self.ports.grant_circle_points(
                    game_account_id,
                    int(amount.strip()),
                    reason,
                    actor_discord_user_id,
                    interaction_id,
                )
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "지급하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        self.ports.logger.info(
            "discord command succeeded correlation_id=%s command=%s transaction_id=%s",
            self.ports.correlation_id(interaction),
            command_name,
            transaction.id,
        )
        await self.ports.send_followup(
            interaction,
            command_name,
            f"지급 #{transaction.id}: `{uma_pid}`에 서클 포인트 {transaction.amount}을 지급했습니다.",
        )

    async def submit_circle_point_adjustment(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        game_account_id: int,
        amount: str,
        reason: str,
    ) -> None:
        command_name = "staff.adjust-circle-points"
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
            actor_discord_user_id = str(interaction.user.id)
            interaction_id = self.ports.correlation_id(interaction)
            transaction, uma_pid = await run_blocking_application(
                lambda: self.ports.adjust_circle_points(
                    game_account_id,
                    int(amount.strip()),
                    reason,
                    actor_discord_user_id,
                    interaction_id,
                )
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "조정하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        self.ports.logger.info(
            "discord command succeeded correlation_id=%s command=%s transaction_id=%s",
            self.ports.correlation_id(interaction),
            command_name,
            transaction.id,
        )
        await self.ports.send_followup(
            interaction,
            command_name,
            f"조정 #{transaction.id}: `{uma_pid}`의 서클 포인트를 {transaction.amount:+d}만큼 조정했습니다.",
        )

    async def submit_ingame_name_update(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        game_account_id: int,
        ingame_name: str,
    ) -> None:
        command_name = "staff.change-name"
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
            account = await run_blocking_application(
                lambda: self.ports.update_ingame_name(
                    game_account_id,
                    ingame_name,
                )
            )
        except DomainError as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "인게임명을 변경하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_followup(
            interaction,
            command_name,
            f"PID `{account.uma_pid}`의 인게임명을 "
            f"{self.ports.safe_text(account.ingame_name or '-')}으로 변경했습니다.",
        )


class CirclePointGrantModal(discord.ui.Modal, title="서클 포인트 지급"):
    def __init__(
        self,
        *,
        adapter: StaffAccountAdapter,
        choices: tuple[AutocompleteChoicePort, ...],
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._context = context
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = discord.ui.Select(
            custom_id="point-grant-target",
            placeholder="지급 대상 선택",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=choice.label,
                    value=str(choice.value),
                )
                for choice in choices
            ],
            required=True,
        )
        self.amount = discord.ui.TextInput(
            custom_id="point-grant-amount",
            placeholder="지급할 서클 포인트",
            required=True,
            min_length=1,
            max_length=12,
        )
        self.reason = discord.ui.TextInput(
            custom_id="point-grant-reason",
            style=discord.TextStyle.paragraph,
            placeholder="지급 사유",
            required=True,
            min_length=1,
            max_length=255,
        )
        self.add_item(
            discord.ui.Label(
                text="지급 대상",
                description="PID와 표시명을 확인하고 선택하세요.",
                component=self.target,
            )
        )
        self.add_item(discord.ui.Label(text="서클 포인트 지급량", component=self.amount))
        self.add_item(discord.ui.Label(text="지급 사유 (필수)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.submit_circle_point_grant(
            interaction,
            context=self._context,
            selected_values=tuple(self.target.values),
            allowed_ids=self._allowed_ids,
            amount=str(self.amount.value),
            reason=str(self.reason.value),
        )

    async def on_error(self, interaction: discord.Interaction, _error: Exception) -> None:
        _log_modal_error(
            self._adapter,
            interaction,
            message="discord point grant modal failed correlation_id=%s actor_id=%s",
        )
        if not interaction.response.is_done():
            await self._adapter.ports.send_initial_response(
                interaction,
                "staff.grant-circle-points",
                f"내부 오류가 발생했습니다. 요청 ID: `{self._adapter.ports.correlation_id(interaction)}`",
            )


class CirclePointAdjustmentModal(discord.ui.Modal, title="서클 포인트 조정"):
    def __init__(
        self,
        *,
        adapter: StaffAccountAdapter,
        game_account_id: int,
        target_label: str,
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._context = context
        self._game_account_id = game_account_id
        self.amount = discord.ui.TextInput(
            custom_id="point-adjustment-amount",
            placeholder="서클 포인트 조정량 (+10 또는 -10)",
            required=True,
            min_length=1,
            max_length=12,
        )
        self.reason = discord.ui.TextInput(
            custom_id="point-adjustment-reason",
            style=discord.TextStyle.paragraph,
            placeholder="조정 사유",
            required=True,
            min_length=1,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text=f"서클 포인트 조정량 (+/-) · {target_label}", component=self.amount))
        self.add_item(discord.ui.Label(text="조정 사유 (필수)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.submit_circle_point_adjustment(
            interaction,
            context=self._context,
            game_account_id=self._game_account_id,
            amount=str(self.amount.value),
            reason=str(self.reason.value),
        )

    async def on_error(self, interaction: discord.Interaction, _error: Exception) -> None:
        _log_modal_error(
            self._adapter,
            interaction,
            message="discord point adjustment modal failed correlation_id=%s actor_id=%s",
        )
        if not interaction.response.is_done():
            await self._adapter.ports.send_initial_response(
                interaction,
                "staff.adjust-circle-points",
                f"내부 오류가 발생했습니다. 요청 ID: `{self._adapter.ports.correlation_id(interaction)}`",
            )


class IngameNameChangeModal(discord.ui.Modal, title="인게임명 변경"):
    def __init__(
        self,
        *,
        adapter: StaffAccountAdapter,
        game_account_id: int,
        target_label: str,
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._context = context
        self._game_account_id = game_account_id
        self.ingame_name = discord.ui.TextInput(
            custom_id="ingame-name-change-value",
            placeholder="새 인게임명",
            required=True,
            min_length=1,
            max_length=100,
        )
        self.add_item(discord.ui.Label(text=f"인게임명 · {target_label}", component=self.ingame_name))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.submit_ingame_name_update(
            interaction,
            context=self._context,
            game_account_id=self._game_account_id,
            ingame_name=str(self.ingame_name.value),
        )

    async def on_error(self, interaction: discord.Interaction, _error: Exception) -> None:
        _log_modal_error(
            self._adapter,
            interaction,
            message="discord ingame name change modal failed correlation_id=%s actor_id=%s",
        )
        if not interaction.response.is_done():
            await self._adapter.ports.send_initial_response(
                interaction,
                "staff.change-name",
                f"내부 오류가 발생했습니다. 요청 ID: `{self._adapter.ports.correlation_id(interaction)}`",
            )


def _validated_game_account_id(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("선택한 대상이 올바르지 않습니다.")
    return value


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
    adapter: StaffAccountAdapter,
    interaction: discord.Interaction,
    *,
    message: str,
) -> None:
    log_sanitized_exception(
        adapter.ports.logger,
        message,
        adapter.ports.correlation_id(interaction),
        adapter.ports.interaction_user_id(interaction),
    )
