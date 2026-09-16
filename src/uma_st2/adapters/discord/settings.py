"""Private Discord panel for audited runtime guild settings updates."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import StrEnum

import discord
from discord import app_commands

from uma_st2.application.discord import (
    DiscordGuildEditableSettings,
    DiscordGuildSettingsUpdateAuditError,
    DiscordGuildSettingsUpdateCommands,
    DiscordGuildSettingsUpdateConcurrentConflictError,
    DiscordGuildSettingsUpdateError,
    DiscordGuildSettingsUpdateIdempotencyConflictError,
    DiscordGuildSettingsUpdateInvalidSourceError,
    DiscordGuildSettingsUpdateNoChangeError,
    DiscordGuildSettingsUpdatePreview,
    DiscordGuildSettingsUpdateQueries,
    DiscordGuildSettingsUpdateStaleError,
    DiscordGuildSettingsUpdateState,
    DiscordGuildSettingsUpdateUnavailableError,
    UpdateDiscordGuildSettings,
)

from .common import (
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    PrepareDiscordCommand,
    bounded_discord_message,
    correlation_id,
    run_blocking_application,
)
from .strings import settings as copy

logger = logging.getLogger(__name__)

_COMMAND_NAME = "settings"
_COMPONENT_TIMEOUT_SECONDS = 600.0
_TEXT_CHANNEL_TYPES = (discord.ChannelType.text, discord.ChannelType.news)


class SettingsChannelValidationError(ValueError):
    """A current Discord destination or permission set is unsafe."""


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive Discord snowflake.")
    return value


@dataclass(frozen=True, slots=True)
class SettingsInteractionContext:
    """Opener and configured guild/channel binding for one settings workflow."""

    user_id: int
    guild_id: int
    channel_id: int

    @classmethod
    def from_interaction(cls, interaction: object) -> SettingsInteractionContext:
        return cls(
            user_id=_required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            ),
            guild_id=_required_snowflake(getattr(interaction, "guild_id", None), field_name="guild ID"),
            channel_id=_required_snowflake(
                getattr(interaction, "channel_id", None),
                field_name="channel ID",
            ),
        )

    def matches(self, interaction: object) -> bool:
        return (
            getattr(getattr(interaction, "user", None), "id", None) == self.user_id
            and getattr(interaction, "guild_id", None) == self.guild_id
            and getattr(interaction, "channel_id", None) == self.channel_id
        )


class SettingsChannelKind(StrEnum):
    WIN5 = "win5"
    MATCH = "match"
    LOG = "log"

    @property
    def label(self) -> str:
        return {
            SettingsChannelKind.WIN5: copy.WIN5_CHANNEL_LABEL,
            SettingsChannelKind.MATCH: copy.MATCH_CHANNEL_LABEL,
            SettingsChannelKind.LOG: copy.LOG_CHANNEL_LABEL,
        }[self]


@dataclass(frozen=True, slots=True)
class SettingsDraft:
    """Detached adapter-local settings draft; never canonical authority."""

    original: DiscordGuildSettingsUpdateState
    desired: DiscordGuildEditableSettings

    @classmethod
    def from_state(cls, state: DiscordGuildSettingsUpdateState) -> SettingsDraft:
        return cls(original=state, desired=state.editable)

    def channel_id(self, kind: SettingsChannelKind) -> str | None:
        if kind is SettingsChannelKind.WIN5:
            return self.desired.win5_announcement_channel_id
        if kind is SettingsChannelKind.MATCH:
            return self.desired.match_announcement_channel_id
        return self.desired.log_channel_id

    def with_channel(self, kind: SettingsChannelKind, channel_id: str | None) -> SettingsDraft:
        if kind is SettingsChannelKind.WIN5:
            desired = replace(self.desired, win5_announcement_channel_id=channel_id)
        elif kind is SettingsChannelKind.MATCH:
            desired = replace(self.desired, match_announcement_channel_id=channel_id)
        else:
            desired = replace(self.desired, log_channel_id=channel_id)
        return replace(self, desired=desired)

    def with_timezone(self, timezone: str) -> SettingsDraft:
        return replace(self, desired=replace(self.desired, default_timezone=timezone))

    def toggle_announcements(self, *, win5: bool) -> SettingsDraft:
        if win5:
            desired = replace(
                self.desired,
                win5_announcements_enabled=not self.desired.win5_announcements_enabled,
            )
        else:
            desired = replace(
                self.desired,
                match_announcements_enabled=not self.desired.match_announcements_enabled,
            )
        return replace(self, desired=desired)


class SettingsChannelButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        kind: SettingsChannelKind,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._kind = kind
        super().__init__(
            label=f"{kind.label} 변경",
            style=discord.ButtonStyle.secondary,
            custom_id=f"settings-channel-{kind.value}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_channel_editor(
            interaction,
            context=self._context,
            draft=self._draft,
            kind=self._kind,
            source_view=self.view,
        )


class SettingsTimezoneButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        super().__init__(
            label=copy.TIMEZONE_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="settings-timezone",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_timezone_modal(
            interaction,
            context=self._context,
            draft=self._draft,
            source_view=self.view,
        )


class SettingsToggleButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        win5: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._win5 = win5
        enabled = draft.desired.win5_announcements_enabled if win5 else draft.desired.match_announcements_enabled
        feature = "WIN5" if win5 else "Match"
        super().__init__(
            label=f"{feature} 공지 {'해제' if enabled else '사용'}",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success,
            custom_id=f"settings-toggle-{'win5' if win5 else 'match'}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.toggle_announcements(
            interaction,
            context=self._context,
            draft=self._draft,
            win5=self._win5,
            source_view=self.view,
        )


class SettingsReviewButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        super().__init__(
            label=copy.REVIEW_LABEL,
            style=discord.ButtonStyle.success,
            custom_id="settings-review",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_reason_modal(
            interaction,
            context=self._context,
            draft=self._draft,
            source_view=self.view,
        )


class SettingsCloseButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(self, *, adapter: SettingsDiscordAdapter, context: SettingsInteractionContext) -> None:
        self._adapter = adapter
        self._context = context
        super().__init__(
            label=copy.CANCEL_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="settings-close",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.close(
            interaction,
            context=self._context,
            source_view=self.view,
        )


class SettingsPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_panel(draft.original, draft.desired)))
        channels = discord.ui.ActionRow()
        for kind in SettingsChannelKind:
            channels.add_item(
                SettingsChannelButton(
                    adapter=adapter,
                    context=context,
                    draft=draft,
                    kind=kind,
                )
            )
        scalars = discord.ui.ActionRow()
        scalars.add_item(SettingsTimezoneButton(adapter=adapter, context=context, draft=draft))
        scalars.add_item(
            SettingsToggleButton(
                adapter=adapter,
                context=context,
                draft=draft,
                win5=True,
            )
        )
        scalars.add_item(
            SettingsToggleButton(
                adapter=adapter,
                context=context,
                draft=draft,
                win5=False,
            )
        )
        actions = discord.ui.ActionRow()
        actions.add_item(SettingsReviewButton(adapter=adapter, context=context, draft=draft))
        actions.add_item(SettingsCloseButton(adapter=adapter, context=context))
        container.add_item(channels)
        container.add_item(scalars)
        container.add_item(actions)
        self.add_item(container)


class SettingsChannelSelect(discord.ui.ChannelSelect[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        kind: SettingsChannelKind,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._kind = kind
        current = draft.channel_id(kind)
        super().__init__(
            custom_id=f"settings-channel-select-{kind.value}",
            channel_types=list(_TEXT_CHANNEL_TYPES),
            placeholder=f"{kind.label} 선택",
            min_values=1,
            max_values=1,
            default_values=(() if current is None else (discord.Object(id=int(current)),)),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.set_channel(
            interaction,
            context=self._context,
            draft=self._draft,
            kind=self._kind,
            channel_id=_required_snowflake(self.values[0].id, field_name="channel ID"),
            source_view=self.view,
        )


class SettingsChannelClearButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        kind: SettingsChannelKind,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._kind = kind
        super().__init__(
            label=copy.CLEAR_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id=f"settings-channel-clear-{kind.value}",
            disabled=draft.channel_id(kind) is None,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.clear_channel(
            interaction,
            context=self._context,
            draft=self._draft,
            kind=self._kind,
            source_view=self.view,
        )


class SettingsChannelBackButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        super().__init__(
            label=copy.BACK_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="settings-channel-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.return_to_editor(
            interaction,
            context=self._context,
            draft=self._draft,
            source_view=self.view,
        )


class SettingsChannelView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        kind: SettingsChannelKind,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(
            discord.ui.TextDisplay(copy.format_channel_editor(kind.label, draft.channel_id(kind)))
        )
        channel_row = discord.ui.ActionRow()
        channel_row.add_item(
            SettingsChannelSelect(
                adapter=adapter,
                context=context,
                draft=draft,
                kind=kind,
            )
        )
        actions = discord.ui.ActionRow()
        actions.add_item(
            SettingsChannelClearButton(
                adapter=adapter,
                context=context,
                draft=draft,
                kind=kind,
            )
        )
        actions.add_item(SettingsChannelBackButton(adapter=adapter, context=context, draft=draft))
        container.add_item(channel_row)
        container.add_item(actions)
        self.add_item(container)


class SettingsTimezoneModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        super().__init__(title=copy.TIMEZONE_MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._source_view = source_view
        self.timezone = discord.ui.TextInput(
            label=copy.TIMEZONE_LABEL,
            min_length=1,
            max_length=32,
            default=draft.desired.default_timezone,
            placeholder="Asia/Seoul",
        )
        self.add_item(self.timezone)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.set_timezone(
            interaction,
            context=self._context,
            draft=self._draft,
            timezone=str(self.timezone.value),
            source_view=self._source_view,
        )

    async def on_error(self, interaction: discord.Interaction, _error: Exception) -> None:
        await self._adapter.send_component_error(interaction, copy.INVALID_INPUT)


class SettingsReasonModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        super().__init__(title=copy.REASON_MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._source_view = source_view
        self.reason = discord.ui.TextInput(
            label=copy.REASON_LABEL,
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=255,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_preview(
            interaction,
            context=self._context,
            draft=self._draft,
            reason=str(self.reason.value),
            source_view=self._source_view,
        )

    async def on_error(self, interaction: discord.Interaction, _error: Exception) -> None:
        await self._adapter.send_component_error(interaction, copy.INVALID_INPUT)


class SettingsConfirmButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        preview: DiscordGuildSettingsUpdatePreview,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        super().__init__(
            label=copy.CONFIRM_LABEL,
            style=discord.ButtonStyle.success,
            custom_id="settings-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.confirm(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self.view,
        )


class SettingsEditButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        preview: DiscordGuildSettingsUpdatePreview,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        super().__init__(
            label=copy.EDIT_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="settings-edit",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.return_to_editor(
            interaction,
            context=self._context,
            draft=SettingsDraft(original=self._preview.before, desired=self._preview.desired),
            source_view=self.view,
        )


class SettingsConfirmView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        preview: DiscordGuildSettingsUpdatePreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_preview(preview)))
        actions = discord.ui.ActionRow()
        actions.add_item(
            SettingsConfirmButton(
                adapter=adapter,
                context=context,
                preview=preview,
            )
        )
        actions.add_item(SettingsEditButton(adapter=adapter, context=context, preview=preview))
        actions.add_item(SettingsCloseButton(adapter=adapter, context=context))
        container.add_item(actions)
        self.add_item(container)


@dataclass(frozen=True, slots=True)
class SettingsDiscordAdapter:
    """Translate the private settings editor into narrow Application ports."""

    queries: DiscordGuildSettingsUpdateQueries
    commands: DiscordGuildSettingsUpdateCommands
    prepare_command: PrepareDiscordCommand
    authorize_interaction: AuthorizeDiscordInteraction
    run_application: BlockingApplicationRunner = run_blocking_application

    async def open_panel(self, interaction: discord.Interaction) -> None:
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            context = SettingsInteractionContext.from_interaction(interaction)
            state = await self.run_application(lambda: self.queries.get_state(guild_id=str(context.guild_id)))
            await interaction.edit_original_response(
                content=None,
                view=SettingsPanelView(
                    adapter=self,
                    context=context,
                    draft=SettingsDraft.from_state(state),
                ),
            )
        except (DiscordGuildSettingsUpdateUnavailableError, DiscordGuildSettingsUpdateInvalidSourceError):
            await self._send_final_error(interaction, copy.UNAVAILABLE)
        except Exception:
            self._log_failure(interaction, operation="open")
            await self._send_final_error(interaction, copy.INTERNAL_ERROR)

    async def show_channel_editor(
        self,
        interaction: discord.Interaction,
        *,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        kind: SettingsChannelKind,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_component_update(
            interaction,
            context=context,
            operation="show-channel-editor",
            source_view=source_view,
        ):
            return
        await self._replace_view(
            interaction,
            source_view=source_view,
            replacement=SettingsChannelView(
                adapter=self,
                context=context,
                draft=draft,
                kind=kind,
            ),
        )

    async def set_channel(
        self,
        interaction: discord.Interaction,
        *,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        kind: SettingsChannelKind,
        channel_id: int,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_component_update(
            interaction,
            context=context,
            operation="set-channel",
            source_view=source_view,
        ):
            return
        try:
            await self._validate_channel(
                interaction,
                state=draft.original,
                kind=kind,
                channel_id=str(channel_id),
            )
            updated = draft.with_channel(kind, str(channel_id))
        except (
            DiscordGuildSettingsUpdateError,
            SettingsChannelValidationError,
            discord.HTTPException,
        ):
            await self.send_component_error(interaction, copy.INVALID_CHANNEL)
            await self._replace_view(
                interaction,
                source_view=source_view,
                replacement=SettingsPanelView(adapter=self, context=context, draft=draft),
            )
            return
        await self._replace_view(
            interaction,
            source_view=source_view,
            replacement=SettingsPanelView(adapter=self, context=context, draft=updated),
        )

    async def clear_channel(
        self,
        interaction: discord.Interaction,
        *,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        kind: SettingsChannelKind,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_component_update(
            interaction,
            context=context,
            operation="clear-channel",
            source_view=source_view,
        ):
            return
        updated = draft.with_channel(kind, None)
        await self._replace_view(
            interaction,
            source_view=source_view,
            replacement=SettingsPanelView(adapter=self, context=context, draft=updated),
        )

    async def open_timezone_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        source_view: discord.ui.LayoutView | None = None,
    ) -> None:
        if not await self._component_allowed(interaction, context=context):
            return
        await interaction.response.send_modal(
            SettingsTimezoneModal(
                adapter=self,
                context=context,
                draft=draft,
                source_view=source_view,
            )
        )

    async def set_timezone(
        self,
        interaction: discord.Interaction,
        *,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        timezone: str,
        source_view: discord.ui.LayoutView | None = None,
    ) -> None:
        if not await self._prepare_component_update(
            interaction,
            context=context,
            operation="set-timezone",
            source_view=source_view,
        ):
            return
        try:
            updated = draft.with_timezone(timezone)
        except DiscordGuildSettingsUpdateError:
            await self.send_component_error(interaction, copy.INVALID_INPUT)
            return
        await self._replace_view(
            interaction,
            source_view=source_view,
            replacement=SettingsPanelView(adapter=self, context=context, draft=updated),
        )

    async def toggle_announcements(
        self,
        interaction: discord.Interaction,
        *,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        win5: bool,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_component_update(
            interaction,
            context=context,
            operation="toggle-announcements",
            source_view=source_view,
        ):
            return
        updated = draft.toggle_announcements(win5=win5)
        await self._replace_view(
            interaction,
            source_view=source_view,
            replacement=SettingsPanelView(adapter=self, context=context, draft=updated),
        )

    async def open_reason_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        source_view: discord.ui.LayoutView | None = None,
    ) -> None:
        if not await self._component_allowed(interaction, context=context):
            return
        await interaction.response.send_modal(
            SettingsReasonModal(
                adapter=self,
                context=context,
                draft=draft,
                source_view=source_view,
            )
        )

    async def show_preview(
        self,
        interaction: discord.Interaction,
        *,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        reason: str,
        source_view: discord.ui.LayoutView | None = None,
    ) -> None:
        if not await self._prepare_component_update(
            interaction,
            context=context,
            operation="show-preview",
            source_view=source_view,
        ):
            return
        try:
            preview = await self.run_application(
                lambda: self.queries.get_preview(
                    guild_id=str(context.guild_id),
                    desired=draft.desired,
                    reason=reason,
                    expected_state_fingerprint=draft.original.state_fingerprint,
                )
            )
            await self._validate_destinations(interaction, preview.before, preview.desired)
            await self._replace_view(
                interaction,
                source_view=source_view,
                replacement=SettingsConfirmView(
                    adapter=self,
                    context=context,
                    preview=preview,
                ),
            )
        except DiscordGuildSettingsUpdateNoChangeError:
            await self.send_component_error(interaction, copy.NO_CHANGE)
        except DiscordGuildSettingsUpdateStaleError:
            await self._close_view_with_error(
                interaction,
                source_view=source_view,
                content=copy.STALE,
            )
        except (DiscordGuildSettingsUpdateUnavailableError, DiscordGuildSettingsUpdateInvalidSourceError):
            await self._close_view_with_error(
                interaction,
                source_view=source_view,
                content=copy.UNAVAILABLE,
            )
        except (SettingsChannelValidationError, discord.HTTPException):
            await self.send_component_error(interaction, copy.INVALID_CHANNEL)
        except DiscordGuildSettingsUpdateError:
            await self.send_component_error(interaction, copy.INVALID_INPUT)
        except Exception:
            self._log_failure(interaction, operation="preview")
            await self._close_view_with_error(
                interaction,
                source_view=source_view,
                content=copy.INTERNAL_ERROR,
            )

    async def return_to_editor(
        self,
        interaction: discord.Interaction,
        *,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_component_update(
            interaction,
            context=context,
            operation="return-to-editor",
            source_view=source_view,
        ):
            return
        await self._replace_view(
            interaction,
            source_view=source_view,
            replacement=SettingsPanelView(adapter=self, context=context, draft=draft),
        )

    async def close(
        self,
        interaction: discord.Interaction,
        *,
        context: SettingsInteractionContext,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_component_update(
            interaction,
            context=context,
            operation="close",
            source_view=source_view,
        ):
            return
        await self._deliver_terminal(
            interaction,
            message=copy.CLOSED,
            operation="close-receipt",
        )
        self._stop(source_view)

    async def confirm(
        self,
        interaction: discord.Interaction,
        *,
        context: SettingsInteractionContext,
        preview: DiscordGuildSettingsUpdatePreview,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_component_update(
            interaction,
            context=context,
            operation="confirm",
            source_view=source_view,
        ):
            return
        try:
            await self._validate_destinations(interaction, preview.before, preview.desired)
        except (SettingsChannelValidationError, discord.HTTPException):
            await self._send_final_error(interaction, copy.INVALID_CHANNEL)
            self._stop(source_view)
            return

        try:
            result = await self.run_application(
                lambda: self.commands.update(
                    UpdateDiscordGuildSettings(
                        guild_id=str(context.guild_id),
                        desired=preview.desired,
                        reason=preview.reason,
                        expected_state_fingerprint=preview.before.state_fingerprint,
                        actor_discord_user_id=str(context.user_id),
                        idempotency_key=correlation_id(interaction),
                        correlation_id=correlation_id(interaction),
                    )
                )
            )
        except DiscordGuildSettingsUpdateNoChangeError:
            await self._send_final_error(interaction, copy.NO_CHANGE)
        except DiscordGuildSettingsUpdateStaleError:
            await self._send_final_error(interaction, copy.STALE)
        except DiscordGuildSettingsUpdateUnavailableError:
            await self._send_final_error(interaction, copy.UNAVAILABLE)
        except DiscordGuildSettingsUpdateIdempotencyConflictError:
            await self._send_final_error(interaction, copy.IDEMPOTENCY_CONFLICT)
        except DiscordGuildSettingsUpdateConcurrentConflictError:
            await self._send_final_error(interaction, copy.CONCURRENT_CONFLICT)
        except (DiscordGuildSettingsUpdateInvalidSourceError, DiscordGuildSettingsUpdateAuditError):
            self._log_failure(interaction, operation="confirm")
            await self._send_final_error(interaction, copy.INTERNAL_ERROR)
        except DiscordGuildSettingsUpdateError:
            await self._send_final_error(interaction, copy.INVALID_INPUT)
        except Exception:
            self._log_failure(interaction, operation="confirm")
            await self._send_final_error(interaction, copy.INTERNAL_ERROR)
        else:
            await self._deliver_terminal(
                interaction,
                message=copy.format_success(result),
                operation="confirm-receipt",
            )
        self._stop(source_view)

    async def _component_allowed(
        self,
        interaction: discord.Interaction,
        *,
        context: SettingsInteractionContext,
    ) -> bool:
        if not context.matches(interaction):
            await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return False
        return await self.authorize_interaction(interaction, _COMMAND_NAME)

    async def _prepare_component_update(
        self,
        interaction: discord.Interaction,
        *,
        context: SettingsInteractionContext,
        operation: str,
        source_view: discord.ui.LayoutView | None,
    ) -> bool:
        if not context.matches(interaction):
            await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return False
        if source_view is not None and source_view.is_finished():
            await self.send_component_error(interaction, copy.EXPIRED_VIEW)
            return False
        try:
            await interaction.response.defer(thinking=False)
        except Exception:
            self._log_failure(interaction, operation=f"{operation}-defer")
            return False
        authorized = await self.authorize_interaction(interaction, _COMMAND_NAME)
        if not authorized:
            await self._close_view(interaction, source_view=source_view)
        return authorized

    async def _validate_destinations(
        self,
        interaction: discord.Interaction,
        state: DiscordGuildSettingsUpdateState,
        desired: DiscordGuildEditableSettings,
    ) -> None:
        for kind, channel_id in (
            (SettingsChannelKind.WIN5, desired.win5_announcement_channel_id),
            (SettingsChannelKind.MATCH, desired.match_announcement_channel_id),
            (SettingsChannelKind.LOG, desired.log_channel_id),
        ):
            if channel_id is not None:
                await self._validate_channel(
                    interaction,
                    state=state,
                    kind=kind,
                    channel_id=channel_id,
                )

    @staticmethod
    async def _validate_channel(
        interaction: discord.Interaction,
        *,
        state: DiscordGuildSettingsUpdateState,
        kind: SettingsChannelKind,
        channel_id: str,
    ) -> None:
        guild = getattr(interaction, "guild", None)
        if guild is None or getattr(guild, "id", None) != int(state.guild_id):
            raise SettingsChannelValidationError("The selected channel guild is unavailable.")
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            channel = await guild.fetch_channel(int(channel_id))
        if (
            getattr(getattr(channel, "guild", None), "id", None) != int(state.guild_id)
            or getattr(channel, "type", None) not in _TEXT_CHANNEL_TYPES
            or not callable(getattr(channel, "permissions_for", None))
            or not callable(getattr(channel, "send", None))
        ):
            raise SettingsChannelValidationError("The selected destination is not a guild text channel.")

        bot_member = getattr(guild, "me", None)
        if bot_member is None:
            raise SettingsChannelValidationError("The bot guild member is unavailable.")
        bot_permissions = channel.permissions_for(bot_member)
        if not bool(getattr(bot_permissions, "view_channel", False)) or not bool(
            getattr(bot_permissions, "send_messages", False)
        ):
            raise SettingsChannelValidationError("The bot cannot view and send to the selected channel.")

        everyone = getattr(guild, "default_role", None)
        if everyone is None:
            raise SettingsChannelValidationError("The guild default Role is unavailable.")
        everyone_can_view = bool(getattr(channel.permissions_for(everyone), "view_channel", False))
        if kind is not SettingsChannelKind.LOG:
            if not everyone_can_view:
                raise SettingsChannelValidationError("Announcement destinations must be public.")
            return
        if everyone_can_view:
            raise SettingsChannelValidationError("The log destination must be hidden from @everyone.")
        for role_id in (state.operator_role_id, state.bot_manager_role_id):
            if role_id is None:
                continue
            role = guild.get_role(int(role_id))
            if role is None or not bool(getattr(channel.permissions_for(role), "view_channel", False)):
                raise SettingsChannelValidationError("Configured staff Roles must be able to view the log destination.")

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
                "Discord settings component error delivery failed correlation_id=%s",
                correlation_id(interaction),
            )

    async def _send_final_error(self, interaction: discord.Interaction, content: str) -> None:
        await self._deliver_terminal(
            interaction,
            message=content,
            operation="final-error-delivery",
        )

    async def _deliver_terminal(
        self,
        interaction: discord.Interaction,
        *,
        message: str,
        operation: str,
    ) -> None:
        try:
            await self._edit_terminal(interaction, message=message)
        except Exception:
            self._log_failure(interaction, operation=operation)
            await self.send_component_error(interaction, message)

    @classmethod
    async def _edit_terminal(cls, interaction: discord.Interaction, *, message: str) -> None:
        await interaction.edit_original_response(
            content=None,
            embeds=[],
            attachments=[],
            view=cls._terminal_layout(message),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @staticmethod
    def _terminal_layout(message: str) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=_COMPONENT_TIMEOUT_SECONDS)
        view.add_item(discord.ui.Container(discord.ui.TextDisplay(bounded_discord_message((message,), limit=3500))))
        return view

    async def _close_view_with_error(
        self,
        interaction: discord.Interaction,
        *,
        source_view: discord.ui.LayoutView | None,
        content: str,
    ) -> None:
        await self._send_final_error(interaction, content)
        self._stop(source_view)

    async def _close_view(
        self,
        interaction: discord.Interaction,
        *,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        await self._deliver_terminal(
            interaction,
            message=copy.AUTHORIZATION_LOST,
            operation="close-invalid-view",
        )
        self._stop(source_view)

    @staticmethod
    def _stop(view: discord.ui.LayoutView | None) -> None:
        if view is not None:
            view.stop()

    async def _replace_view(
        self,
        interaction: discord.Interaction,
        *,
        source_view: discord.ui.LayoutView | None,
        replacement: discord.ui.LayoutView,
    ) -> None:
        # discord.py stores the replacement before edit_original_response returns.
        # Stop the old dispatcher first so overlapping custom IDs remain registered.
        self._stop(source_view)
        try:
            await interaction.edit_original_response(content=None, view=replacement)
        except Exception:
            self._log_failure(interaction, operation="replace-view")
            await self.send_component_error(interaction, copy.REOPEN_SETTINGS)

    @staticmethod
    def _log_failure(interaction: object, *, operation: str) -> None:
        logger.error(
            "Discord settings operation failed correlation_id=%s operation=%s actor_id=%s guild_id=%s",
            correlation_id(interaction),
            operation,
            getattr(getattr(interaction, "user", None), "id", "unavailable"),
            getattr(interaction, "guild_id", "unavailable"),
        )


def create_settings_command(adapter: SettingsDiscordAdapter) -> app_commands.Command:
    """Create the direct `/settings` root command without a synthetic group."""

    async def settings(interaction: discord.Interaction) -> None:
        await adapter.open_panel(interaction)

    return app_commands.Command(
        name=_COMMAND_NAME,
        description=copy.COMMAND_DESCRIPTION,
        callback=settings,
    )
