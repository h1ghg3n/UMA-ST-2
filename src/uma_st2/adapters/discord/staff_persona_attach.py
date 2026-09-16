"""Private Discord workflow for attaching access to an existing Persona."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import discord

from uma_st2.application.identity import (
    AttachDiscordAccountToPersona,
    StaffDiscordAttachAuditError,
    StaffDiscordAttachCommands,
    StaffDiscordAttachConcurrentConflictError,
    StaffDiscordAttachIdempotencyConflictError,
    StaffDiscordAttachPendingRequestError,
    StaffDiscordAttachPersonaNotFoundError,
    StaffDiscordAttachQueries,
    StaffDiscordAttachStaleError,
    StaffDiscordAttachState,
    StaffDiscordAttachTargetLinkedError,
)

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


class StaffDiscordAttachMemberSelect(discord.ui.UserSelect[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDiscordAttachDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._persona_id = persona_id
        super().__init__(
            placeholder=copy.ATTACH_MEMBER_PLACEHOLDER,
            min_values=1,
            max_values=1,
            custom_id="staff-persona-discord-attach-member",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        target = self.values[0]
        await self._adapter.show_preview(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._persona_id,
            target_discord_user_id=required_snowflake(
                getattr(target, "id", None),
                field_name="target Discord user ID",
            ),
            target_display_name=_display_name(target),
            operational_note=None,
            source_view=self.view,
        )


class StaffDiscordAttachBackButton(discord.ui.Button[discord.ui.LayoutView]):
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
            custom_id="staff-persona-discord-attach-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._navigation.show_panel_component(
            interaction,
            context=self._context,
            selected_persona_id=self._persona_id,
            source_view=self.view,
        )


class StaffDiscordAttachPickerView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffDiscordAttachDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        persona_display_name: str,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                copy.format_discord_attach_picker(
                    persona_id=persona_id,
                    persona_display_name=persona_display_name,
                )
            )
        )
        select_row = discord.ui.ActionRow()
        select_row.add_item(
            StaffDiscordAttachMemberSelect(
                adapter=adapter,
                navigation=navigation,
                context=context,
                persona_id=persona_id,
            )
        )
        action_row = discord.ui.ActionRow()
        action_row.add_item(
            StaffDiscordAttachBackButton(
                navigation=navigation,
                context=context,
                persona_id=persona_id,
            )
        )
        container.add_item(select_row)
        container.add_item(action_row)
        self.add_item(container)


class StaffDiscordAttachNoteButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDiscordAttachDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffDiscordAttachState,
        target_display_name: str,
        operational_note: str | None,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._preview = preview
        self._target_display_name = target_display_name
        self._operational_note = operational_note
        super().__init__(
            label=copy.ATTACH_NOTE_EDIT_LABEL if operational_note else copy.ATTACH_NOTE_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-discord-attach-note",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_note_modal(
            interaction,
            navigation=self._navigation,
            context=self._context,
            preview=self._preview,
            target_display_name=self._target_display_name,
            operational_note=self._operational_note,
            source_view=self.view,
        )


class StaffDiscordAttachConfirmButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDiscordAttachDiscordAdapter,
        context: StaffPersonaInteractionContext,
        preview: StaffDiscordAttachState,
        operational_note: str | None,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._operational_note = operational_note
        super().__init__(
            label=copy.ATTACH_CONFIRM_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id="staff-persona-discord-attach-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.attach(
            interaction,
            context=self._context,
            preview=self._preview,
            operational_note=self._operational_note,
            source_view=self.view,
        )


class StaffDiscordAttachRetargetButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDiscordAttachDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffDiscordAttachState,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._preview = preview
        super().__init__(
            label=copy.ATTACH_RETARGET_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-discord-attach-retarget",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_picker(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._preview.persona_id,
            persona_display_name=self._preview.persona_display_name,
            source_view=self.view,
        )


class StaffDiscordAttachConfirmView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffDiscordAttachDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffDiscordAttachState,
        target_display_name: str,
        operational_note: str | None,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                copy.format_discord_attach_preview(
                    preview,
                    target_display_name=target_display_name,
                    operational_note=operational_note,
                )
            )
        )
        row = discord.ui.ActionRow()
        row.add_item(
            StaffDiscordAttachConfirmButton(
                adapter=adapter,
                context=context,
                preview=preview,
                operational_note=operational_note,
            )
        )
        row.add_item(
            StaffDiscordAttachNoteButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                preview=preview,
                target_display_name=target_display_name,
                operational_note=operational_note,
            )
        )
        row.add_item(
            StaffDiscordAttachRetargetButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                preview=preview,
            )
        )
        row.add_item(
            StaffDiscordAttachBackButton(
                navigation=navigation,
                context=context,
                persona_id=preview.persona_id,
            )
        )
        container.add_item(row)
        self.add_item(container)


class StaffDiscordAttachNoteModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        adapter: StaffDiscordAttachDiscordAdapter,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffDiscordAttachState,
        target_display_name: str,
        operational_note: str | None,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        super().__init__(title=copy.ATTACH_NOTE_MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._preview = preview
        self._target_display_name = target_display_name
        self._source_view = source_view
        self.note = discord.ui.TextInput(
            label=copy.ATTACH_NOTE_INPUT_LABEL,
            style=discord.TextStyle.paragraph,
            required=False,
            default=operational_note,
            max_length=255,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_preview(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._preview.persona_id,
            target_discord_user_id=int(self._preview.target_discord_user_id),
            target_display_name=self._target_display_name,
            operational_note=str(self.note.value),
            source_view=self._source_view,
        )


@dataclass(frozen=True, slots=True)
class StaffDiscordAttachDiscordAdapter:
    queries: StaffDiscordAttachQueries
    commands: StaffDiscordAttachCommands
    prepare_command: PrepareDiscordCommand
    authorize_interaction: AuthorizeDiscordInteraction
    run_application: BlockingApplicationRunner = run_blocking_application

    async def show_picker(
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
            replacement = StaffDiscordAttachPickerView(
                adapter=self,
                navigation=navigation,
                context=context,
                persona_id=persona_id,
                persona_display_name=persona_display_name,
            )
        except Exception:
            self._log_failure(interaction, operation="attach-picker")
            await self._send_component_error(
                interaction,
                copy.attach_internal_error(correlation_id(interaction)),
            )
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="attach-picker",
        )

    async def show_preview(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        target_discord_user_id: int,
        target_display_name: str,
        operational_note: str | None,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            preview = await self.run_application(
                lambda: self.queries.get_preview(
                    guild_id=str(context.guild_id),
                    persona_id=persona_id,
                    discord_user_id=str(target_discord_user_id),
                )
            )
            view = StaffDiscordAttachConfirmView(
                adapter=self,
                navigation=navigation,
                context=context,
                preview=preview,
                target_display_name=target_display_name,
                operational_note=operational_note,
            )
        except StaffDiscordAttachPersonaNotFoundError:
            await self._send_component_error(interaction, copy.ATTACH_PERSONA_UNAVAILABLE)
            return
        except StaffDiscordAttachTargetLinkedError:
            await self._send_component_error(interaction, copy.ATTACH_TARGET_LINKED)
            return
        except StaffDiscordAttachPendingRequestError:
            await self._send_component_error(interaction, copy.ATTACH_PENDING_REQUEST)
            return
        except (TypeError, ValueError):
            await self._send_component_error(interaction, copy.ATTACH_INVALID_INPUT)
            return
        except Exception:
            self._log_failure(interaction, operation="attach-preview")
            await self._send_component_error(
                interaction,
                copy.attach_internal_error(correlation_id(interaction)),
            )
            return
        await self._replace_component_layout(
            interaction,
            view=view,
            source_view=source_view,
            operation="attach-preview",
        )

    async def open_note_modal(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffPersonaPanelNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffDiscordAttachState,
        target_display_name: str,
        operational_note: str | None,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                StaffDiscordAttachNoteModal(
                    adapter=self,
                    navigation=navigation,
                    context=context,
                    preview=preview,
                    target_display_name=target_display_name,
                    operational_note=operational_note,
                    source_view=source_view,
                )
            )
        except Exception:
            self._log_failure(interaction, operation="attach-note-modal")
            await self._send_component_error(
                interaction,
                copy.attach_internal_error(correlation_id(interaction)),
            )

    async def attach(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        preview: StaffDiscordAttachState,
        operational_note: str | None,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not context.matches(interaction):
            await self._send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            interaction_key = correlation_id(interaction)
            result = await self.run_application(
                lambda: self.commands.attach(
                    AttachDiscordAccountToPersona(
                        guild_id=str(context.guild_id),
                        target_persona_id=preview.persona_id,
                        target_discord_user_id=preview.target_discord_user_id,
                        attached_by_discord_user_id=str(context.user_id),
                        expected_target_fingerprint=preview.state_fingerprint,
                        idempotency_key=interaction_key,
                        correlation_id=interaction_key,
                        operational_note=operational_note,
                    )
                )
            )
            content = copy.format_discord_attach_receipt(result)
        except StaffDiscordAttachPersonaNotFoundError:
            content = copy.ATTACH_PERSONA_UNAVAILABLE
        except StaffDiscordAttachTargetLinkedError:
            content = copy.ATTACH_TARGET_LINKED
        except StaffDiscordAttachPendingRequestError:
            content = copy.ATTACH_PENDING_REQUEST
        except StaffDiscordAttachStaleError:
            content = copy.ATTACH_STALE
        except StaffDiscordAttachIdempotencyConflictError:
            content = copy.IDEMPOTENCY_CONFLICT
        except StaffDiscordAttachConcurrentConflictError:
            content = copy.ATTACH_CONCURRENT_CONFLICT
        except (TypeError, ValueError):
            content = copy.ATTACH_INVALID_INPUT
        except StaffDiscordAttachAuditError:
            self._log_failure(interaction, operation="attach-audit")
            content = copy.attach_internal_error(correlation_id(interaction))
        except Exception:
            self._log_failure(interaction, operation="attach-final")
            content = copy.attach_internal_error(correlation_id(interaction))
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
                "Discord Staff Persona attach component response failed correlation_id=%s",
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
                "Discord Staff Persona attach deferred response failed correlation_id=%s",
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
            await self._send_component_error(interaction, copy.ATTACH_TRANSITION_ERROR)

    @staticmethod
    def _stop(view: discord.ui.LayoutView | None) -> None:
        if view is not None:
            view.stop()

    @staticmethod
    def _log_failure(interaction: object, *, operation: str) -> None:
        logger.error(
            "Discord Staff Persona attach failed correlation_id=%s command=%s operation=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
            safe_discord_text(operation, limit=64),
        )
