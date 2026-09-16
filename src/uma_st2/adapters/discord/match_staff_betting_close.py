"""Discord adapter for native Match betting-close."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from uma_st2.application.match import (
    CloseMatchBetting,
    MatchBettingCloseAuditError,
    MatchBettingCloseCommands,
    MatchBettingCloseError,
    MatchBettingCloseIdempotencyConflictError,
    MatchBettingClosePreviewTarget,
    MatchBettingCloseUnavailableError,
    MatchStaffBettingCloseQueries,
    MatchStaffBettingCloseQueryError,
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
from .strings.match_staff_betting_close import (
    BOUND_CONFIRM_ERROR,
    BOUND_INTERACTION_ERROR,
    CANCEL_LABEL,
    CANCELLED,
    CONFIRM_LABEL,
    CONFIRMATION_STARTED,
    IDEMPOTENCY_CONFLICT,
    UNAVAILABLE,
    close_error,
    close_internal_error,
    format_match_betting_close_success,
    format_match_betting_close_target,
    match_betting_close_autocomplete_choices,
    preview_error,
    preview_internal_error,
)
from .strings.match_staff_workflows import RACE_TRANSITION_ERROR

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.staff.betting-close"
_COMPONENT_TIMEOUT_SECONDS = 600.0


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} is unavailable.")
    return value


@dataclass(frozen=True, slots=True)
class MatchBettingClosePreview:
    """Adapter-local wrapper around one closed query DTO."""

    target: MatchBettingClosePreviewTarget

    def __post_init__(self) -> None:
        if not isinstance(self.target, MatchBettingClosePreviewTarget):
            raise ValueError("target must be MatchBettingClosePreviewTarget.")


def format_match_betting_close_preview(preview: MatchBettingClosePreview) -> str:
    """Render one bounded private close confirmation."""

    return format_match_betting_close_target(preview.target)


class MatchBettingCloseConfirmButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchBettingCloseDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchBettingClosePreview,
        source_view: MatchBettingClosePreviewView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._source_view = source_view
        super().__init__(
            label=CONFIRM_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id="match-betting-close-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.confirm_close(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self._source_view,
        )


class MatchBettingCloseCancelButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchBettingCloseDiscordAdapter,
        context: MatchStaffInteractionContext,
        source_view: MatchBettingClosePreviewView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._source_view = source_view
        super().__init__(
            label=CANCEL_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-betting-close-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.cancel_close(
            interaction,
            context=self._context,
            source_view=self._source_view,
        )


class MatchBettingClosePreviewView(discord.ui.LayoutView):
    """Bound close Preview that retains no Session or transaction."""

    def __init__(
        self,
        *,
        adapter: MatchBettingCloseDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchBettingClosePreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.preview = preview
        self._confirmation_started = False
        container = discord.ui.Container(discord.ui.TextDisplay(format_match_betting_close_preview(preview)))
        actions = discord.ui.ActionRow()
        actions.add_item(
            MatchBettingCloseConfirmButton(
                adapter=adapter,
                context=context,
                preview=preview,
                source_view=self,
            )
        )
        actions.add_item(MatchBettingCloseCancelButton(adapter=adapter, context=context, source_view=self))
        container.add_item(actions)
        self.add_item(container)

    def claim_confirmation(self) -> bool:
        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True


@dataclass(frozen=True, slots=True)
class MatchBettingCloseDiscordAdapter:
    """Translate one bound staff close flow into query and command calls."""

    queries: MatchStaffBettingCloseQueries
    commands: MatchBettingCloseCommands
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
            return match_betting_close_autocomplete_choices(targets)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return []

    async def preview_close(
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
            response_kind="betting-close-preview",
        ):
            return
        source_view.stop()
        try:
            target = await self.blocking_runner(lambda: self.queries.get_target(match_id=match_id))
            preview = MatchBettingClosePreview(target=target)
            await self._edit_layout(
                interaction,
                view=MatchBettingClosePreviewView(adapter=self, context=context, preview=preview),
            )
        except (MatchStaffBettingCloseQueryError, TypeError, ValueError) as error:
            await self._edit_deferred(interaction, preview_error(error))
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                preview_internal_error(correlation_id(interaction)),
            )

    async def confirm_close(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        preview: MatchBettingClosePreview,
        source_view: MatchBettingClosePreviewView,
    ) -> None:
        if not context.matches(interaction):
            await self._send_component_error(
                interaction,
                BOUND_CONFIRM_ERROR,
            )
            return
        if not await self._defer_message_update(interaction, response_kind="betting-close-final"):
            return
        if not await self.authorize_interaction(interaction, _COMMAND_NAME):
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
                lambda: self.commands.close_betting(
                    CloseMatchBetting(
                        match_id=preview.target.match_id,
                        idempotency_key=f"match-betting-close:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except MatchBettingCloseIdempotencyConflictError:
            message = IDEMPOTENCY_CONFLICT
        except MatchBettingCloseUnavailableError:
            message = UNAVAILABLE
        except (MatchBettingCloseAuditError, MatchBettingCloseError, TypeError, ValueError) as error:
            message = close_error(error)
        except Exception:
            self._log_application_failure(interaction)
            message = close_internal_error(correlation_id(interaction))
        else:
            message = format_match_betting_close_success(result)
        await self._edit_terminal(interaction, message=message)

    async def cancel_close(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: MatchBettingClosePreviewView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="betting-close-cancel",
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
            await MatchBettingCloseDiscordAdapter._send_reopen_notice(interaction)

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
