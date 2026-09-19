"""Three consolidated Discord workflow panels for Circle Match staff actions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import discord
from discord import app_commands

from .common import (
    AuthorizeDiscordInteraction,
    PrepareDiscordCommand,
    correlation_id,
)
from .match_staff import (
    MatchStaffBettingCloseHandler,
    MatchStaffBettingOpenHandler,
    MatchStaffCancellationHandler,
    MatchStaffInteractionContext,
    MatchStaffResultConfirmationHandler,
    MatchStaffResultPublicationHandler,
    MatchStaffResultReviewHandler,
    MatchStaffResultSubmissionHandler,
    MatchStaffSettlementHandler,
    MatchStaffSettlementRollbackHandler,
)
from .match_staff_entries import MatchEntryDiscordAdapter
from .match_staff_odds import MatchOddsModeDiscordAdapter
from .match_staff_setup import MatchSetupDiscordAdapter
from .strings import match_staff_workflows as copy

logger = logging.getLogger(__name__)

_COMPONENT_TIMEOUT_SECONDS = 600.0


class MatchStaffPanelKind(StrEnum):
    """The three public slash leaves below ``/match staff``."""

    RACE = "race"
    RESULT = "result"
    SETTLEMENT = "settlement"


def _command_name(kind: MatchStaffPanelKind) -> str:
    return f"match.staff.{kind.value}"


def _panel_catalog(kind: MatchStaffPanelKind) -> tuple[tuple[str, str, str], ...]:
    if kind == MatchStaffPanelKind.RACE:
        return copy.RACE_ACTIONS
    if kind == MatchStaffPanelKind.RESULT:
        return copy.RESULT_ACTIONS
    return copy.SETTLEMENT_ACTIONS


def _panel_title(kind: MatchStaffPanelKind) -> str:
    if kind == MatchStaffPanelKind.RACE:
        return copy.RACE_PANEL_TITLE
    if kind == MatchStaffPanelKind.RESULT:
        return copy.RESULT_PANEL_TITLE
    return copy.SETTLEMENT_PANEL_TITLE


def _transition_error(kind: MatchStaffPanelKind) -> str:
    if kind == MatchStaffPanelKind.RACE:
        return copy.RACE_TRANSITION_ERROR
    if kind == MatchStaffPanelKind.RESULT:
        return copy.RESULT_TRANSITION_ERROR
    return copy.SETTLEMENT_TRANSITION_ERROR


def _terminal_layout(content: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(content)))
    return view


class MatchStaffTargetHandler(Protocol):
    """Existing narrow adapter target query used only to populate one Select."""

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...


class MatchStaffPanelActionButton(discord.ui.Button[discord.ui.LayoutView]):
    """One discoverable workflow action."""

    def __init__(
        self,
        *,
        adapter: MatchStaffWorkflowDiscordAdapter,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
        action: str,
        label: str,
        row: int,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._kind = kind
        self._action = action
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary
            if action in {"create", "submit", "settle"}
            else discord.ButtonStyle.secondary,
            custom_id=f"match-staff-{kind.value}-{action}",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.select_action(
            interaction,
            context=self._context,
            kind=self._kind,
            action=self._action,
            source_view=self.view,
        )


class MatchStaffPanelCloseButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: MatchStaffWorkflowDiscordAdapter,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
        row: int,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._kind = kind
        super().__init__(
            label=copy.PANEL_CLOSE_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id=f"match-staff-{kind.value}-close-panel",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.close_panel(
            interaction,
            context=self._context,
            kind=self._kind,
            source_view=self.view,
        )


class MatchStaffPanelView(discord.ui.LayoutView):
    """Top-level action panel for exactly one staff slash leaf."""

    def __init__(
        self,
        *,
        adapter: MatchStaffWorkflowDiscordAdapter,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(f"{_panel_title(kind)}\n{copy.PANEL_GUIDANCE}"))
        action_rows: list[discord.ui.ActionRow] = []
        catalog = _panel_catalog(kind)
        for offset in range(0, len(catalog), 5):
            row_index = len(action_rows)
            action_row = discord.ui.ActionRow()
            for action, label, _description in catalog[offset : offset + 5]:
                action_row.add_item(
                    MatchStaffPanelActionButton(
                        adapter=adapter,
                        context=context,
                        kind=kind,
                        action=action,
                        label=label,
                        row=row_index,
                    )
                )
            action_rows.append(action_row)
        close_row = discord.ui.ActionRow()
        close_row.add_item(
            MatchStaffPanelCloseButton(
                adapter=adapter,
                context=context,
                kind=kind,
                row=len(action_rows),
            )
        )
        for action_row in action_rows:
            container.add_item(action_row)
        container.add_item(close_row)
        self.add_item(container)


class MatchStaffTargetSelect(discord.ui.Select[discord.ui.LayoutView]):
    """Choose one target for an existing narrow action adapter."""

    def __init__(
        self,
        *,
        adapter: MatchStaffWorkflowDiscordAdapter,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
        action: str,
        choices: tuple[app_commands.Choice[int], ...],
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._kind = kind
        self._action = action
        super().__init__(
            placeholder=copy.TARGET_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=choice.name[:100], value=str(choice.value)) for choice in choices],
            custom_id=f"match-staff-{kind.value}-{action}-target",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            match_id = int(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(interaction, copy.TARGET_LOAD_ERROR)
            return
        await self._adapter.select_target(
            interaction,
            context=self._context,
            kind=self._kind,
            action=self._action,
            match_id=match_id,
            source_view=self.view,
        )


class MatchStaffTargetBackButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: MatchStaffWorkflowDiscordAdapter,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._kind = kind
        super().__init__(
            label=copy.PANEL_BACK_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"match-staff-{kind.value}-target-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.return_to_panel(
            interaction,
            context=self._context,
            kind=self._kind,
            source_view=self.view,
        )


class MatchStaffTargetView(discord.ui.LayoutView):
    """Bounded target selector; canonical eligibility is still rechecked later."""

    def __init__(
        self,
        *,
        adapter: MatchStaffWorkflowDiscordAdapter,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
        action: str,
        choices: tuple[app_commands.Choice[int], ...],
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        label = next(label for candidate, label, _ in _panel_catalog(kind) if candidate == action)
        container = discord.ui.Container(discord.ui.TextDisplay(f"## {label}\n대상 경기를 선택해 주세요."))
        select_row = discord.ui.ActionRow()
        select_row.add_item(
            MatchStaffTargetSelect(
                adapter=adapter,
                context=context,
                kind=kind,
                action=action,
                choices=choices,
            )
        )
        back_row = discord.ui.ActionRow()
        back_row.add_item(
            MatchStaffTargetBackButton(
                adapter=adapter,
                context=context,
                kind=kind,
            )
        )
        container.add_item(select_row)
        container.add_item(back_row)
        self.add_item(container)


class MatchStaffReasonModal(discord.ui.Modal):
    """Action-specific optional/required reason collection before existing Preview."""

    def __init__(
        self,
        *,
        adapter: MatchStaffWorkflowDiscordAdapter,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
        action: str,
        match_id: int,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        super().__init__(title=copy.REASON_MODAL_TITLES[action], timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._kind = kind
        self._action = action
        self._match_id = match_id
        self._source_view = source_view
        self.reason = discord.ui.TextInput(
            label=copy.REASON_MODAL_LABELS[action],
            style=discord.TextStyle.paragraph,
            required=action == "rollback",
            min_length=1 if action == "rollback" else None,
            max_length=255,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self.reason.value).strip() or None
        await self._adapter.dispatch_target(
            interaction,
            context=self._context,
            kind=self._kind,
            action=self._action,
            match_id=self._match_id,
            reason=reason,
            source_view=self._source_view,
        )


@dataclass(frozen=True, slots=True)
class MatchStaffWorkflowDiscordAdapter:
    """Route panel choices to existing narrow Application-backed adapters."""

    setup_adapter: MatchSetupDiscordAdapter
    entry_adapter: MatchEntryDiscordAdapter
    betting_open_adapter: MatchStaffBettingOpenHandler
    betting_close_adapter: MatchStaffBettingCloseHandler
    cancellation_adapter: MatchStaffCancellationHandler
    result_submission_adapter: MatchStaffResultSubmissionHandler
    result_review_adapter: MatchStaffResultReviewHandler
    result_confirmation_adapter: MatchStaffResultConfirmationHandler
    settlement_adapter: MatchStaffSettlementHandler
    settlement_rollback_adapter: MatchStaffSettlementRollbackHandler
    publication_adapter: MatchStaffResultPublicationHandler
    odds_mode_adapter: MatchOddsModeDiscordAdapter
    prepare_command: PrepareDiscordCommand
    authorize_interaction: AuthorizeDiscordInteraction

    async def start_panel(
        self,
        interaction: discord.Interaction,
        *,
        kind: MatchStaffPanelKind,
    ) -> None:
        if not await self.prepare_command(interaction, _command_name(kind), ephemeral=True):
            return
        try:
            context = MatchStaffInteractionContext.from_interaction(interaction)
            await self._edit_deferred(
                interaction,
                view=MatchStaffPanelView(adapter=self, context=context, kind=kind),
            )
        except (TypeError, ValueError):
            await self._edit_deferred(interaction, content=copy.BOUND_INTERACTION_ERROR)

    async def select_action(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
        action: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if kind == MatchStaffPanelKind.RACE and action == "create":
            if not await self._authorize_bound(interaction, context=context, kind=kind):
                return
            await self.setup_adapter.start_creation(
                interaction,
                context=context,
                source_view=source_view,
            )
            return
        if kind == MatchStaffPanelKind.RACE and action == "edit":
            if not await self._authorize_bound(interaction, context=context, kind=kind):
                return
            await self.setup_adapter.show_edit_targets(
                interaction,
                context=context,
                source_view=source_view,
            )
            return
        if kind == MatchStaffPanelKind.RACE and action == "odds":
            if not await self._authorize_bound(interaction, context=context, kind=kind):
                return
            await self.odds_mode_adapter.show_status(
                interaction,
                navigation=self,
                context=context,
                source_view=source_view,
            )
            return
        handler = self._target_handler(kind=kind, action=action)
        if handler is None:
            if not await self._authorize_bound(interaction, context=context, kind=kind):
                return
            await self.send_component_error(interaction, copy.INVALID_ACTION)
            return
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            kind=kind,
            response_kind="target-list",
        ):
            return
        try:
            choices = tuple(await handler.autocomplete_targets(interaction, ""))
        except Exception:
            self._log_application_failure(interaction, kind=kind)
            await self.send_component_error(interaction, copy.TARGET_LOAD_ERROR)
            return
        if not choices:
            await self.send_component_error(interaction, copy.NO_TARGETS)
            return
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(
            interaction,
            kind=kind,
            view=MatchStaffTargetView(
                adapter=self,
                context=context,
                kind=kind,
                action=action,
                choices=choices,
            ),
        )

    async def select_target(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
        action: str,
        match_id: int,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context, kind=kind):
            return
        if action in copy.REASON_MODAL_TITLES:
            await interaction.response.send_modal(
                MatchStaffReasonModal(
                    adapter=self,
                    context=context,
                    kind=kind,
                    action=action,
                    match_id=match_id,
                    source_view=source_view,
                )
            )
            return
        await self.dispatch_target(
            interaction,
            context=context,
            kind=kind,
            action=action,
            match_id=match_id,
            reason=None,
            source_view=source_view,
        )

    async def dispatch_target(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
        action: str,
        match_id: int,
        reason: str | None,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not context.matches(interaction):
            await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return
        if kind == MatchStaffPanelKind.RACE and action == "entries":
            if source_view is None:
                await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
                return
            await self.entry_adapter.start_replacement(
                interaction,
                match_id=match_id,
                reason=None,
                context=context,
                source_view=source_view,
            )
        elif kind == MatchStaffPanelKind.RACE and action == "open":
            if source_view is None:
                await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
                return
            await self.betting_open_adapter.preview_opening(
                interaction,
                match_id=match_id,
                context=context,
                source_view=source_view,
            )
        elif kind == MatchStaffPanelKind.RACE and action == "close":
            if source_view is None:
                await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
                return
            await self.betting_close_adapter.preview_close(
                interaction,
                match_id=match_id,
                context=context,
                source_view=source_view,
            )
        elif kind == MatchStaffPanelKind.RACE and action == "cancel":
            if source_view is None:
                await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
                return
            await self.cancellation_adapter.preview_cancellation(
                interaction,
                match_id=match_id,
                reason=reason,
                context=context,
                source_view=source_view,
            )
        elif kind == MatchStaffPanelKind.RESULT and action == "submit":
            if source_view is None:
                await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
                return
            await self.result_submission_adapter.start_submission(
                interaction,
                match_id=match_id,
                reason=reason,
                context=context,
                source_view=source_view,
            )
        elif kind == MatchStaffPanelKind.RESULT and action == "review":
            if source_view is None:
                await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
                return
            await self.result_review_adapter.start_review(
                interaction,
                match_id=match_id,
                context=context,
                source_view=source_view,
            )
        elif kind == MatchStaffPanelKind.RESULT and action == "confirm":
            if source_view is None:
                await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
                return
            await self.result_confirmation_adapter.start_confirmation(
                interaction,
                match_id=match_id,
                context=context,
                source_view=source_view,
            )
        elif kind == MatchStaffPanelKind.SETTLEMENT and action == "settle":
            if source_view is None:
                await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
                return
            await self.settlement_adapter.preview_settlement(
                interaction,
                match_id=match_id,
                reason=reason,
                context=context,
                source_view=source_view,
            )
        elif kind == MatchStaffPanelKind.SETTLEMENT and action == "rollback":
            if source_view is None:
                await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
                return
            if reason is None:
                await self.send_component_error(interaction, copy.REASON_MODAL_LABELS[action])
                return
            await self.settlement_rollback_adapter.preview_rollback(
                interaction,
                match_id=match_id,
                reason=reason,
                context=context,
                source_view=source_view,
            )
        elif kind == MatchStaffPanelKind.SETTLEMENT and action == "recover":
            if source_view is None:
                await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
                return
            await self.publication_adapter.publish_result(
                interaction,
                match_id=match_id,
                context=context,
                source_view=source_view,
            )
        else:
            await self.send_component_error(interaction, copy.INVALID_ACTION)
            return

    async def return_to_panel(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            kind=kind,
            response_kind="panel-return",
        ):
            return
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(
            interaction,
            kind=kind,
            view=MatchStaffPanelView(adapter=self, context=context, kind=kind),
        )

    async def return_to_race_panel(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        await self.return_to_panel(
            interaction,
            context=context,
            kind=MatchStaffPanelKind.RACE,
            source_view=source_view,
        )

    async def close_panel(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            kind=kind,
            response_kind="panel-close",
        ):
            return
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(
            interaction,
            kind=kind,
            view=_terminal_layout(copy.PANEL_CANCELLED),
        )

    def _target_handler(
        self,
        *,
        kind: MatchStaffPanelKind,
        action: str,
    ) -> MatchStaffTargetHandler | None:
        handlers: dict[tuple[MatchStaffPanelKind, str], MatchStaffTargetHandler] = {
            (MatchStaffPanelKind.RACE, "entries"): self.entry_adapter,
            (MatchStaffPanelKind.RACE, "open"): self.betting_open_adapter,
            (MatchStaffPanelKind.RACE, "close"): self.betting_close_adapter,
            (MatchStaffPanelKind.RACE, "cancel"): self.cancellation_adapter,
            (MatchStaffPanelKind.RESULT, "submit"): self.result_submission_adapter,
            (MatchStaffPanelKind.RESULT, "review"): self.result_review_adapter,
            (MatchStaffPanelKind.RESULT, "confirm"): self.result_confirmation_adapter,
            (MatchStaffPanelKind.SETTLEMENT, "settle"): self.settlement_adapter,
            (MatchStaffPanelKind.SETTLEMENT, "rollback"): self.settlement_rollback_adapter,
            (MatchStaffPanelKind.SETTLEMENT, "recover"): self.publication_adapter,
        }
        return handlers.get((kind, action))

    async def _authorize_bound(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
    ) -> bool:
        if not context.matches(interaction):
            await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return False
        return await self.authorize_interaction(interaction, _command_name(kind))

    async def _prepare_bound_update(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        kind: MatchStaffPanelKind,
        response_kind: str,
    ) -> bool:
        if not context.matches(interaction):
            await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return False
        try:
            await interaction.response.defer(thinking=False)
        except Exception as error:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=%s-defer error_type=%s",
                correlation_id(interaction),
                _command_name(kind),
                response_kind,
                type(error).__name__,
            )
            return False
        return await self.authorize_interaction(interaction, _command_name(kind))

    async def _edit_layout(
        self,
        interaction: discord.Interaction,
        *,
        kind: MatchStaffPanelKind,
        view: discord.ui.LayoutView,
    ) -> None:
        try:
            await interaction.edit_original_response(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception as error:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=layout error_type=%s",
                correlation_id(interaction),
                _command_name(kind),
                type(error).__name__,
            )
            await self.send_component_error(interaction, _transition_error(kind))

    @staticmethod
    async def _edit_deferred(
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        view: discord.ui.LayoutView | None = None,
    ) -> None:
        try:
            await interaction.edit_original_response(
                content=content,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=match.staff response_kind=deferred",
                correlation_id(interaction),
            )

    @staticmethod
    async def send_component_error(interaction: discord.Interaction, content: str) -> None:
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
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=match.staff response_kind=component-error",
                correlation_id(interaction),
            )

    @staticmethod
    def _log_application_failure(interaction: object, *, kind: MatchStaffPanelKind) -> None:
        logger.error(
            "Discord application call failed correlation_id=%s command=%s",
            correlation_id(interaction),
            _command_name(kind),
        )


class MatchStaffCommandGroup(app_commands.Group):
    """Expose exactly three workflow panels below ``/match staff``."""

    def __init__(self, *, adapter: MatchStaffWorkflowDiscordAdapter) -> None:
        super().__init__(name="staff", description="룸매치 운영 workflow를 관리합니다.")
        self._adapter = adapter

    @app_commands.command(name="race", description=copy.RACE_COMMAND_DESCRIPTION)
    async def race(self, interaction: discord.Interaction) -> None:
        await self._adapter.start_panel(interaction, kind=MatchStaffPanelKind.RACE)

    @app_commands.command(name="result", description=copy.RESULT_COMMAND_DESCRIPTION)
    async def result(self, interaction: discord.Interaction) -> None:
        await self._adapter.start_panel(interaction, kind=MatchStaffPanelKind.RESULT)

    @app_commands.command(name="settlement", description=copy.SETTLEMENT_COMMAND_DESCRIPTION)
    async def settlement(self, interaction: discord.Interaction) -> None:
        await self._adapter.start_panel(interaction, kind=MatchStaffPanelKind.SETTLEMENT)
