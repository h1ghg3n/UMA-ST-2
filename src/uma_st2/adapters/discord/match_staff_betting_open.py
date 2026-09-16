"""Discord adapter for native Match betting-open."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

import discord
from discord import app_commands

from uma_st2.application.match import (
    MatchBettingOpenAuditError,
    MatchBettingOpenCommands,
    MatchBettingOpenError,
    MatchBettingOpenIdempotencyConflictError,
    MatchBettingOpenStaleError,
    MatchBettingOpenTarget,
    MatchBettingOpenUnavailableError,
    MatchStaffBettingOpenQueries,
    MatchStaffBettingOpenQueryError,
    OpenMatchBetting,
)

from .common import (
    AuthorizeDiscordAutocomplete,
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    buttonless_terminal_layout,
    correlation_id,
    run_blocking_application,
)
from .match_staff import MatchStaffInteractionContext
from .strings.match_staff_betting_open import (
    BOUND_CONFIRM_ERROR,
    BOUND_INTERACTION_ERROR,
    CANCEL_LABEL,
    CANCELLED,
    CONFIRM_LABEL,
    CONFIRMATION_STARTED,
    IDEMPOTENCY_CONFLICT,
    NEXT_LABEL,
    PREVIOUS_LABEL,
    STALE,
    UNAVAILABLE,
    format_match_betting_open_success,
    format_match_betting_open_target,
    match_betting_open_autocomplete_choices,
    opening_error,
    opening_internal_error,
    preview_error,
    preview_internal_error,
)
from .strings.match_staff_workflows import RACE_TRANSITION_ERROR

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.staff.betting-open"
_COMPONENT_TIMEOUT_SECONDS = 600.0
_PREVIEW_PAGE_SIZE = 8


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} is unavailable.")
    return value


@dataclass(frozen=True, slots=True)
class MatchBettingOpenPreview:
    """Closed query DTO plus adapter-local complete-review state."""

    target: MatchBettingOpenTarget
    page: int = 0
    reviewed_pages: frozenset[int] = field(default_factory=lambda: frozenset({0}))

    def __post_init__(self) -> None:
        if not isinstance(self.target, MatchBettingOpenTarget):
            raise ValueError("target must be MatchBettingOpenTarget.")
        if not 0 <= self.page < self.page_count:
            raise ValueError("page is outside the betting-open preview range.")
        if not self.reviewed_pages or any(not 0 <= page < self.page_count for page in self.reviewed_pages):
            raise ValueError("reviewed_pages contains an invalid preview page.")
        object.__setattr__(self, "reviewed_pages", frozenset((*self.reviewed_pages, self.page)))

    @property
    def page_count(self) -> int:
        return max(1, (len(self.target.entries) + _PREVIEW_PAGE_SIZE - 1) // _PREVIEW_PAGE_SIZE)

    @property
    def all_pages_reviewed(self) -> bool:
        return self.reviewed_pages == frozenset(range(self.page_count))

    @property
    def can_confirm(self) -> bool:
        return not self.target.readiness_issues and self.all_pages_reviewed

    def viewed(self, page: int) -> MatchBettingOpenPreview:
        return replace(self, page=page, reviewed_pages=frozenset((*self.reviewed_pages, page)))


def format_match_betting_open_preview(preview: MatchBettingOpenPreview) -> str:
    """Render a bounded complete opening projection review page."""

    return format_match_betting_open_target(
        target=preview.target,
        page=preview.page,
        page_count=preview.page_count,
        all_pages_reviewed=preview.all_pages_reviewed,
        page_size=_PREVIEW_PAGE_SIZE,
    )


class MatchBettingOpenPageButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchBettingOpenDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchBettingOpenPreview,
        page: int,
        label: str,
        source_view: MatchBettingOpenPreviewView,
        disabled: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._page = page
        self._source_view = source_view
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=f"match-betting-open-page-{label}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_preview_page(
            interaction,
            context=self._context,
            preview=self._preview.viewed(self._page),
            source_view=self._source_view,
        )


class MatchBettingOpenConfirmButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchBettingOpenDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchBettingOpenPreview,
        source_view: MatchBettingOpenPreviewView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._source_view = source_view
        super().__init__(
            label=CONFIRM_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id="match-betting-open-confirm",
            disabled=not preview.can_confirm,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.confirm_opening(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self._source_view,
        )


class MatchBettingOpenCancelButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchBettingOpenDiscordAdapter,
        context: MatchStaffInteractionContext,
        source_view: MatchBettingOpenPreviewView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._source_view = source_view
        super().__init__(
            label=CANCEL_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-betting-open-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.cancel_opening(
            interaction,
            context=self._context,
            source_view=self._source_view,
        )


class MatchBettingOpenPreviewView(discord.ui.LayoutView):
    """Bound paged opening Preview that retains no Session or transaction."""

    def __init__(
        self,
        *,
        adapter: MatchBettingOpenDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchBettingOpenPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.preview = preview
        self._confirmation_started = False
        container = discord.ui.Container(discord.ui.TextDisplay(format_match_betting_open_preview(preview)))
        if preview.page_count > 1:
            pages = discord.ui.ActionRow()
            pages.add_item(
                MatchBettingOpenPageButton(
                    adapter=adapter,
                    context=context,
                    preview=preview,
                    page=max(0, preview.page - 1),
                    label=PREVIOUS_LABEL,
                    source_view=self,
                    disabled=preview.page == 0,
                )
            )
            pages.add_item(
                discord.ui.Button(
                    label=f"{preview.page + 1} / {preview.page_count}",
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                )
            )
            pages.add_item(
                MatchBettingOpenPageButton(
                    adapter=adapter,
                    context=context,
                    preview=preview,
                    page=min(preview.page_count - 1, preview.page + 1),
                    label=NEXT_LABEL,
                    source_view=self,
                    disabled=preview.page == preview.page_count - 1,
                )
            )
            container.add_item(pages)
        actions = discord.ui.ActionRow()
        actions.add_item(
            MatchBettingOpenConfirmButton(
                adapter=adapter,
                context=context,
                preview=preview,
                source_view=self,
            )
        )
        actions.add_item(MatchBettingOpenCancelButton(adapter=adapter, context=context, source_view=self))
        container.add_item(actions)
        self.add_item(container)

    def claim_confirmation(self) -> bool:
        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True


@dataclass(frozen=True, slots=True)
class MatchBettingOpenDiscordAdapter:
    """Translate one bound staff flow into query and command runner calls."""

    queries: MatchStaffBettingOpenQueries
    commands: MatchBettingOpenCommands
    authorize_autocomplete: AuthorizeDiscordAutocomplete
    authorize_interaction: AuthorizeDiscordInteraction
    blocking_runner: BlockingApplicationRunner = run_blocking_application

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        try:
            if not await self.authorize_autocomplete(interaction, _COMMAND_NAME):
                return []
            targets = await self.blocking_runner(lambda: self.queries.search_targets(search=current, limit=25))
            return match_betting_open_autocomplete_choices(targets)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return []

    async def preview_opening(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="betting-open-preview",
        ):
            return
        source_view.stop()
        try:
            target = await self.blocking_runner(
                lambda: self.queries.get_target(match_id=match_id, guild_id=str(context.guild_id))
            )
            preview = MatchBettingOpenPreview(target=target)
            await self._edit_layout(
                interaction,
                view=MatchBettingOpenPreviewView(adapter=self, context=context, preview=preview),
            )
        except (MatchStaffBettingOpenQueryError, TypeError, ValueError) as error:
            await self._edit_deferred(interaction, preview_error(error))
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                preview_internal_error(correlation_id(interaction)),
            )

    async def show_preview_page(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        preview: MatchBettingOpenPreview,
        source_view: MatchBettingOpenPreviewView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="betting-open-preview-page",
        ):
            return
        source_view.stop()
        await self._edit_layout(
            interaction,
            view=MatchBettingOpenPreviewView(adapter=self, context=context, preview=preview),
        )

    async def confirm_opening(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        preview: MatchBettingOpenPreview,
        source_view: MatchBettingOpenPreviewView,
    ) -> None:
        if not context.matches(interaction):
            await self._send_component_error(
                interaction,
                BOUND_CONFIRM_ERROR,
            )
            return
        if not await self._defer_message_update(interaction, response_kind="betting-open-final"):
            return
        if not await self.authorize_interaction(interaction, _COMMAND_NAME):
            return
        if not preview.can_confirm:
            source_view.stop()
            await self._edit_layout(
                interaction,
                view=MatchBettingOpenPreviewView(adapter=self, context=context, preview=preview),
            )
            return
        if not source_view.claim_confirmation():
            source_view.stop()
            await self._edit_deferred(interaction, CONFIRMATION_STARTED)
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            result = await self.blocking_runner(
                lambda: self.commands.open_betting(
                    OpenMatchBetting(
                        match_id=preview.target.match_id,
                        expected_state_fingerprint=preview.target.state_fingerprint,
                        idempotency_key=f"match-betting-open:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except MatchBettingOpenIdempotencyConflictError:
            message = IDEMPOTENCY_CONFLICT
        except MatchBettingOpenStaleError:
            message = STALE
        except MatchBettingOpenUnavailableError:
            message = UNAVAILABLE
        except (MatchBettingOpenAuditError, MatchBettingOpenError, TypeError, ValueError) as error:
            message = opening_error(error)
        except Exception:
            self._log_application_failure(interaction)
            message = opening_internal_error(correlation_id(interaction))
        else:
            message = format_match_betting_open_success(result)
        await self._edit_terminal(interaction, message=message)

    async def cancel_opening(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: MatchBettingOpenPreviewView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="betting-open-cancel",
        ):
            return
        source_view.stop()
        await self._edit_terminal(interaction, message=CANCELLED)

    async def _prepare_bound_update(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        response_kind: str,
    ) -> bool:
        if not context.matches(interaction):
            await self._send_component_error(interaction, BOUND_INTERACTION_ERROR)
            return False
        if not await self._defer_message_update(interaction, response_kind=response_kind):
            return False
        return await self.authorize_interaction(interaction, _COMMAND_NAME)

    async def _defer_message_update(
        self,
        interaction: discord.Interaction,
        *,
        response_kind: str,
    ) -> bool:
        try:
            await interaction.response.defer(thinking=False)
        except Exception as error:
            self._log_delivery_failure(interaction, f"{response_kind}-defer", error=error)
            return False
        return True

    @staticmethod
    async def _edit_layout(interaction: discord.Interaction, *, view: discord.ui.LayoutView) -> None:
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
                _COMMAND_NAME,
                type(error).__name__,
            )
            await MatchBettingOpenDiscordAdapter._send_reopen_notice(interaction)

    @classmethod
    async def _edit_terminal(cls, interaction: discord.Interaction, *, message: str) -> None:
        await cls._edit_layout(interaction, view=cls._terminal_layout(message))

    @classmethod
    async def _edit_deferred(cls, interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.edit_original_response(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=cls._terminal_layout(content),
            )
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=deferred",
                correlation_id(interaction),
                _COMMAND_NAME,
            )

    @staticmethod
    async def _send_component_error(interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.response.send_message(
                content,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=component-error",
                correlation_id(interaction),
                _COMMAND_NAME,
            )

    @staticmethod
    def _terminal_layout(message: str) -> discord.ui.LayoutView:
        return buttonless_terminal_layout(message, timeout_seconds=_COMPONENT_TIMEOUT_SECONDS)

    @staticmethod
    async def _send_reopen_notice(interaction: discord.Interaction) -> None:
        try:
            await interaction.followup.send(
                RACE_TRANSITION_ERROR,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=reopen-notice",
                correlation_id(interaction),
                _COMMAND_NAME,
            )

    @staticmethod
    def _log_application_failure(interaction: object) -> None:
        logger.error(
            "Discord application call failed correlation_id=%s command=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
        )

    @staticmethod
    def _log_delivery_failure(
        interaction: object,
        response_kind: str,
        *,
        error: Exception | None = None,
    ) -> None:
        logger.error(
            "Discord response failed correlation_id=%s command=%s response_kind=%s error_type=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
            response_kind,
            type(error).__name__ if error is not None else "unknown",
        )
