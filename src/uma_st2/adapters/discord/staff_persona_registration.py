"""Private Discord workflow for staff direct registration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import discord

from uma_st2.application.identity import (
    DirectlyRegisterDiscordAccount,
    StaffDirectRegistrationAuditError,
    StaffDirectRegistrationCommands,
    StaffDirectRegistrationConcurrentConflictError,
    StaffDirectRegistrationIdempotencyConflictError,
    StaffDirectRegistrationPendingRequestError,
    StaffDirectRegistrationPidUnavailableError,
    StaffDirectRegistrationPreview,
    StaffDirectRegistrationQueries,
    StaffDirectRegistrationStaleError,
    StaffDirectRegistrationTargetLinkedError,
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
from .staff_persona_common import StaffPersonaInteractionContext, required_snowflake
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


def _display_name(target: object) -> str:
    for attribute in ("display_name", "global_name", "name"):
        value = getattr(target, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"Discord user {getattr(target, 'id', '-')}"


class StaffDirectRegistrationBackButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
    ) -> None:
        self._navigation = navigation
        self._context = context
        self._selected_persona_id = selected_persona_id
        super().__init__(
            label=copy.BACK_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-direct-registration-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._navigation.show_panel_component(
            interaction,
            context=self._context,
            selected_persona_id=self._selected_persona_id,
            source_view=self.view,
        )


class StaffDirectRegistrationMemberSelect(discord.ui.UserSelect[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDirectRegistrationDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._selected_persona_id = selected_persona_id
        super().__init__(
            placeholder=copy.DIRECT_REGISTER_MEMBER_PLACEHOLDER,
            min_values=1,
            max_values=1,
            custom_id="staff-persona-direct-registration-member",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        target = self.values[0]
        await self._adapter.show_region(
            interaction,
            navigation=self._navigation,
            context=self._context,
            selected_persona_id=self._selected_persona_id,
            target_discord_user_id=required_snowflake(
                getattr(target, "id", None),
                field_name="target Discord user ID",
            ),
            target_display_name=_display_name(target),
            source_view=self.view,
        )


class StaffDirectRegistrationPickerView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffDirectRegistrationDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_direct_registration_picker()))
        select_row = discord.ui.ActionRow()
        select_row.add_item(
            StaffDirectRegistrationMemberSelect(
                adapter=adapter,
                navigation=navigation,
                context=context,
                selected_persona_id=selected_persona_id,
            )
        )
        action_row = discord.ui.ActionRow()
        action_row.add_item(
            StaffDirectRegistrationBackButton(
                navigation=navigation,
                context=context,
                selected_persona_id=selected_persona_id,
            )
        )
        container.add_item(select_row)
        container.add_item(action_row)
        self.add_item(container)


class StaffDirectRegistrationRegionSelect(discord.ui.Select[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDirectRegistrationDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        target_discord_user_id: int,
        target_display_name: str,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._selected_persona_id = selected_persona_id
        self._target_discord_user_id = target_discord_user_id
        self._target_display_name = target_display_name
        super().__init__(
            placeholder=copy.DIRECT_REGISTER_REGION_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="KR", value=GameRegion.KR.value),
                discord.SelectOption(label="JP", value=GameRegion.JP.value),
            ],
            custom_id="staff-persona-direct-registration-region",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_input_modal(
            interaction,
            navigation=self._navigation,
            context=self._context,
            selected_persona_id=self._selected_persona_id,
            target_discord_user_id=self._target_discord_user_id,
            target_display_name=self._target_display_name,
            game_region=GameRegion(self.values[0]),
            source_view=self.view,
        )


class StaffDirectRegistrationRegionView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffDirectRegistrationDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        target_discord_user_id: int,
        target_display_name: str,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                copy.format_direct_registration_region(
                    target_display_name=target_display_name,
                    target_discord_user_id=str(target_discord_user_id),
                )
            )
        )
        select_row = discord.ui.ActionRow()
        select_row.add_item(
            StaffDirectRegistrationRegionSelect(
                adapter=adapter,
                navigation=navigation,
                context=context,
                selected_persona_id=selected_persona_id,
                target_discord_user_id=target_discord_user_id,
                target_display_name=target_display_name,
            )
        )
        action_row = discord.ui.ActionRow()
        action_row.add_item(
            StaffDirectRegistrationBackButton(
                navigation=navigation,
                context=context,
                selected_persona_id=selected_persona_id,
            )
        )
        container.add_item(select_row)
        container.add_item(action_row)
        self.add_item(container)


class StaffDirectRegistrationInputModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        adapter: StaffDirectRegistrationDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        target_discord_user_id: int,
        target_display_name: str,
        game_region: GameRegion,
        source_view: discord.ui.LayoutView | None,
        current: StaffDirectRegistrationPreview | None = None,
    ) -> None:
        super().__init__(title=copy.DIRECT_REGISTER_INPUT_MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._selected_persona_id = selected_persona_id
        self._target_discord_user_id = target_discord_user_id
        self._target_display_name = target_display_name
        self._game_region = game_region
        self._source_view = source_view
        self.pid = discord.ui.TextInput(
            label=copy.DIRECT_REGISTER_PID_LABEL,
            min_length=1,
            max_length=32,
            default=None if current is None else current.state.uma_pid,
        )
        self.nickname = discord.ui.TextInput(
            label=copy.DIRECT_REGISTER_NICKNAME_LABEL,
            min_length=1,
            max_length=100,
            default=None if current is None else current.nickname,
        )
        self.affiliation = discord.ui.TextInput(
            label=copy.DIRECT_REGISTER_AFFILIATION_LABEL,
            required=False,
            max_length=100,
            default=None if current is None else current.affiliation,
        )
        self.note = discord.ui.TextInput(
            label=copy.DIRECT_REGISTER_NOTE_LABEL,
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=255,
            default=None if current is None else current.operational_note,
        )
        self.add_item(self.pid)
        self.add_item(self.nickname)
        self.add_item(self.affiliation)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_preview(
            interaction,
            navigation=self._navigation,
            context=self._context,
            selected_persona_id=self._selected_persona_id,
            target_discord_user_id=self._target_discord_user_id,
            target_display_name=self._target_display_name,
            game_region=self._game_region,
            uma_pid=str(self.pid.value),
            nickname=str(self.nickname.value),
            affiliation=str(self.affiliation.value),
            operational_note=str(self.note.value),
            source_view=self._source_view,
        )


class StaffDirectRegistrationConfirmButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDirectRegistrationDiscordAdapter,
        context: StaffPersonaInteractionContext,
        preview: StaffDirectRegistrationPreview,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        super().__init__(
            label=copy.DIRECT_REGISTER_CONFIRM_LABEL,
            style=discord.ButtonStyle.success,
            custom_id="staff-persona-direct-registration-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.register(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self.view,
        )


class StaffDirectRegistrationReenterButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDirectRegistrationDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        target_display_name: str,
        preview: StaffDirectRegistrationPreview,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._selected_persona_id = selected_persona_id
        self._target_display_name = target_display_name
        self._preview = preview
        super().__init__(
            label=copy.DIRECT_REGISTER_REENTER_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-direct-registration-reenter",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_input_modal(
            interaction,
            navigation=self._navigation,
            context=self._context,
            selected_persona_id=self._selected_persona_id,
            target_discord_user_id=int(self._preview.state.target_discord_user_id),
            target_display_name=self._target_display_name,
            game_region=self._preview.state.game_region,
            source_view=self.view,
            current=self._preview,
        )


class StaffDirectRegistrationRetargetButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDirectRegistrationDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._selected_persona_id = selected_persona_id
        super().__init__(
            label=copy.DIRECT_REGISTER_RETARGET_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-direct-registration-retarget",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_picker(
            interaction,
            navigation=self._navigation,
            context=self._context,
            selected_persona_id=self._selected_persona_id,
            source_view=self.view,
        )


class StaffDirectRegistrationConfirmView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffDirectRegistrationDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        target_display_name: str,
        preview: StaffDirectRegistrationPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        state = preview.state
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                copy.format_direct_registration_preview(
                    target_display_name=target_display_name,
                    target_discord_user_id=state.target_discord_user_id,
                    game_region=state.game_region.value,
                    uma_pid=state.uma_pid,
                    nickname=preview.nickname,
                    affiliation=preview.affiliation,
                    operational_note=preview.operational_note,
                )
            )
        )
        row = discord.ui.ActionRow()
        row.add_item(
            StaffDirectRegistrationConfirmButton(
                adapter=adapter,
                context=context,
                preview=preview,
            )
        )
        row.add_item(
            StaffDirectRegistrationReenterButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                selected_persona_id=selected_persona_id,
                target_display_name=target_display_name,
                preview=preview,
            )
        )
        row.add_item(
            StaffDirectRegistrationRetargetButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                selected_persona_id=selected_persona_id,
            )
        )
        row.add_item(
            StaffDirectRegistrationBackButton(
                navigation=navigation,
                context=context,
                selected_persona_id=selected_persona_id,
            )
        )
        container.add_item(row)
        self.add_item(container)


@dataclass(frozen=True, slots=True)
class StaffDirectRegistrationDiscordAdapter:
    queries: StaffDirectRegistrationQueries
    commands: StaffDirectRegistrationCommands
    prepare_command: PrepareDiscordCommand
    authorize_interaction: AuthorizeDiscordInteraction
    run_application: BlockingApplicationRunner = run_blocking_application

    async def show_picker(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            replacement = StaffDirectRegistrationPickerView(
                adapter=self,
                navigation=navigation,
                context=context,
                selected_persona_id=selected_persona_id,
            )
        except Exception:
            self._log_failure(interaction, operation="direct-registration-picker")
            await self._send_component_error(
                interaction,
                copy.direct_registration_internal_error(correlation_id(interaction)),
            )
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="direct-registration-picker",
        )

    async def show_region(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        target_discord_user_id: int,
        target_display_name: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            replacement = StaffDirectRegistrationRegionView(
                adapter=self,
                navigation=navigation,
                context=context,
                selected_persona_id=selected_persona_id,
                target_discord_user_id=target_discord_user_id,
                target_display_name=target_display_name,
            )
        except Exception:
            self._log_failure(interaction, operation="direct-registration-region")
            await self._send_component_error(
                interaction,
                copy.direct_registration_internal_error(correlation_id(interaction)),
            )
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="direct-registration-region",
        )

    async def open_input_modal(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        target_discord_user_id: int,
        target_display_name: str,
        game_region: GameRegion,
        source_view: discord.ui.LayoutView | None,
        current: StaffDirectRegistrationPreview | None = None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                StaffDirectRegistrationInputModal(
                    adapter=self,
                    navigation=navigation,
                    context=context,
                    selected_persona_id=selected_persona_id,
                    target_discord_user_id=target_discord_user_id,
                    target_display_name=target_display_name,
                    game_region=game_region,
                    source_view=source_view,
                    current=current,
                )
            )
        except Exception:
            self._log_failure(interaction, operation="direct-registration-input")
            await self._send_component_error(
                interaction,
                copy.direct_registration_internal_error(correlation_id(interaction)),
            )

    async def show_preview(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        target_discord_user_id: int,
        target_display_name: str,
        game_region: GameRegion,
        uma_pid: str,
        nickname: str,
        affiliation: str | None,
        operational_note: str | None,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            preview = await self.run_application(
                lambda: self.queries.get_preview(
                    guild_id=str(context.guild_id),
                    target_discord_user_id=str(target_discord_user_id),
                    target_display_name_snapshot=target_display_name,
                    game_region=game_region,
                    uma_pid=uma_pid,
                    nickname=nickname,
                    affiliation=affiliation,
                    operational_note=operational_note,
                )
            )
            replacement = StaffDirectRegistrationConfirmView(
                adapter=self,
                navigation=navigation,
                context=context,
                selected_persona_id=selected_persona_id,
                target_display_name=preview.target_display_name_snapshot,
                preview=preview,
            )
        except StaffDirectRegistrationTargetLinkedError:
            await self._send_component_error(interaction, copy.DIRECT_REGISTER_TARGET_LINKED)
            return
        except StaffDirectRegistrationPendingRequestError:
            await self._send_component_error(interaction, copy.DIRECT_REGISTER_PENDING_REQUEST)
            return
        except StaffDirectRegistrationPidUnavailableError:
            await self._send_component_error(interaction, copy.DIRECT_REGISTER_PID_UNAVAILABLE)
            return
        except (TypeError, ValueError):
            await self._send_component_error(interaction, copy.DIRECT_REGISTER_INVALID_INPUT)
            return
        except Exception:
            self._log_failure(interaction, operation="direct-registration-preview")
            await self._send_component_error(
                interaction,
                copy.direct_registration_internal_error(correlation_id(interaction)),
            )
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="direct-registration-preview",
        )

    async def register(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        preview: StaffDirectRegistrationPreview,
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
                lambda: self.commands.register(
                    DirectlyRegisterDiscordAccount(
                        guild_id=str(context.guild_id),
                        target_discord_user_id=state.target_discord_user_id,
                        target_display_name_snapshot=preview.target_display_name_snapshot,
                        game_region=state.game_region,
                        uma_pid=state.uma_pid,
                        nickname=preview.nickname,
                        affiliation=preview.affiliation,
                        registered_by_discord_user_id=str(context.user_id),
                        expected_target_fingerprint=state.state_fingerprint,
                        idempotency_key=interaction_key,
                        correlation_id=interaction_key,
                        operational_note=preview.operational_note,
                    )
                )
            )
            content = copy.format_direct_registration_receipt(result)
        except StaffDirectRegistrationTargetLinkedError:
            content = copy.DIRECT_REGISTER_TARGET_LINKED
        except StaffDirectRegistrationPendingRequestError:
            content = copy.DIRECT_REGISTER_PENDING_REQUEST
        except StaffDirectRegistrationPidUnavailableError:
            content = copy.DIRECT_REGISTER_PID_UNAVAILABLE
        except StaffDirectRegistrationStaleError:
            content = copy.DIRECT_REGISTER_STALE
        except StaffDirectRegistrationIdempotencyConflictError:
            content = copy.IDEMPOTENCY_CONFLICT
        except StaffDirectRegistrationConcurrentConflictError:
            content = copy.DIRECT_REGISTER_CONCURRENT_CONFLICT
        except StaffDirectRegistrationAuditError:
            self._log_failure(interaction, operation="direct-registration-audit")
            content = copy.direct_registration_internal_error(correlation_id(interaction))
        except (TypeError, ValueError):
            content = copy.DIRECT_REGISTER_INVALID_INPUT
        except Exception:
            self._log_failure(interaction, operation="direct-registration-final")
            content = copy.direct_registration_internal_error(correlation_id(interaction))
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
                "Discord Staff Persona direct-registration component response failed correlation_id=%s",
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
                "Discord Staff Persona direct-registration deferred response failed correlation_id=%s",
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
            await self._send_component_error(interaction, copy.DIRECT_REGISTER_TRANSITION_ERROR)

    @staticmethod
    def _stop(view: discord.ui.LayoutView | None) -> None:
        if view is not None:
            view.stop()

    @staticmethod
    def _log_failure(interaction: object, *, operation: str) -> None:
        logger.error(
            "Discord Staff Persona direct registration failed correlation_id=%s command=%s operation=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
            safe_discord_text(operation, limit=64),
        )
