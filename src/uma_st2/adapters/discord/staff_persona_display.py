"""Discord presentation for staff Persona and GameAccount display corrections."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import discord

from uma_st2.application.identity import (
    StaffDisplayEditAuditError,
    StaffDisplayEditCommands,
    StaffDisplayEditConcurrentConflictError,
    StaffDisplayEditGameAccountNotFoundError,
    StaffDisplayEditIdempotencyConflictError,
    StaffDisplayEditNoChangeError,
    StaffDisplayEditPersonaNotFoundError,
    StaffDisplayEditQueries,
    StaffDisplayEditStaleError,
    StaffDisplayGameAccountPage,
    StaffGameAccountDisplayPreview,
    StaffGameAccountDisplayState,
    StaffPersonaDisplayPreview,
    StaffPersonaDisplayState,
    UpdateGameAccountDisplayInfo,
    UpdatePersonaDisplayName,
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
from .staff_persona_common import StaffPersonaInteractionContext
from .staff_persona_common import bounded_label as _bounded_label
from .strings import staff_persona as copy

logger = logging.getLogger(__name__)

_COMMAND_NAME = "staff.persona"
_COMPONENT_TIMEOUT_SECONDS = 600.0


class StaffDisplayEditNavigation(Protocol):
    async def show_panel_component(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        selected_persona_id: str | None,
        source_view: discord.ui.LayoutView | None,
    ) -> None: ...


class StaffDisplayEditBackButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
    ) -> None:
        self._navigation = navigation
        self._context = context
        self._persona_id = persona_id
        super().__init__(
            label=copy.BACK_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-display-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._navigation.show_panel_component(
            interaction,
            context=self._context,
            selected_persona_id=self._persona_id,
            source_view=self.view,
        )


class StaffDisplayEditPersonaButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._persona_id = persona_id
        super().__init__(
            label=copy.DISPLAY_EDIT_PERSONA_ACTION_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id="staff-persona-display-persona",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_persona_input(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._persona_id,
            source_view=self.view,
        )


class StaffDisplayEditGameAccountButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._persona_id = persona_id
        super().__init__(
            label=copy.DISPLAY_EDIT_GAME_ACCOUNT_ACTION_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id="staff-persona-display-game-account",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_game_accounts(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._persona_id,
            page=0,
            source_view=self.view,
        )


class StaffDisplayEditScopeView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        persona_display_name: str,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(
            discord.ui.TextDisplay(copy.format_display_edit_scope(persona_display_name=persona_display_name))
        )
        row = discord.ui.ActionRow()
        row.add_item(
            StaffDisplayEditPersonaButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                persona_id=persona_id,
            )
        )
        row.add_item(
            StaffDisplayEditGameAccountButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                persona_id=persona_id,
            )
        )
        row.add_item(
            StaffDisplayEditBackButton(
                navigation=navigation,
                context=context,
                persona_id=persona_id,
            )
        )
        container.add_item(row)
        self.add_item(container)


class StaffPersonaDisplayInputModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        state: StaffPersonaDisplayState,
        source_view: discord.ui.LayoutView | None,
        current: StaffPersonaDisplayPreview | None = None,
    ) -> None:
        super().__init__(title=copy.DISPLAY_EDIT_PERSONA_MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._state = state
        self._source_view = source_view
        self.display_name = discord.ui.TextInput(
            label=copy.DISPLAY_EDIT_PERSONA_NAME_LABEL,
            min_length=1,
            max_length=100,
            default=state.display_name if current is None else current.display_name,
        )
        self.reason = discord.ui.TextInput(
            label=copy.DISPLAY_EDIT_REASON_LABEL,
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=255,
            default=None if current is None else current.reason,
        )
        self.add_item(self.display_name)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_persona_preview(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._state.persona_id,
            display_name=str(self.display_name.value),
            reason=str(self.reason.value),
            source_view=self._source_view,
        )


class StaffPersonaDisplayConfirmButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        context: StaffPersonaInteractionContext,
        preview: StaffPersonaDisplayPreview,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        super().__init__(
            label=copy.DISPLAY_EDIT_PERSONA_CONFIRM_LABEL,
            style=discord.ButtonStyle.success,
            custom_id="staff-persona-display-persona-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.update_persona(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self.view,
        )


class StaffPersonaDisplayReenterButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffPersonaDisplayPreview,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._preview = preview
        super().__init__(
            label=copy.DISPLAY_EDIT_REENTER_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-display-persona-reenter",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_persona_input(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._preview.state.persona_id,
            source_view=self.view,
            current=self._preview,
        )


class StaffPersonaDisplayConfirmView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffPersonaDisplayPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_persona_display_preview(preview)))
        row = discord.ui.ActionRow()
        row.add_item(StaffPersonaDisplayConfirmButton(adapter=adapter, context=context, preview=preview))
        row.add_item(
            StaffPersonaDisplayReenterButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                preview=preview,
            )
        )
        row.add_item(
            StaffDisplayEditBackButton(
                navigation=navigation,
                context=context,
                persona_id=preview.state.persona_id,
            )
        )
        container.add_item(row)
        self.add_item(container)


class StaffDisplayGameAccountSelect(discord.ui.Select[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        page: StaffDisplayGameAccountPage,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._page = page
        self._items = {item.game_account_id: item for item in page.items}
        options = []
        for item in page.items:
            pid = "PID 없음" if item.uma_pid is None else copy.mask_pid(item.uma_pid)
            options.append(
                discord.SelectOption(
                    label=_bounded_label(f"{item.game_region.value} · {pid} · {item.nickname}"),
                    value=str(item.game_account_id),
                    description=_bounded_label(f"소속: {item.affiliation or '없음'}"),
                )
            )
        super().__init__(
            placeholder=copy.DISPLAY_EDIT_GAME_ACCOUNT_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=options,
            custom_id="staff-persona-display-game-account-select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_game_account_input(
            interaction,
            navigation=self._navigation,
            context=self._context,
            account=self._items[int(self.values[0])],
            page=self._page.page,
            source_view=self.view,
        )


class StaffDisplayGameAccountPageButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        page: int,
        direction: int,
        disabled: bool,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._persona_id = persona_id
        self._page = max(0, page + direction)
        super().__init__(
            label=copy.PREVIOUS_LABEL if direction < 0 else copy.NEXT_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"staff-persona-display-page-{'previous' if direction < 0 else 'next'}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_game_accounts(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._persona_id,
            page=self._page,
            source_view=self.view,
        )


class StaffDisplayGameAccountListView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        page: StaffDisplayGameAccountPage,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_display_game_account_page(page)))
        if page.items:
            select_row = discord.ui.ActionRow()
            select_row.add_item(
                StaffDisplayGameAccountSelect(
                    adapter=adapter,
                    navigation=navigation,
                    context=context,
                    page=page,
                )
            )
            container.add_item(select_row)
        navigation_row = discord.ui.ActionRow()
        navigation_row.add_item(
            StaffDisplayGameAccountPageButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                persona_id=page.persona.persona_id,
                page=page.page,
                direction=-1,
                disabled=not page.has_previous,
            )
        )
        navigation_row.add_item(
            StaffDisplayGameAccountPageButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                persona_id=page.persona.persona_id,
                page=page.page,
                direction=1,
                disabled=not page.has_next,
            )
        )
        navigation_row.add_item(
            StaffDisplayEditBackButton(
                navigation=navigation,
                context=context,
                persona_id=page.persona.persona_id,
            )
        )
        container.add_item(navigation_row)
        self.add_item(container)


class StaffGameAccountDisplayInputModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        state: StaffGameAccountDisplayState,
        page: int,
        source_view: discord.ui.LayoutView | None,
        current: StaffGameAccountDisplayPreview | None = None,
    ) -> None:
        super().__init__(title=copy.DISPLAY_EDIT_GAME_ACCOUNT_MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._state = state
        self._page = page
        self._source_view = source_view
        self.nickname = discord.ui.TextInput(
            label=copy.DISPLAY_EDIT_NICKNAME_LABEL,
            min_length=1,
            max_length=100,
            default=state.nickname if current is None else current.nickname,
        )
        self.affiliation = discord.ui.TextInput(
            label=copy.DISPLAY_EDIT_AFFILIATION_LABEL,
            required=False,
            max_length=100,
            default=state.affiliation if current is None else current.affiliation,
        )
        self.reason = discord.ui.TextInput(
            label=copy.DISPLAY_EDIT_REASON_LABEL,
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=255,
            default=None if current is None else current.reason,
        )
        self.add_item(self.nickname)
        self.add_item(self.affiliation)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_game_account_preview(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._state.persona_id,
            game_account_id=self._state.game_account_id,
            nickname=str(self.nickname.value),
            affiliation=str(self.affiliation.value),
            reason=str(self.reason.value),
            page=self._page,
            source_view=self._source_view,
        )


class StaffGameAccountDisplayConfirmButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        context: StaffPersonaInteractionContext,
        preview: StaffGameAccountDisplayPreview,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        super().__init__(
            label=copy.DISPLAY_EDIT_GAME_ACCOUNT_CONFIRM_LABEL,
            style=discord.ButtonStyle.success,
            custom_id="staff-persona-display-game-account-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.update_game_account(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self.view,
        )


class StaffGameAccountDisplayReenterButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffGameAccountDisplayPreview,
        page: int,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._preview = preview
        self._page = page
        super().__init__(
            label=copy.DISPLAY_EDIT_REENTER_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-display-game-account-reenter",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_game_account_input(
            interaction,
            navigation=self._navigation,
            context=self._context,
            account=self._preview.state,
            page=self._page,
            source_view=self.view,
            current=self._preview,
        )


class StaffGameAccountDisplayListBackButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        page: int,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._persona_id = persona_id
        self._page = page
        super().__init__(
            label=copy.BACK_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="staff-persona-display-game-account-list-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_game_accounts(
            interaction,
            navigation=self._navigation,
            context=self._context,
            persona_id=self._persona_id,
            page=self._page,
            source_view=self.view,
        )


class StaffGameAccountDisplayConfirmView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffDisplayEditDiscordAdapter,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        preview: StaffGameAccountDisplayPreview,
        page: int,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_game_account_display_preview(preview)))
        row = discord.ui.ActionRow()
        row.add_item(StaffGameAccountDisplayConfirmButton(adapter=adapter, context=context, preview=preview))
        row.add_item(
            StaffGameAccountDisplayReenterButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                preview=preview,
                page=page,
            )
        )
        row.add_item(
            StaffGameAccountDisplayListBackButton(
                adapter=adapter,
                navigation=navigation,
                context=context,
                persona_id=preview.state.persona_id,
                page=page,
            )
        )
        container.add_item(row)
        self.add_item(container)


@dataclass(frozen=True, slots=True)
class StaffDisplayEditDiscordAdapter:
    queries: StaffDisplayEditQueries
    commands: StaffDisplayEditCommands
    prepare_command: PrepareDiscordCommand
    authorize_interaction: AuthorizeDiscordInteraction
    run_application: BlockingApplicationRunner = run_blocking_application

    async def show_scope(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        persona_display_name: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            replacement = StaffDisplayEditScopeView(
                adapter=self,
                navigation=navigation,
                context=context,
                persona_id=persona_id,
                persona_display_name=persona_display_name,
            )
        except Exception:
            self._log_failure(interaction, operation="display-scope")
            await self._send_component_error(interaction, copy.display_edit_internal_error(correlation_id(interaction)))
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="display-scope",
        )

    async def open_persona_input(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        source_view: discord.ui.LayoutView | None,
        current: StaffPersonaDisplayPreview | None = None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            state = await self.run_application(
                lambda: self.queries.get_persona_state(
                    guild_id=str(context.guild_id),
                    persona_id=persona_id,
                )
            )
            await interaction.response.send_modal(
                StaffPersonaDisplayInputModal(
                    adapter=self,
                    navigation=navigation,
                    context=context,
                    state=state,
                    source_view=source_view,
                    current=current,
                )
            )
        except StaffDisplayEditPersonaNotFoundError:
            await self._send_component_error(interaction, copy.DISPLAY_EDIT_PERSONA_UNAVAILABLE)
        except Exception:
            self._log_failure(interaction, operation="persona-display-input")
            await self._send_component_error(interaction, copy.display_edit_internal_error(correlation_id(interaction)))

    async def show_persona_preview(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        display_name: str,
        reason: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            preview = await self.run_application(
                lambda: self.queries.get_persona_preview(
                    guild_id=str(context.guild_id),
                    persona_id=persona_id,
                    display_name=display_name,
                    reason=reason,
                )
            )
            replacement = StaffPersonaDisplayConfirmView(
                adapter=self,
                navigation=navigation,
                context=context,
                preview=preview,
            )
        except StaffDisplayEditPersonaNotFoundError:
            await self._send_component_error(interaction, copy.DISPLAY_EDIT_PERSONA_UNAVAILABLE)
            return
        except StaffDisplayEditNoChangeError:
            await self._send_component_error(interaction, copy.DISPLAY_EDIT_NO_CHANGE)
            return
        except (TypeError, ValueError):
            await self._send_component_error(interaction, copy.DISPLAY_EDIT_INVALID_INPUT)
            return
        except Exception:
            self._log_failure(interaction, operation="persona-display-preview")
            await self._send_component_error(interaction, copy.display_edit_internal_error(correlation_id(interaction)))
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="persona-display-preview",
        )

    async def show_game_accounts(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        page: int,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            projection = await self.run_application(
                lambda: self.queries.list_game_accounts(
                    guild_id=str(context.guild_id),
                    persona_id=persona_id,
                    page=page,
                )
            )
            replacement = StaffDisplayGameAccountListView(
                adapter=self,
                navigation=navigation,
                context=context,
                page=projection,
            )
        except StaffDisplayEditPersonaNotFoundError:
            await self._send_component_error(interaction, copy.DISPLAY_EDIT_PERSONA_UNAVAILABLE)
            return
        except Exception:
            self._log_failure(interaction, operation="display-account-list")
            await self._send_component_error(interaction, copy.display_edit_internal_error(correlation_id(interaction)))
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="display-account-list",
        )

    async def open_game_account_input(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        account: StaffGameAccountDisplayState,
        page: int,
        source_view: discord.ui.LayoutView | None,
        current: StaffGameAccountDisplayPreview | None = None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                StaffGameAccountDisplayInputModal(
                    adapter=self,
                    navigation=navigation,
                    context=context,
                    state=account,
                    page=page,
                    source_view=source_view,
                    current=current,
                )
            )
        except Exception:
            self._log_failure(interaction, operation="display-account-input")
            await self._send_component_error(interaction, copy.display_edit_internal_error(correlation_id(interaction)))

    async def show_game_account_preview(
        self,
        interaction: discord.Interaction,
        *,
        navigation: StaffDisplayEditNavigation,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        game_account_id: int,
        nickname: str,
        affiliation: str | None,
        reason: str,
        page: int,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            preview = await self.run_application(
                lambda: self.queries.get_game_account_preview(
                    guild_id=str(context.guild_id),
                    persona_id=persona_id,
                    game_account_id=game_account_id,
                    nickname=nickname,
                    affiliation=affiliation,
                    reason=reason,
                )
            )
            replacement = StaffGameAccountDisplayConfirmView(
                adapter=self,
                navigation=navigation,
                context=context,
                preview=preview,
                page=page,
            )
        except StaffDisplayEditGameAccountNotFoundError:
            await self._send_component_error(interaction, copy.DISPLAY_EDIT_GAME_ACCOUNT_UNAVAILABLE)
            return
        except StaffDisplayEditNoChangeError:
            await self._send_component_error(interaction, copy.DISPLAY_EDIT_NO_CHANGE)
            return
        except (TypeError, ValueError):
            await self._send_component_error(interaction, copy.DISPLAY_EDIT_INVALID_INPUT)
            return
        except Exception:
            self._log_failure(interaction, operation="display-account-preview")
            await self._send_component_error(interaction, copy.display_edit_internal_error(correlation_id(interaction)))
            return
        await self._replace_component_layout(
            interaction,
            view=replacement,
            source_view=source_view,
            operation="display-account-preview",
        )

    async def update_persona(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        preview: StaffPersonaDisplayPreview,
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
                lambda: self.commands.update_persona(
                    UpdatePersonaDisplayName(
                        guild_id=str(context.guild_id),
                        target_persona_id=preview.state.persona_id,
                        display_name=preview.display_name,
                        reason=preview.reason,
                        updated_by_discord_user_id=str(context.user_id),
                        expected_target_fingerprint=preview.state.state_fingerprint,
                        idempotency_key=key,
                        correlation_id=key,
                    )
                )
            )
            content = copy.format_persona_display_receipt(result)
        except StaffDisplayEditPersonaNotFoundError:
            content = copy.DISPLAY_EDIT_PERSONA_UNAVAILABLE
        except StaffDisplayEditNoChangeError:
            content = copy.DISPLAY_EDIT_NO_CHANGE
        except StaffDisplayEditStaleError:
            content = copy.DISPLAY_EDIT_STALE
        except StaffDisplayEditIdempotencyConflictError:
            content = copy.IDEMPOTENCY_CONFLICT
        except StaffDisplayEditConcurrentConflictError:
            content = copy.DISPLAY_EDIT_CONCURRENT_CONFLICT
        except StaffDisplayEditAuditError:
            self._log_failure(interaction, operation="persona-display-audit")
            content = copy.display_edit_internal_error(correlation_id(interaction))
        except (TypeError, ValueError):
            content = copy.DISPLAY_EDIT_INVALID_INPUT
        except Exception:
            self._log_failure(interaction, operation="persona-display-final")
            content = copy.display_edit_internal_error(correlation_id(interaction))
        await self._edit_deferred(interaction, content)
        self._stop(source_view)

    async def update_game_account(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        preview: StaffGameAccountDisplayPreview,
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
                lambda: self.commands.update_game_account(
                    UpdateGameAccountDisplayInfo(
                        guild_id=str(context.guild_id),
                        target_persona_id=preview.state.persona_id,
                        game_account_id=preview.state.game_account_id,
                        nickname=preview.nickname,
                        affiliation=preview.affiliation,
                        reason=preview.reason,
                        updated_by_discord_user_id=str(context.user_id),
                        expected_target_fingerprint=preview.state.state_fingerprint,
                        idempotency_key=key,
                        correlation_id=key,
                    )
                )
            )
            content = copy.format_game_account_display_receipt(result)
        except StaffDisplayEditGameAccountNotFoundError:
            content = copy.DISPLAY_EDIT_GAME_ACCOUNT_UNAVAILABLE
        except StaffDisplayEditNoChangeError:
            content = copy.DISPLAY_EDIT_NO_CHANGE
        except StaffDisplayEditStaleError:
            content = copy.DISPLAY_EDIT_STALE
        except StaffDisplayEditIdempotencyConflictError:
            content = copy.IDEMPOTENCY_CONFLICT
        except StaffDisplayEditConcurrentConflictError:
            content = copy.DISPLAY_EDIT_CONCURRENT_CONFLICT
        except StaffDisplayEditAuditError:
            self._log_failure(interaction, operation="display-account-audit")
            content = copy.display_edit_internal_error(correlation_id(interaction))
        except (TypeError, ValueError):
            content = copy.DISPLAY_EDIT_INVALID_INPUT
        except Exception:
            self._log_failure(interaction, operation="display-account-final")
            content = copy.display_edit_internal_error(correlation_id(interaction))
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
                "Discord Staff Persona display-edit component response failed correlation_id=%s",
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
                "Discord Staff Persona display-edit deferred response failed correlation_id=%s",
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
            "Discord Staff Persona display edit failed correlation_id=%s command=%s operation=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
            safe_discord_text(operation, limit=64),
        )
