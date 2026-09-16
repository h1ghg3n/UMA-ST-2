"""Private `/staff persona` peer GameAccount-add workflow."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import discord

from uma_st2.application.identity import (
    AddGameAccountToPersona,
    StaffGameAccountAddAuditError,
    StaffGameAccountAddCommands,
    StaffGameAccountAddConcurrentConflictError,
    StaffGameAccountAddIdempotencyConflictError,
    StaffGameAccountAddPersonaNotFoundError,
    StaffGameAccountAddPersonaRestrictedError,
    StaffGameAccountAddPidUnavailableError,
    StaffGameAccountAddPreview,
    StaffGameAccountAddQueries,
    StaffGameAccountAddStaleError,
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


class StaffGameAccountAddBackButton(discord.ui.Button[discord.ui.LayoutView]):
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
            custom_id="staff-persona-game-account-add-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._navigation.show_panel_component(
            interaction,
            context=self._context,
            selected_persona_id=self._persona_id,
            source_view=self.view,
        )


class StaffGameAccountAddRegionSelect(discord.ui.Select[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffGameAccountAddDiscordAdapter,
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
            placeholder=copy.GAME_ACCOUNT_ADD_REGION_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="KR", value=GameRegion.KR.value),
                discord.SelectOption(label="JP", value=GameRegion.JP.value),
            ],
            custom_id="staff-persona-game-account-add-region",
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


class StaffGameAccountAddRegionView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffGameAccountAddDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        persona_display_name: str,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(
            discord.ui.TextDisplay(copy.format_game_account_add_region(persona_display_name=persona_display_name))
        )
        select_row = discord.ui.ActionRow()
        select_row.add_item(
            StaffGameAccountAddRegionSelect(
                adapter=adapter,
                navigation=navigation,
                context=context,
                persona_id=persona_id,
                persona_display_name=persona_display_name,
            )
        )
        action_row = discord.ui.ActionRow()
        action_row.add_item(
            StaffGameAccountAddBackButton(
                navigation=navigation,
                context=context,
                persona_id=persona_id,
            )
        )
        container.add_item(select_row)
        container.add_item(action_row)
        self.add_item(container)


class StaffGameAccountAddInputModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        adapter: StaffGameAccountAddDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        persona_display_name: str,
        game_region: GameRegion,
        source_view: discord.ui.LayoutView | None,
        current: StaffGameAccountAddPreview | None = None,
    ) -> None:
        super().__init__(title=copy.GAME_ACCOUNT_ADD_INPUT_MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._persona_id = persona_id
        self._persona_display_name = persona_display_name
        self._game_region = game_region
        self._source_view = source_view
        self.pid = discord.ui.TextInput(
            label=copy.GAME_ACCOUNT_ADD_PID_LABEL,
            min_length=1,
            max_length=32,
            default=None if current is None else current.state.uma_pid,
        )
        self.nickname = discord.ui.TextInput(
            label=copy.GAME_ACCOUNT_ADD_NICKNAME_LABEL,
            min_length=1,
            max_length=100,
            default=None if current is None else current.nickname,
        )
        self.affiliation = discord.ui.TextInput(
            label=copy.GAME_ACCOUNT_ADD_AFFILIATION_LABEL,
            required=False,
            max_length=100,
            default=None if current is None else current.affiliation,
        )
        self.reason = discord.ui.TextInput(
            label=copy.GAME_ACCOUNT_ADD_REASON_LABEL,
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=255,
            default=None if current is None else current.reason,
        )
        self.add_item(self.pid)
        self.add_item(self.nickname)
        self.add_item(self.affiliation)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_preview(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._persona_id,
            persona_display_name=self._persona_display_name,
            game_region=self._game_region,
            uma_pid=str(self.pid.value),
            nickname=str(self.nickname.value),
            affiliation=str(self.affiliation.value),
            reason=str(self.reason.value),
            source_view=self._source_view,
        )


class StaffGameAccountAddConfirmButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffGameAccountAddDiscordAdapter,
        context: StaffPersonaInteractionContext,
        preview: StaffGameAccountAddPreview,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        super().__init__(
            label=copy.GAME_ACCOUNT_ADD_CONFIRM_LABEL,
            style=discord.ButtonStyle.success,
            custom_id="staff-persona-game-account-add-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.add(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self.view,
        )


class StaffGameAccountAddReenterButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffGameAccountAddDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffGameAccountAddPreview,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._preview = preview
        super().__init__(
            label=copy.GAME_ACCOUNT_ADD_REENTER_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-game-account-add-reenter",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        state = self._preview.state
        await self._adapter.open_input_modal(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=state.persona_id,
            persona_display_name=state.persona_display_name,
            game_region=state.game_region,
            source_view=self.view,
            current=self._preview,
        )


class StaffGameAccountAddConfirmView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffGameAccountAddDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffGameAccountAddPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_game_account_add_preview(preview)))
        row = discord.ui.ActionRow()
        row.add_item(StaffGameAccountAddConfirmButton(adapter=adapter, context=context, preview=preview))
        row.add_item(
            StaffGameAccountAddReenterButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                preview=preview,
            )
        )
        row.add_item(
            StaffGameAccountAddBackButton(
                navigation=navigation,
                context=context,
                persona_id=preview.state.persona_id,
            )
        )
        container.add_item(row)
        self.add_item(container)


@dataclass(frozen=True, slots=True)
class StaffGameAccountAddDiscordAdapter:
    queries: StaffGameAccountAddQueries
    commands: StaffGameAccountAddCommands
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
            replacement = StaffGameAccountAddRegionView(
                adapter=self,
                navigation=navigation,
                context=context,
                persona_id=persona_id,
                persona_display_name=persona_display_name,
            )
        except Exception:
            self._log_failure(interaction, operation="game-account-add-region")
            await self._send_component_error(
                interaction, copy.game_account_add_internal_error(correlation_id(interaction))
            )
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="game-account-add-region",
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
        current: StaffGameAccountAddPreview | None = None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                StaffGameAccountAddInputModal(
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
            self._log_failure(interaction, operation="game-account-add-input")
            await self._send_component_error(
                interaction, copy.game_account_add_internal_error(correlation_id(interaction))
            )

    async def show_preview(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        persona_display_name: str,
        game_region: GameRegion,
        uma_pid: str,
        nickname: str,
        affiliation: str | None,
        reason: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        del persona_display_name
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            preview = await self.run_application(
                lambda: self.queries.get_preview(
                    guild_id=str(context.guild_id),
                    persona_id=persona_id,
                    game_region=game_region,
                    uma_pid=uma_pid,
                    nickname=nickname,
                    affiliation=affiliation,
                    reason=reason,
                )
            )
            replacement = StaffGameAccountAddConfirmView(
                adapter=self,
                navigation=navigation,
                context=context,
                preview=preview,
            )
        except StaffGameAccountAddPersonaNotFoundError:
            await self._send_component_error(interaction, copy.GAME_ACCOUNT_ADD_PERSONA_UNAVAILABLE)
            return
        except StaffGameAccountAddPersonaRestrictedError:
            await self._send_component_error(interaction, copy.GAME_ACCOUNT_ADD_PERSONA_RESTRICTED)
            return
        except StaffGameAccountAddPidUnavailableError:
            await self._send_component_error(interaction, copy.GAME_ACCOUNT_ADD_PID_UNAVAILABLE)
            return
        except (TypeError, ValueError):
            await self._send_component_error(interaction, copy.GAME_ACCOUNT_ADD_INVALID_INPUT)
            return
        except Exception:
            self._log_failure(interaction, operation="game-account-add-preview")
            await self._send_component_error(
                interaction, copy.game_account_add_internal_error(correlation_id(interaction))
            )
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="game-account-add-preview",
        )

    async def add(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        preview: StaffGameAccountAddPreview,
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
                lambda: self.commands.add(
                    AddGameAccountToPersona(
                        guild_id=str(context.guild_id),
                        target_persona_id=state.persona_id,
                        game_region=state.game_region,
                        uma_pid=state.uma_pid,
                        nickname=preview.nickname,
                        affiliation=preview.affiliation,
                        reason=preview.reason,
                        added_by_discord_user_id=str(context.user_id),
                        expected_target_fingerprint=state.state_fingerprint,
                        idempotency_key=interaction_key,
                        correlation_id=interaction_key,
                    )
                )
            )
            content = copy.format_game_account_add_receipt(result)
        except StaffGameAccountAddPersonaNotFoundError:
            content = copy.GAME_ACCOUNT_ADD_PERSONA_UNAVAILABLE
        except StaffGameAccountAddPersonaRestrictedError:
            content = copy.GAME_ACCOUNT_ADD_PERSONA_RESTRICTED
        except StaffGameAccountAddPidUnavailableError:
            content = copy.GAME_ACCOUNT_ADD_PID_UNAVAILABLE
        except StaffGameAccountAddStaleError:
            content = copy.GAME_ACCOUNT_ADD_STALE
        except StaffGameAccountAddIdempotencyConflictError:
            content = copy.IDEMPOTENCY_CONFLICT
        except StaffGameAccountAddConcurrentConflictError:
            content = copy.GAME_ACCOUNT_ADD_CONCURRENT_CONFLICT
        except StaffGameAccountAddAuditError:
            self._log_failure(interaction, operation="game-account-add-audit")
            content = copy.game_account_add_internal_error(correlation_id(interaction))
        except (TypeError, ValueError):
            content = copy.GAME_ACCOUNT_ADD_INVALID_INPUT
        except Exception:
            self._log_failure(interaction, operation="game-account-add-final")
            content = copy.game_account_add_internal_error(correlation_id(interaction))
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
                "Discord Staff Persona GameAccount-add component response failed correlation_id=%s",
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
                "Discord Staff Persona GameAccount-add deferred response failed correlation_id=%s",
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
            "Discord Staff Persona GameAccount add failed correlation_id=%s command=%s operation=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
            safe_discord_text(operation, limit=64),
        )
