"""Discord presentation for staff Persona status management."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import discord

from uma_st2.application.identity import (
    ChangePersonaStatus,
    StaffPersonaStatusAuditError,
    StaffPersonaStatusCommands,
    StaffPersonaStatusConcurrentConflictError,
    StaffPersonaStatusIdempotencyConflictError,
    StaffPersonaStatusNoChangeError,
    StaffPersonaStatusNotFoundError,
    StaffPersonaStatusPreview,
    StaffPersonaStatusQueries,
    StaffPersonaStatusStaleError,
    StaffPersonaStatusState,
)
from uma_st2.domain.identity import PersonaStatus

from .common import (
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    PrepareDiscordCommand,
    buttonless_terminal_layout,
    correlation_id,
    run_blocking_application,
    safe_discord_text,
)
from .staff_persona_common import StaffPersonaInteractionContext
from .strings import staff_persona as copy

logger = logging.getLogger(__name__)

_COMMAND_NAME = "staff.persona"
_COMPONENT_TIMEOUT_SECONDS = 600.0
_RESTRICTING_STATUSES = frozenset(
    {
        PersonaStatus.PENDING_APPROVAL,
        PersonaStatus.EXPELLED,
        PersonaStatus.WITHDRAWN,
    }
)


class StaffPersonaStatusNavigation(Protocol):
    async def show_panel_component(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        source_view: discord.ui.LayoutView | None,
    ) -> None: ...


class StaffPersonaStatusBackButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        navigation: StaffPersonaStatusNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
    ) -> None:
        self._navigation = navigation
        self._context = context
        self._persona_id = persona_id
        super().__init__(
            label=copy.BACK_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-status-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._navigation.show_panel_component(
            interaction,
            context=self._context,
            selected_persona_id=self._persona_id,
            source_view=self.view,
        )


class StaffPersonaStatusSelect(discord.ui.Select[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaStatusDiscordAdapter,
        navigation: StaffPersonaStatusNavigation,
        context: StaffPersonaInteractionContext,
        state: StaffPersonaStatusState,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._state = state
        options = [
            discord.SelectOption(
                label=copy.persona_status_label(status),
                value=status.value,
                description=_status_option_description(status),
            )
            for status in PersonaStatus
            if status is not state.status
        ]
        super().__init__(
            placeholder=copy.STATUS_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=options,
            custom_id="staff-persona-status-select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_reason_input(
            interaction,
            navigation=self._navigation,
            context=self._context,
            state=self._state,
            desired_status=PersonaStatus(self.values[0]),
            source_view=self.view,
        )


class StaffPersonaStatusView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffPersonaStatusDiscordAdapter,
        navigation: StaffPersonaStatusNavigation,
        context: StaffPersonaInteractionContext,
        state: StaffPersonaStatusState,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_persona_status_management(state)))
        select_row = discord.ui.ActionRow()
        select_row.add_item(
            StaffPersonaStatusSelect(
                adapter=adapter,
                navigation=navigation,
                context=context,
                state=state,
            )
        )
        action_row = discord.ui.ActionRow()
        action_row.add_item(
            StaffPersonaStatusBackButton(
                navigation=navigation,
                context=context,
                persona_id=state.persona_id,
            )
        )
        container.add_item(select_row)
        container.add_item(action_row)
        self.add_item(container)


class StaffPersonaStatusReasonModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        adapter: StaffPersonaStatusDiscordAdapter,
        navigation: StaffPersonaStatusNavigation,
        context: StaffPersonaInteractionContext,
        state: StaffPersonaStatusState,
        desired_status: PersonaStatus,
        source_view: discord.ui.LayoutView | None,
        current_reason: str | None = None,
    ) -> None:
        super().__init__(title=copy.STATUS_REASON_MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._state = state
        self._desired_status = desired_status
        self._source_view = source_view
        self.reason = discord.ui.TextInput(
            label=copy.STATUS_REASON_LABEL,
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=255,
            default=current_reason,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_preview(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._state.persona_id,
            desired_status=self._desired_status,
            reason=str(self.reason.value),
            source_view=self._source_view,
        )


class StaffPersonaStatusConfirmButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaStatusDiscordAdapter,
        context: StaffPersonaInteractionContext,
        preview: StaffPersonaStatusPreview,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        style = (
            discord.ButtonStyle.danger
            if preview.desired_status in _RESTRICTING_STATUSES
            else discord.ButtonStyle.success
        )
        super().__init__(
            label=copy.STATUS_CONFIRM_LABEL,
            style=style,
            custom_id="staff-persona-status-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.change_status(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self.view,
        )


class StaffPersonaStatusEditReasonButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaStatusDiscordAdapter,
        navigation: StaffPersonaStatusNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffPersonaStatusPreview,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._preview = preview
        super().__init__(
            label=copy.STATUS_REENTER_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-status-reason-edit",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_reason_input(
            interaction,
            navigation=self._navigation,
            context=self._context,
            state=self._preview.state,
            desired_status=self._preview.desired_status,
            source_view=self.view,
            current_reason=self._preview.reason,
        )


class StaffPersonaStatusReselectButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaStatusDiscordAdapter,
        navigation: StaffPersonaStatusNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._persona_id = persona_id
        super().__init__(
            label=copy.STATUS_RESELECT_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-status-reselect",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_status(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._persona_id,
            source_view=self.view,
        )


class StaffPersonaStatusConfirmView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffPersonaStatusDiscordAdapter,
        navigation: StaffPersonaStatusNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffPersonaStatusPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_persona_status_preview(preview)))
        row = discord.ui.ActionRow()
        row.add_item(
            StaffPersonaStatusConfirmButton(
                adapter=adapter,
                context=context,
                preview=preview,
            )
        )
        row.add_item(
            StaffPersonaStatusEditReasonButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                preview=preview,
            )
        )
        row.add_item(
            StaffPersonaStatusReselectButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                persona_id=preview.state.persona_id,
            )
        )
        container.add_item(row)
        self.add_item(container)


@dataclass(frozen=True, slots=True)
class StaffPersonaStatusDiscordAdapter:
    queries: StaffPersonaStatusQueries
    commands: StaffPersonaStatusCommands
    prepare_command: PrepareDiscordCommand
    authorize_interaction: AuthorizeDiscordInteraction
    run_application: BlockingApplicationRunner = run_blocking_application

    async def show_status(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffPersonaStatusNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            state = await self.run_application(
                lambda: self.queries.get_state(
                    guild_id=str(context.guild_id),
                    persona_id=persona_id,
                )
            )
            replacement = StaffPersonaStatusView(
                adapter=self,
                navigation=navigation,
                context=context,
                state=state,
            )
        except StaffPersonaStatusNotFoundError:
            await self._send_component_error(interaction, copy.STATUS_PERSONA_UNAVAILABLE)
            return
        except Exception:
            self._log_failure(interaction, operation="status-view")
            await self._send_component_error(interaction, copy.internal_error(correlation_id(interaction)))
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="status-view",
        )

    async def open_reason_input(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffPersonaStatusNavigation,
        context: StaffPersonaInteractionContext,
        state: StaffPersonaStatusState,
        desired_status: PersonaStatus,
        source_view: discord.ui.LayoutView | None,
        current_reason: str | None = None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                StaffPersonaStatusReasonModal(
                    adapter=self,
                    navigation=navigation,
                    context=context,
                    state=state,
                    desired_status=desired_status,
                    source_view=source_view,
                    current_reason=current_reason,
                )
            )
        except Exception:
            self._log_failure(interaction, operation="status-reason")
            await self._send_component_error(interaction, copy.internal_error(correlation_id(interaction)))

    async def show_preview(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffPersonaStatusNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        desired_status: PersonaStatus,
        reason: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            preview = await self.run_application(
                lambda: self.queries.get_preview(
                    guild_id=str(context.guild_id),
                    persona_id=persona_id,
                    desired_status=desired_status,
                    reason=reason,
                )
            )
            replacement = StaffPersonaStatusConfirmView(
                adapter=self,
                navigation=navigation,
                context=context,
                preview=preview,
            )
        except StaffPersonaStatusNotFoundError:
            await self._send_component_error(interaction, copy.STATUS_PERSONA_UNAVAILABLE)
            return
        except StaffPersonaStatusNoChangeError:
            await self._send_component_error(interaction, copy.STATUS_NO_CHANGE)
            return
        except (TypeError, ValueError):
            await self._send_component_error(interaction, copy.STATUS_INVALID_INPUT)
            return
        except Exception:
            self._log_failure(interaction, operation="status-preview")
            await self._send_component_error(interaction, copy.internal_error(correlation_id(interaction)))
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="status-preview",
        )

    async def change_status(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        preview: StaffPersonaStatusPreview,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not context.matches(interaction):
            await self._send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            key = correlation_id(interaction)
            result = await self.run_application(
                lambda: self.commands.change(
                    ChangePersonaStatus(
                        guild_id=str(context.guild_id),
                        target_persona_id=preview.state.persona_id,
                        desired_status=preview.desired_status,
                        reason=preview.reason,
                        updated_by_discord_user_id=str(context.user_id),
                        expected_target_fingerprint=preview.state.state_fingerprint,
                        idempotency_key=key,
                        correlation_id=key,
                    )
                )
            )
            content = copy.format_persona_status_receipt(result)
        except StaffPersonaStatusNotFoundError:
            content = copy.STATUS_PERSONA_UNAVAILABLE
        except StaffPersonaStatusNoChangeError:
            content = copy.STATUS_NO_CHANGE
        except StaffPersonaStatusStaleError:
            content = copy.STATUS_STALE
        except StaffPersonaStatusIdempotencyConflictError:
            content = copy.IDEMPOTENCY_CONFLICT
        except StaffPersonaStatusConcurrentConflictError:
            content = copy.STATUS_CONCURRENT_CONFLICT
        except StaffPersonaStatusAuditError:
            self._log_failure(interaction, operation="status-audit")
            content = copy.internal_error(correlation_id(interaction))
        except (TypeError, ValueError):
            content = copy.STATUS_INVALID_INPUT
        except Exception:
            self._log_failure(interaction, operation="status-final")
            content = copy.internal_error(correlation_id(interaction))
        await self._edit_deferred(interaction, content)
        self._stop(source_view)

    async def _authorize_bound(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
    ) -> bool:
        if not context.matches(interaction):
            await self._send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return False
        return await self.authorize_interaction(interaction, _COMMAND_NAME)

    @staticmethod
    async def _send_component_error(interaction: discord.Interaction, content: str) -> None:
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
                "Discord Staff Persona status component response failed correlation_id=%s",
                correlation_id(interaction),
            )

    @staticmethod
    async def _edit_deferred(interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.edit_original_response(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=buttonless_terminal_layout(
                    content,
                    timeout_seconds=_COMPONENT_TIMEOUT_SECONDS,
                ),
            )
        except Exception:
            logger.error(
                "Discord Staff Persona status deferred response failed correlation_id=%s",
                correlation_id(interaction),
            )

    async def _replace_component_layout(
        self,
        interaction: discord.Interaction,
        *,
        view: discord.ui.LayoutView,
        source_view: discord.ui.LayoutView | None,
        operation: str,
    ) -> None:
        self._stop(source_view)
        try:
            await interaction.response.edit_message(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_failure(interaction, operation=operation)
            await self._send_component_error(interaction, copy.PANEL_TRANSITION_ERROR)

    @staticmethod
    def _stop(view: discord.ui.LayoutView | None) -> None:
        if view is not None:
            view.stop()

    @staticmethod
    def _log_failure(interaction: object, *, operation: str) -> None:
        logger.error(
            "Discord Staff Persona status failed correlation_id=%s command=%s operation=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
            safe_discord_text(operation, limit=64),
        )


def _status_option_description(status: PersonaStatus) -> str:
    if status is PersonaStatus.NORMAL:
        return "정상 status gate로 복귀합니다."
    if status is PersonaStatus.WARNING:
        return "경고를 표시하되 member mutation을 허용합니다."
    if status is PersonaStatus.PENDING_APPROVAL:
        return "History read만 유지하고 새 member mutation을 막습니다."
    if status is PersonaStatus.EXPELLED:
        return "제명 상태로 새 member mutation을 막습니다."
    return "탈퇴 상태로 새 member mutation을 막습니다."
