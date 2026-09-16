"""Private `/staff persona` registration-review workflow."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import discord
from discord import app_commands

from uma_st2.application.identity import (
    ApproveAccountRegistrationRequest,
    RegistrationReviewCommands,
    RejectAccountRegistrationRequest,
    StaffPersonaChoice,
    StaffPersonaContext,
    StaffPersonaPanel,
    StaffRegistrationPidUnavailableError,
    StaffRegistrationRequesterLinkedError,
    StaffRegistrationRequestNotFoundError,
    StaffRegistrationRequestNotPendingError,
    StaffRegistrationRequestPage,
    StaffRegistrationRequestStaleError,
    StaffRegistrationRequestState,
    StaffRegistrationReviewAuditError,
    StaffRegistrationReviewConcurrentConflictError,
    StaffRegistrationReviewIdempotencyConflictError,
    StaffRegistrationReviewQueries,
)
from uma_st2.application.point import StaffCirclePointOperation

from .common import (
    AuthorizeDiscordAutocomplete,
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    PrepareDiscordCommand,
    buttonless_terminal_layout,
    correlation_id,
    run_blocking_application,
    safe_discord_text,
    send_ephemeral_internal_error_after_defer_safely,
)
from .localization import korean_command_name, korean_parameter_name
from .staff_circle_point import StaffCirclePointHandler
from .staff_persona_attach import StaffDiscordAttachDiscordAdapter
from .staff_persona_common import StaffPersonaInteractionContext
from .staff_persona_common import bounded_label as _bounded_label
from .staff_persona_display import StaffDisplayEditDiscordAdapter
from .staff_persona_game_account import StaffGameAccountAddDiscordAdapter
from .staff_persona_owner_correction import StaffGameAccountOwnerCorrectionDiscordAdapter
from .staff_persona_registration import StaffDirectRegistrationDiscordAdapter
from .staff_persona_status import StaffPersonaStatusDiscordAdapter
from .strings import staff_circle_point as point_copy
from .strings import staff_persona as copy

logger = logging.getLogger(__name__)

_COMMAND_NAME = "staff.persona"
_COMPONENT_TIMEOUT_SECONDS = 600.0
PERSONA_OPTION_NAME = korean_parameter_name("persona", "페르소나")


class StaffPersonaReviewButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        disabled: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._selected_persona_id = selected_persona_id
        super().__init__(
            label=copy.REVIEW_ACTION_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id="staff-persona-registration-review",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_request_list(
            interaction,
            context=self._context,
            selected_persona_id=self._selected_persona_id,
            page=0,
            source_view=self.view,
        )


class StaffPersonaAttachButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona: StaffPersonaContext | None,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._selected_persona = selected_persona
        super().__init__(
            label=copy.ATTACH_ACTION_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id="staff-persona-discord-attach",
            disabled=selected_persona is None,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self._selected_persona
        if selected is None:
            return
        await self._adapter.attach_adapter.show_picker(
            interaction,
            navigation=self._adapter,
            context=self._context,
            persona_id=selected.persona_id,
            persona_display_name=selected.display_name,
            source_view=self.view,
        )


class StaffPersonaDirectRegistrationButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._selected_persona_id = selected_persona_id
        super().__init__(
            label=copy.DIRECT_REGISTER_ACTION_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id="staff-persona-direct-registration",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.registration_adapter.show_picker(
            interaction,
            navigation=self._adapter,
            context=self._context,
            selected_persona_id=self._selected_persona_id,
            source_view=self.view,
        )


class StaffPersonaGameAccountAddButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona: StaffPersonaContext | None,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._selected_persona = selected_persona
        super().__init__(
            label=copy.GAME_ACCOUNT_ADD_ACTION_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id="staff-persona-game-account-add",
            disabled=selected_persona is None,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self._selected_persona
        if selected is None:
            return
        await self._adapter.game_account_add_adapter.show_region(
            interaction,
            navigation=self._adapter,
            context=self._context,
            persona_id=selected.persona_id,
            persona_display_name=selected.display_name,
            source_view=self.view,
        )


class StaffPersonaDisplayEditButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona: StaffPersonaContext | None,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._selected_persona = selected_persona
        super().__init__(
            label=copy.DISPLAY_EDIT_ACTION_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id="staff-persona-display-edit",
            disabled=selected_persona is None,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self._selected_persona
        if selected is None:
            return
        await self._adapter.display_edit_adapter.show_scope(
            interaction,
            navigation=self._adapter,
            context=self._context,
            persona_id=selected.persona_id,
            persona_display_name=selected.display_name,
            source_view=self.view,
        )


class StaffPersonaGameAccountOwnerCorrectionButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona: StaffPersonaContext | None,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._selected_persona = selected_persona
        super().__init__(
            label=copy.OWNER_CORRECTION_ACTION_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-game-account-owner-correction",
            disabled=selected_persona is None,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self._selected_persona
        if selected is None:
            return
        await self._adapter.owner_correction_adapter.show_region(
            interaction,
            navigation=self._adapter,
            context=self._context,
            persona_id=selected.persona_id,
            persona_display_name=selected.display_name,
            source_view=self.view,
        )


class StaffPersonaStatusButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona: StaffPersonaContext | None,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._selected_persona = selected_persona
        super().__init__(
            label=copy.STATUS_ACTION_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id="staff-persona-status",
            disabled=selected_persona is None,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self._selected_persona
        if selected is None:
            return
        await self._adapter.status_adapter.show_status(
            interaction,
            navigation=self._adapter,
            context=self._context,
            persona_id=selected.persona_id,
            source_view=self.view,
        )


class StaffPersonaCloseButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
    ) -> None:
        self._adapter = adapter
        self._context = context
        super().__init__(
            label=copy.CLOSE_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-close",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.close(interaction, context=self._context, source_view=self.view)


class StaffPersonaPanelView(discord.ui.LayoutView):
    """Initial panel with only currently executable actions."""

    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        panel: StaffPersonaPanel,
        selected_persona_id: str | None,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_staff_persona_panel(panel)))
        primary_row = discord.ui.ActionRow()
        primary_row.add_item(
            StaffPersonaReviewButton(
                adapter=adapter,
                context=context,
                selected_persona_id=selected_persona_id,
                disabled=panel.pending_request_count == 0,
            )
        )
        primary_row.add_item(
            StaffPersonaDirectRegistrationButton(
                adapter=adapter,
                context=context,
                selected_persona_id=selected_persona_id,
            )
        )
        selected_row = discord.ui.ActionRow()
        selected_row.add_item(
            StaffPersonaAttachButton(
                adapter=adapter,
                context=context,
                selected_persona=panel.selected_persona,
            )
        )
        selected_row.add_item(
            StaffPersonaGameAccountAddButton(
                adapter=adapter,
                context=context,
                selected_persona=panel.selected_persona,
            )
        )
        selected_row.add_item(
            StaffPersonaDisplayEditButton(
                adapter=adapter,
                context=context,
                selected_persona=panel.selected_persona,
            )
        )
        selected_row.add_item(
            StaffPersonaStatusButton(
                adapter=adapter,
                context=context,
                selected_persona=panel.selected_persona,
            )
        )
        selected_row.add_item(
            StaffPersonaGameAccountOwnerCorrectionButton(
                adapter=adapter,
                context=context,
                selected_persona=panel.selected_persona,
            )
        )
        footer_row = discord.ui.ActionRow()
        footer_row.add_item(StaffPersonaCloseButton(adapter=adapter, context=context))
        container.add_item(primary_row)
        container.add_item(selected_row)
        container.add_item(footer_row)
        self.add_item(container)


class StaffRegistrationRequestSelect(discord.ui.Select[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        page: StaffRegistrationRequestPage,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._selected_persona_id = selected_persona_id
        self._page = page.page
        options = [
            discord.SelectOption(
                label=_bounded_label(f"#{item.request_id} · {item.discord_display_name_snapshot}"),
                value=str(item.request_id),
                description=_bounded_label(
                    f"{item.game_region.value} · {copy.mask_pid(item.uma_pid)} · {item.nickname}"
                ),
            )
            for item in page.items
        ]
        super().__init__(
            placeholder="검토할 등록 요청을 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="staff-persona-registration-request-select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_request_detail(
            interaction,
            context=self._context,
            selected_persona_id=self._selected_persona_id,
            page=self._page,
            request_id=int(self.values[0]),
            source_view=self.view,
        )


class StaffRegistrationPageButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        page: int,
        direction: int,
        disabled: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._selected_persona_id = selected_persona_id
        self._page = max(0, page + direction)
        super().__init__(
            label=copy.PREVIOUS_LABEL if direction < 0 else copy.NEXT_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"staff-persona-request-page-{'previous' if direction < 0 else 'next'}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_request_list(
            interaction,
            context=self._context,
            selected_persona_id=self._selected_persona_id,
            page=self._page,
            source_view=self.view,
        )


class StaffPersonaPanelBackButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._selected_persona_id = selected_persona_id
        super().__init__(
            label=copy.BACK_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-panel-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_panel_component(
            interaction,
            context=self._context,
            selected_persona_id=self._selected_persona_id,
            source_view=self.view,
        )


class StaffRegistrationRequestListView(discord.ui.LayoutView):
    """One detached pending-request selector page."""

    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        page: StaffRegistrationRequestPage,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_registration_request_page(page)))
        if page.items:
            select_row = discord.ui.ActionRow()
            select_row.add_item(
                StaffRegistrationRequestSelect(
                    adapter=adapter,
                    context=context,
                    selected_persona_id=selected_persona_id,
                    page=page,
                )
            )
            container.add_item(select_row)
        navigation = discord.ui.ActionRow()
        navigation.add_item(
            StaffRegistrationPageButton(
                adapter=adapter,
                context=context,
                selected_persona_id=selected_persona_id,
                page=page.page,
                direction=-1,
                disabled=not page.has_previous,
            )
        )
        navigation.add_item(
            StaffRegistrationPageButton(
                adapter=adapter,
                context=context,
                selected_persona_id=selected_persona_id,
                page=page.page,
                direction=1,
                disabled=not page.has_next,
            )
        )
        navigation.add_item(
            StaffPersonaPanelBackButton(
                adapter=adapter,
                context=context,
                selected_persona_id=selected_persona_id,
            )
        )
        navigation.add_item(StaffPersonaCloseButton(adapter=adapter, context=context))
        container.add_item(navigation)
        self.add_item(container)


class StaffRegistrationApproveButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        page: int,
        request: StaffRegistrationRequestState,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._selected_persona_id = selected_persona_id
        self._page = page
        self._request_id = request.request_id
        super().__init__(
            label=copy.APPROVE_LABEL,
            style=discord.ButtonStyle.success,
            custom_id="staff-persona-registration-approve-preview",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_approval_confirmation(
            interaction,
            context=self._context,
            selected_persona_id=self._selected_persona_id,
            page=self._page,
            request_id=self._request_id,
            source_view=self.view,
        )


class StaffRegistrationRejectButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        request: StaffRegistrationRequestState,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._request = request
        super().__init__(
            label=copy.REJECT_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id="staff-persona-registration-reject",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_rejection_modal(
            interaction,
            context=self._context,
            request=self._request,
            source_view=self.view,
        )


class StaffRegistrationListBackButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        page: int,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._selected_persona_id = selected_persona_id
        self._page = page
        super().__init__(
            label=copy.BACK_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-registration-list-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_request_list(
            interaction,
            context=self._context,
            selected_persona_id=self._selected_persona_id,
            page=self._page,
            source_view=self.view,
        )


class StaffRegistrationRequestDetailView(discord.ui.LayoutView):
    """Complete staff-private request Preview."""

    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        page: int,
        request: StaffRegistrationRequestState,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._final_started = False
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_registration_request_detail(request)))
        row = discord.ui.ActionRow()
        row.add_item(
            StaffRegistrationApproveButton(
                adapter=adapter,
                context=context,
                selected_persona_id=selected_persona_id,
                page=page,
                request=request,
            )
        )
        row.add_item(
            StaffRegistrationRejectButton(
                adapter=adapter,
                context=context,
                request=request,
            )
        )
        row.add_item(
            StaffRegistrationListBackButton(
                adapter=adapter,
                context=context,
                selected_persona_id=selected_persona_id,
                page=page,
            )
        )
        row.add_item(StaffPersonaCloseButton(adapter=adapter, context=context))
        container.add_item(row)
        self.add_item(container)

    def claim_final(self) -> bool:
        """Claim the one final review action available from this detail."""

        if self._final_started:
            return False
        self._final_started = True
        return True


class StaffRegistrationApprovalConfirmButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        request: StaffRegistrationRequestState,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._request = request
        super().__init__(
            label=copy.APPROVE_CONFIRM_LABEL,
            style=discord.ButtonStyle.success,
            custom_id="staff-persona-registration-approve-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.approve(
            interaction,
            context=self._context,
            request=self._request,
            source_view=self.view,
        )


class StaffRegistrationApprovalConfirmView(discord.ui.LayoutView):
    """Final approval consequence Preview and Confirm."""

    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        page: int,
        request: StaffRegistrationRequestState,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._final_started = False
        container = discord.ui.Container(
            discord.ui.TextDisplay(copy.format_registration_approval_confirmation(request))
        )
        row = discord.ui.ActionRow()
        row.add_item(
            StaffRegistrationApprovalConfirmButton(
                adapter=adapter,
                context=context,
                request=request,
            )
        )
        row.add_item(
            StaffRegistrationListBackButton(
                adapter=adapter,
                context=context,
                selected_persona_id=selected_persona_id,
                page=page,
            )
        )
        row.add_item(StaffPersonaCloseButton(adapter=adapter, context=context))
        container.add_item(row)
        self.add_item(container)

    def claim_final(self) -> bool:
        """Claim this approval confirmation once."""

        if self._final_started:
            return False
        self._final_started = True
        return True


class StaffRegistrationRejectionModal(discord.ui.Modal):
    """Required-reason direct-final rejection input."""

    def __init__(
        self,
        *,
        adapter: StaffPersonaDiscordAdapter,
        context: StaffPersonaInteractionContext,
        request: StaffRegistrationRequestState,
        source_view: StaffRegistrationRequestDetailView | None,
    ) -> None:
        super().__init__(title=copy.REJECTION_MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._request = request
        self._source_view = source_view
        self.reason = discord.ui.TextInput(
            label=copy.REJECTION_REASON_LABEL,
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=255,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.reject(
            interaction,
            context=self._context,
            request=self._request,
            reason=str(self.reason.value),
            source_view=self._source_view,
        )


@dataclass(frozen=True, slots=True)
class StaffPersonaDiscordAdapter:
    """Present staff review queries and invoke narrow final mutations."""

    queries: StaffRegistrationReviewQueries
    commands: RegistrationReviewCommands
    attach_adapter: StaffDiscordAttachDiscordAdapter
    registration_adapter: StaffDirectRegistrationDiscordAdapter
    game_account_add_adapter: StaffGameAccountAddDiscordAdapter
    owner_correction_adapter: StaffGameAccountOwnerCorrectionDiscordAdapter
    display_edit_adapter: StaffDisplayEditDiscordAdapter
    status_adapter: StaffPersonaStatusDiscordAdapter
    prepare_command: PrepareDiscordCommand
    authorize_autocomplete: AuthorizeDiscordAutocomplete
    authorize_interaction: AuthorizeDiscordInteraction
    run_application: BlockingApplicationRunner = run_blocking_application

    async def persona_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not await self.authorize_autocomplete(interaction, _COMMAND_NAME):
            return []
        try:
            choices = await self.run_application(lambda: self.queries.search_personas(query=current, limit=25))
        except Exception:
            self._log_failure(interaction, operation="persona-autocomplete")
            return []
        return [self._persona_choice(choice) for choice in choices]

    async def open_panel(self, interaction: discord.Interaction, *, persona_id: str | None) -> None:
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            context = StaffPersonaInteractionContext.from_interaction(interaction)
            panel = await self.run_application(
                lambda: self.queries.get_panel(
                    guild_id=str(context.guild_id),
                    persona_id=persona_id,
                )
            )
            await interaction.edit_original_response(
                content=None,
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=StaffPersonaPanelView(
                    adapter=self,
                    context=context,
                    panel=panel,
                    selected_persona_id=persona_id,
                ),
            )
        except Exception:
            self._log_failure(interaction, operation="open-panel")
            await send_ephemeral_internal_error_after_defer_safely(interaction, _COMMAND_NAME)

    async def show_panel_component(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_component(interaction, context=context):
            return
        try:
            panel = await self.run_application(
                lambda: self.queries.get_panel(
                    guild_id=str(context.guild_id),
                    persona_id=selected_persona_id,
                )
            )
            replacement = StaffPersonaPanelView(
                adapter=self,
                context=context,
                panel=panel,
                selected_persona_id=selected_persona_id,
            )
        except Exception:
            self._log_failure(interaction, operation="panel-refresh")
            await self._send_component_error(interaction, copy.internal_error(correlation_id(interaction)))
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="panel-refresh",
        )

    async def show_request_list(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        page: int,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_component(interaction, context=context):
            return
        try:
            projection = await self.run_application(
                lambda: self.queries.list_pending_requests(
                    guild_id=str(context.guild_id),
                    page=page,
                )
            )
            replacement = StaffRegistrationRequestListView(
                adapter=self,
                context=context,
                selected_persona_id=selected_persona_id,
                page=projection,
            )
        except Exception:
            self._log_failure(interaction, operation="request-list")
            await self._send_component_error(interaction, copy.internal_error(correlation_id(interaction)))
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="request-list",
        )

    async def show_request_detail(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        page: int,
        request_id: int,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_component(interaction, context=context):
            return
        try:
            request = await self._get_request(context=context, request_id=request_id)
            replacement = StaffRegistrationRequestDetailView(
                adapter=self,
                context=context,
                selected_persona_id=selected_persona_id,
                page=page,
                request=request,
            )
        except StaffRegistrationRequestNotFoundError:
            await self._send_component_error(interaction, copy.REQUEST_UNAVAILABLE)
            return
        except Exception:
            self._log_failure(interaction, operation="request-detail")
            await self._send_component_error(interaction, copy.internal_error(correlation_id(interaction)))
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="request-detail",
        )

    async def show_approval_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        page: int,
        request_id: int,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_component(interaction, context=context):
            return
        try:
            request = await self._get_request(context=context, request_id=request_id)
            replacement = StaffRegistrationApprovalConfirmView(
                adapter=self,
                context=context,
                selected_persona_id=selected_persona_id,
                page=page,
                request=request,
            )
        except StaffRegistrationRequestNotFoundError:
            await self._send_component_error(interaction, copy.REQUEST_UNAVAILABLE)
            return
        except Exception:
            self._log_failure(interaction, operation="approval-preview")
            await self._send_component_error(interaction, copy.internal_error(correlation_id(interaction)))
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="approval-preview",
        )

    async def open_rejection_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        request: StaffRegistrationRequestState,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_component(interaction, context=context):
            return
        try:
            current = await self._get_request(context=context, request_id=request.request_id)
            await interaction.response.send_modal(
                StaffRegistrationRejectionModal(
                    adapter=self,
                    context=context,
                    request=current,
                    source_view=source_view,
                )
            )
        except StaffRegistrationRequestNotFoundError:
            await self._send_component_error(interaction, copy.REQUEST_UNAVAILABLE)
        except Exception:
            self._log_failure(interaction, operation="rejection-modal")
            await self._send_component_error(interaction, copy.internal_error(correlation_id(interaction)))

    async def approve(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        request: StaffRegistrationRequestState,
        source_view: StaffRegistrationApprovalConfirmView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            operation="approve-final",
        ):
            return
        if source_view is not None and not source_view.claim_final():
            source_view.stop()
            await self._edit_deferred(interaction, copy.REVIEW_ALREADY_STARTED)
            return
        self._stop(source_view)
        try:
            result = await self.run_application(
                lambda: self.commands.approve(
                    ApproveAccountRegistrationRequest(
                        request_id=request.request_id,
                        guild_id=str(context.guild_id),
                        reviewed_by_discord_user_id=str(context.user_id),
                        expected_request_fingerprint=request.state_fingerprint,
                        idempotency_key=correlation_id(interaction),
                        correlation_id=correlation_id(interaction),
                    )
                )
            )
            content = copy.format_registration_approval_receipt(result)
        except StaffRegistrationRequestStaleError:
            content = copy.REQUEST_STALE
        except (StaffRegistrationRequestNotFoundError, StaffRegistrationRequestNotPendingError):
            content = copy.REQUEST_UNAVAILABLE
        except StaffRegistrationRequesterLinkedError:
            content = copy.REQUESTER_LINKED
        except StaffRegistrationPidUnavailableError:
            content = copy.PID_UNAVAILABLE
        except StaffRegistrationReviewIdempotencyConflictError:
            content = copy.IDEMPOTENCY_CONFLICT
        except StaffRegistrationReviewConcurrentConflictError:
            content = copy.CONCURRENT_CONFLICT
        except ValueError:
            content = copy.INVALID_INPUT
        except StaffRegistrationReviewAuditError:
            self._log_failure(interaction, operation="approve-audit")
            content = copy.internal_error(correlation_id(interaction))
        except Exception:
            self._log_failure(interaction, operation="approve")
            content = copy.internal_error(correlation_id(interaction))
        await self._edit_deferred(interaction, content)

    async def reject(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        request: StaffRegistrationRequestState,
        reason: str,
        source_view: StaffRegistrationRequestDetailView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            operation="reject-final",
        ):
            return
        if source_view is not None and not source_view.claim_final():
            source_view.stop()
            await self._edit_deferred(interaction, copy.REVIEW_ALREADY_STARTED)
            return
        self._stop(source_view)
        try:
            result = await self.run_application(
                lambda: self.commands.reject(
                    RejectAccountRegistrationRequest(
                        request_id=request.request_id,
                        guild_id=str(context.guild_id),
                        reviewed_by_discord_user_id=str(context.user_id),
                        expected_request_fingerprint=request.state_fingerprint,
                        reason=reason,
                        idempotency_key=correlation_id(interaction),
                        correlation_id=correlation_id(interaction),
                    )
                )
            )
            content = copy.format_registration_rejection_receipt(result)
        except StaffRegistrationRequestStaleError:
            content = copy.REQUEST_STALE
        except (StaffRegistrationRequestNotFoundError, StaffRegistrationRequestNotPendingError):
            content = copy.REQUEST_UNAVAILABLE
        except StaffRegistrationReviewIdempotencyConflictError:
            content = copy.IDEMPOTENCY_CONFLICT
        except StaffRegistrationReviewConcurrentConflictError:
            content = copy.CONCURRENT_CONFLICT
        except ValueError:
            content = copy.INVALID_INPUT
        except StaffRegistrationReviewAuditError:
            self._log_failure(interaction, operation="reject-audit")
            content = copy.internal_error(correlation_id(interaction))
        except Exception:
            self._log_failure(interaction, operation="reject")
            content = copy.internal_error(correlation_id(interaction))
        await self._edit_deferred(interaction, content)

    async def close(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_component(interaction, context=context):
            return
        try:
            await interaction.response.edit_message(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=buttonless_terminal_layout(
                    copy.CLOSED_RECEIPT,
                    timeout_seconds=_COMPONENT_TIMEOUT_SECONDS,
                ),
            )
        except Exception:
            self._log_failure(interaction, operation="close")
            return
        self._stop(source_view)

    async def _get_request(
        self,
        *,
        context: StaffPersonaInteractionContext,
        request_id: int,
    ) -> StaffRegistrationRequestState:
        return await self.run_application(
            lambda: self.queries.get_pending_request(
                guild_id=str(context.guild_id),
                request_id=request_id,
            )
        )

    async def _authorize_component(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
    ) -> bool:
        if not context.matches(interaction):
            await self._send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return False
        return await self.authorize_interaction(interaction, _COMMAND_NAME)

    async def _prepare_bound_update(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        operation: str,
    ) -> bool:
        if not context.matches(interaction):
            await self._send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return False
        if not await self._defer_message_update(interaction, operation=operation):
            return False
        return await self.authorize_interaction(interaction, _COMMAND_NAME)

    async def _defer_message_update(
        self,
        interaction: discord.Interaction,
        *,
        operation: str,
    ) -> bool:
        try:
            await interaction.response.defer(thinking=False)
        except Exception as error:
            self._log_failure(interaction, operation=f"{operation}-defer-{type(error).__name__}")
            return False
        return True

    @staticmethod
    def _persona_choice(choice: StaffPersonaChoice) -> app_commands.Choice[str]:
        suffix = choice.persona_id[-8:]
        return app_commands.Choice(
            name=_bounded_label(f"{choice.display_name} · {suffix}"),
            value=choice.persona_id,
        )

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
                "Discord Staff Persona component response failed correlation_id=%s",
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
                "Discord Staff Persona deferred response failed correlation_id=%s",
                correlation_id(interaction),
            )

    @staticmethod
    def _stop(view: discord.ui.LayoutView | None) -> None:
        if view is not None:
            view.stop()

    @staticmethod
    def _log_failure(interaction: object, *, operation: str) -> None:
        logger.error(
            "Discord Staff Persona failed correlation_id=%s command=%s operation=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
            safe_discord_text(operation, limit=64),
        )


class StaffPersonaHandler(Protocol):
    async def open_panel(self, interaction: discord.Interaction, *, persona_id: str | None) -> None: ...

    async def persona_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]: ...


class StaffCommandGroup(app_commands.Group):
    """V2 top-level `/staff` root for Persona and Circle Point operations."""

    def __init__(
        self,
        *,
        persona_adapter: StaffPersonaHandler,
        circle_point_adapter: StaffCirclePointHandler,
    ) -> None:
        super().__init__(name="staff", description=copy.STAFF_ROOT_DESCRIPTION)
        self._persona_adapter = persona_adapter
        self._circle_point_adapter = circle_point_adapter

    @app_commands.command(
        name=korean_command_name("persona", "계정"),
        description=copy.PERSONA_COMMAND_DESCRIPTION,
    )
    @app_commands.rename(persona_id=PERSONA_OPTION_NAME)
    @app_commands.describe(persona_id=copy.PERSONA_OPTION_DESCRIPTION)
    async def persona(
        self,
        interaction: discord.Interaction,
        persona_id: str | None = None,
    ) -> None:
        await self._persona_adapter.open_panel(interaction, persona_id=persona_id)

    @persona.autocomplete("persona_id")
    async def persona_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._persona_adapter.persona_autocomplete(interaction, current)

    @app_commands.command(
        name="grant-circle-points",
        description=point_copy.GRANT_COMMAND_DESCRIPTION,
    )
    @app_commands.rename(persona_id=PERSONA_OPTION_NAME)
    @app_commands.describe(persona_id=point_copy.PERSONA_OPTION_DESCRIPTION)
    async def grant_circle_points(
        self,
        interaction: discord.Interaction,
        persona_id: str,
    ) -> None:
        await self._circle_point_adapter.open_modal(
            interaction,
            persona_id=persona_id,
            operation=StaffCirclePointOperation.GRANT,
        )

    @grant_circle_points.autocomplete("persona_id")
    async def grant_circle_points_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._circle_point_adapter.target_autocomplete(
            interaction,
            current,
            operation=StaffCirclePointOperation.GRANT,
        )

    @app_commands.command(
        name="adjust-circle-points",
        description=point_copy.ADJUST_COMMAND_DESCRIPTION,
    )
    @app_commands.rename(persona_id=PERSONA_OPTION_NAME)
    @app_commands.describe(persona_id=point_copy.PERSONA_OPTION_DESCRIPTION)
    async def adjust_circle_points(
        self,
        interaction: discord.Interaction,
        persona_id: str,
    ) -> None:
        await self._circle_point_adapter.open_modal(
            interaction,
            persona_id=persona_id,
            operation=StaffCirclePointOperation.ADJUSTMENT,
        )

    @adjust_circle_points.autocomplete("persona_id")
    async def adjust_circle_points_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._circle_point_adapter.target_autocomplete(
            interaction,
            current,
            operation=StaffCirclePointOperation.ADJUSTMENT,
        )
