"""Private `/staff persona` GameAccount owner-correction workflow."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import discord

from uma_st2.application.identity import (
    CorrectGameAccountOwner,
    StaffGameAccountOwnerCorrectionAuditError,
    StaffGameAccountOwnerCorrectionCommands,
    StaffGameAccountOwnerCorrectionConcurrentConflictError,
    StaffGameAccountOwnerCorrectionIdempotencyConflictError,
    StaffGameAccountOwnerCorrectionPreview,
    StaffGameAccountOwnerCorrectionQueries,
    StaffGameAccountOwnerCorrectionSameOwnerError,
    StaffGameAccountOwnerCorrectionStaleError,
    StaffGameAccountOwnerCorrectionUnavailableError,
    StaffGameAccountOwnerCorrectionWalletMissingError,
)
from uma_st2.domain.identity import GameRegion

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


class StaffPersonaPanelNavigation(Protocol):
    async def show_panel_component(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        source_view: discord.ui.LayoutView | None,
    ) -> None: ...


class StaffGameAccountOwnerCorrectionBackButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
    ) -> None:
        self._navigation = navigation
        self._context = context
        self._persona_id = persona_id
        super().__init__(
            label=copy.BACK_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-owner-correction-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._navigation.show_panel_component(
            interaction,
            context=self._context,
            selected_persona_id=self._persona_id,
            source_view=self.view,
        )


class StaffGameAccountOwnerCorrectionRegionSelect(discord.ui.Select[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffGameAccountOwnerCorrectionDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        persona_display_name: str,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._persona_id = persona_id
        self._persona_display_name = persona_display_name
        super().__init__(
            placeholder=copy.OWNER_CORRECTION_REGION_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="KR", value=GameRegion.KR.value),
                discord.SelectOption(label="JP", value=GameRegion.JP.value),
            ],
            custom_id="staff-persona-owner-correction-region",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_input_modal(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._persona_id,
            persona_display_name=self._persona_display_name,
            game_region=GameRegion(self.values[0]),
            source_view=self.view,
        )


class StaffGameAccountOwnerCorrectionRegionView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffGameAccountOwnerCorrectionDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        persona_display_name: str,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(
            discord.ui.TextDisplay(copy.format_owner_correction_region(persona_display_name=persona_display_name))
        )
        select_row = discord.ui.ActionRow()
        select_row.add_item(
            StaffGameAccountOwnerCorrectionRegionSelect(
                adapter=adapter,
                navigation=navigation,
                context=context,
                persona_id=persona_id,
                persona_display_name=persona_display_name,
            )
        )
        action_row = discord.ui.ActionRow()
        action_row.add_item(
            StaffGameAccountOwnerCorrectionBackButton(
                navigation=navigation,
                context=context,
                persona_id=persona_id,
            )
        )
        container.add_item(select_row)
        container.add_item(action_row)
        self.add_item(container)


class StaffGameAccountOwnerCorrectionInputModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        adapter: StaffGameAccountOwnerCorrectionDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        persona_display_name: str,
        game_region: GameRegion,
        source_view: discord.ui.LayoutView | None,
        current: StaffGameAccountOwnerCorrectionPreview | None = None,
    ) -> None:
        super().__init__(title=copy.OWNER_CORRECTION_MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._persona_id = persona_id
        self._persona_display_name = persona_display_name
        self._game_region = game_region
        self._source_view = source_view
        self.pid = discord.ui.TextInput(
            label=copy.OWNER_CORRECTION_PID_LABEL,
            min_length=1,
            max_length=32,
            default=None if current is None else current.state.uma_pid,
        )
        self.evidence_reason = discord.ui.TextInput(
            label=copy.OWNER_CORRECTION_REASON_LABEL,
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=255,
            default=None if current is None else current.evidence_reason,
        )
        self.add_item(self.pid)
        self.add_item(self.evidence_reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_preview(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._persona_id,
            game_region=self._game_region,
            uma_pid=str(self.pid.value),
            evidence_reason=str(self.evidence_reason.value),
            source_view=self._source_view,
        )


class StaffGameAccountOwnerCorrectionConfirmButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffGameAccountOwnerCorrectionDiscordAdapter,
        context: StaffPersonaInteractionContext,
        preview: StaffGameAccountOwnerCorrectionPreview,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        super().__init__(
            label=copy.OWNER_CORRECTION_CONFIRM_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id="staff-persona-owner-correction-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.correct(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self.view,
        )


class StaffGameAccountOwnerCorrectionReenterButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffGameAccountOwnerCorrectionDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffGameAccountOwnerCorrectionPreview,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._preview = preview
        super().__init__(
            label=copy.OWNER_CORRECTION_REENTER_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-owner-correction-reenter",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        state = self._preview.state
        await self._adapter.open_input_modal(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=state.target_persona_id,
            persona_display_name=state.target_persona_display_name,
            game_region=state.game_region,
            source_view=self.view,
            current=self._preview,
        )


class StaffGameAccountOwnerCorrectionConfirmView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffGameAccountOwnerCorrectionDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffGameAccountOwnerCorrectionPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_owner_correction_preview(preview)))
        row = discord.ui.ActionRow()
        row.add_item(
            StaffGameAccountOwnerCorrectionConfirmButton(
                adapter=adapter,
                context=context,
                preview=preview,
            )
        )
        row.add_item(
            StaffGameAccountOwnerCorrectionReenterButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                preview=preview,
            )
        )
        row.add_item(
            StaffGameAccountOwnerCorrectionBackButton(
                navigation=navigation,
                context=context,
                persona_id=preview.state.target_persona_id,
            )
        )
        container.add_item(row)
        self.add_item(container)


@dataclass(frozen=True, slots=True)
class StaffGameAccountOwnerCorrectionDiscordAdapter:
    queries: StaffGameAccountOwnerCorrectionQueries
    commands: StaffGameAccountOwnerCorrectionCommands
    prepare_command: PrepareDiscordCommand
    authorize_interaction: AuthorizeDiscordInteraction
    run_application: BlockingApplicationRunner = run_blocking_application

    async def show_region(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        persona_display_name: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            replacement = StaffGameAccountOwnerCorrectionRegionView(
                adapter=self,
                navigation=navigation,
                context=context,
                persona_id=persona_id,
                persona_display_name=persona_display_name,
            )
        except Exception:
            self._log_failure(interaction, operation="owner-correction-region")
            await self._send_component_error(
                interaction,
                copy.owner_correction_internal_error(correlation_id(interaction)),
            )
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="owner-correction-region",
        )

    async def open_input_modal(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        persona_display_name: str,
        game_region: GameRegion,
        source_view: discord.ui.LayoutView | None,
        current: StaffGameAccountOwnerCorrectionPreview | None = None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                StaffGameAccountOwnerCorrectionInputModal(
                    adapter=self,
                    navigation=navigation,
                    context=context,
                    persona_id=persona_id,
                    persona_display_name=persona_display_name,
                    game_region=game_region,
                    source_view=source_view,
                    current=current,
                )
            )
        except Exception:
            self._log_failure(interaction, operation="owner-correction-input")
            await self._send_component_error(
                interaction,
                copy.owner_correction_internal_error(correlation_id(interaction)),
            )

    async def show_preview(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        game_region: GameRegion,
        uma_pid: str,
        evidence_reason: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            preview = await self.run_application(
                lambda: self.queries.get_preview(
                    guild_id=str(context.guild_id),
                    target_persona_id=persona_id,
                    game_region=game_region,
                    uma_pid=uma_pid,
                    evidence_reason=evidence_reason,
                )
            )
            replacement = StaffGameAccountOwnerCorrectionConfirmView(
                adapter=self,
                navigation=navigation,
                context=context,
                preview=preview,
            )
        except StaffGameAccountOwnerCorrectionUnavailableError:
            await self._send_component_error(interaction, copy.OWNER_CORRECTION_UNAVAILABLE)
            return
        except StaffGameAccountOwnerCorrectionSameOwnerError:
            await self._send_component_error(interaction, copy.OWNER_CORRECTION_SAME_OWNER)
            return
        except StaffGameAccountOwnerCorrectionWalletMissingError:
            await self._send_component_error(interaction, copy.OWNER_CORRECTION_WALLET_MISSING)
            return
        except (TypeError, ValueError):
            await self._send_component_error(interaction, copy.OWNER_CORRECTION_INVALID_INPUT)
            return
        except Exception:
            self._log_failure(interaction, operation="owner-correction-preview")
            await self._send_component_error(
                interaction,
                copy.owner_correction_internal_error(correlation_id(interaction)),
            )
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="owner-correction-preview",
        )

    async def correct(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        preview: StaffGameAccountOwnerCorrectionPreview,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not context.matches(interaction):
            await self._send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            interaction_key = correlation_id(interaction)
            state = preview.state
            result = await self.run_application(
                lambda: self.commands.correct(
                    CorrectGameAccountOwner(
                        guild_id=str(context.guild_id),
                        target_persona_id=state.target_persona_id,
                        game_region=state.game_region,
                        uma_pid=state.uma_pid,
                        evidence_reason=preview.evidence_reason,
                        corrected_by_discord_user_id=str(context.user_id),
                        expected_source_persona_id=state.source_persona_id,
                        expected_state_fingerprint=state.state_fingerprint,
                        idempotency_key=interaction_key,
                        correlation_id=interaction_key,
                    )
                )
            )
            content = copy.format_owner_correction_receipt(result)
        except StaffGameAccountOwnerCorrectionUnavailableError:
            content = copy.OWNER_CORRECTION_UNAVAILABLE
        except StaffGameAccountOwnerCorrectionSameOwnerError:
            content = copy.OWNER_CORRECTION_SAME_OWNER
        except StaffGameAccountOwnerCorrectionWalletMissingError:
            content = copy.OWNER_CORRECTION_WALLET_MISSING
        except StaffGameAccountOwnerCorrectionStaleError:
            content = copy.OWNER_CORRECTION_STALE
        except StaffGameAccountOwnerCorrectionIdempotencyConflictError:
            content = copy.IDEMPOTENCY_CONFLICT
        except StaffGameAccountOwnerCorrectionConcurrentConflictError:
            content = copy.OWNER_CORRECTION_CONCURRENT_CONFLICT
        except StaffGameAccountOwnerCorrectionAuditError:
            self._log_failure(interaction, operation="owner-correction-audit")
            content = copy.owner_correction_internal_error(correlation_id(interaction))
        except (TypeError, ValueError):
            content = copy.OWNER_CORRECTION_INVALID_INPUT
        except Exception:
            self._log_failure(interaction, operation="owner-correction-final")
            content = copy.owner_correction_internal_error(correlation_id(interaction))
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
                "Discord Staff Persona owner-correction component response failed correlation_id=%s",
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
                "Discord Staff Persona owner-correction deferred response failed correlation_id=%s",
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
            "Discord Staff Persona owner correction failed correlation_id=%s command=%s operation=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
            safe_discord_text(operation, limit=64),
        )
