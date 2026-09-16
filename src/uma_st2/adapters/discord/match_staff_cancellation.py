"""Discord adapter for native whole-Match cancellation."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from uma_st2.application.match import (
    CancelMatch,
    MatchCancellationAuditError,
    MatchCancellationCommands,
    MatchCancellationError,
    MatchCancellationIdempotencyConflictError,
    MatchCancellationPreviewTarget,
    MatchCancellationUnavailableError,
    MatchCancellationWalletUnavailableError,
    MatchStaffCancellationQueries,
    MatchStaffCancellationQueryError,
)

from .common import (
    AuthorizeDiscordAutocomplete,
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    bounded_discord_message,
    correlation_id,
    run_blocking_application,
)
from .match_staff import MatchStaffInteractionContext
from .strings.match_staff_cancellation import (
    BOUND_CONFIRM_ERROR,
    BOUND_INTERACTION_ERROR,
    CANCEL_LABEL,
    CANCELLED,
    CONFIRM_LABEL,
    CONFIRMATION_STARTED,
    IDEMPOTENCY_CONFLICT,
    REASON_TOO_LONG,
    REASON_TYPE_ERROR,
    UNAVAILABLE,
    WALLET_UNAVAILABLE,
    cancellation_error,
    cancellation_internal_error,
    format_match_cancellation_success,
    format_match_cancellation_target,
    match_cancellation_autocomplete_choices,
    preview_error,
    preview_internal_error,
)
from .strings.match_staff_workflows import RACE_TRANSITION_ERROR

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.staff.race-cancel"
_COMPONENT_TIMEOUT_SECONDS = 600.0


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} is unavailable.")
    return value


def _optional_reason(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(REASON_TYPE_ERROR)
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 255:
        raise ValueError(REASON_TOO_LONG)
    return normalized


@dataclass(frozen=True, slots=True)
class MatchCancellationPreview:
    """Adapter-local optional refund reason plus one closed query DTO."""

    target: MatchCancellationPreviewTarget
    reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.target, MatchCancellationPreviewTarget):
            raise ValueError("target must be MatchCancellationPreviewTarget.")
        object.__setattr__(self, "reason", _optional_reason(self.reason))


def format_match_cancellation_preview(preview: MatchCancellationPreview) -> str:
    """Render one bounded private terminal cancellation confirmation."""

    return format_match_cancellation_target(target=preview.target, reason=preview.reason)


class MatchCancellationConfirmButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchCancellationDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchCancellationPreview,
        source_view: MatchCancellationPreviewView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._source_view = source_view
        super().__init__(
            label=CONFIRM_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id="match-cancellation-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.confirm_cancellation(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self._source_view,
        )


class MatchCancellationCancelButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchCancellationDiscordAdapter,
        context: MatchStaffInteractionContext,
        source_view: MatchCancellationPreviewView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._source_view = source_view
        super().__init__(
            label=CANCEL_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-cancellation-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.cancel_preview(
            interaction,
            context=self._context,
            source_view=self._source_view,
        )


class MatchCancellationPreviewView(discord.ui.LayoutView):
    """Bound cancellation Preview that retains no Session or transaction."""

    def __init__(
        self,
        *,
        adapter: MatchCancellationDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchCancellationPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.preview = preview
        self._confirmation_started = False
        container = discord.ui.Container(discord.ui.TextDisplay(format_match_cancellation_preview(preview)))
        actions = discord.ui.ActionRow()
        actions.add_item(
            MatchCancellationConfirmButton(
                adapter=adapter,
                context=context,
                preview=preview,
                source_view=self,
            )
        )
        actions.add_item(MatchCancellationCancelButton(adapter=adapter, context=context, source_view=self))
        container.add_item(actions)
        self.add_item(container)

    def claim_confirmation(self) -> bool:
        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True


@dataclass(frozen=True, slots=True)
class MatchCancellationDiscordAdapter:
    """Translate one bound staff cancellation flow into query and command calls."""

    queries: MatchStaffCancellationQueries
    commands: MatchCancellationCommands
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
            return match_cancellation_autocomplete_choices(targets)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return []

    async def preview_cancellation(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        reason: str | None,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="match-cancellation-preview",
        ):
            return
        source_view.stop()
        try:
            normalized_reason = _optional_reason(reason)
            target = await self.blocking_runner(lambda: self.queries.get_target(match_id=match_id))
            preview = MatchCancellationPreview(target=target, reason=normalized_reason)
            await self._edit_layout(
                interaction,
                view=MatchCancellationPreviewView(adapter=self, context=context, preview=preview),
            )
        except (MatchStaffCancellationQueryError, TypeError, ValueError) as error:
            await self._edit_deferred(interaction, preview_error(error))
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                preview_internal_error(correlation_id(interaction)),
            )

    async def confirm_cancellation(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        preview: MatchCancellationPreview,
        source_view: MatchCancellationPreviewView,
    ) -> None:
        if not context.matches(interaction):
            await self._send_component_error(
                interaction,
                BOUND_CONFIRM_ERROR,
            )
            return
        if not await self._defer_message_update(interaction, response_kind="match-cancellation-final"):
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
                lambda: self.commands.cancel_match(
                    CancelMatch(
                        match_id=preview.target.match_id,
                        reason=preview.reason,
                        idempotency_key=f"match-cancel:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except MatchCancellationIdempotencyConflictError:
            message = IDEMPOTENCY_CONFLICT
        except MatchCancellationWalletUnavailableError:
            message = WALLET_UNAVAILABLE
        except MatchCancellationUnavailableError:
            message = UNAVAILABLE
        except (MatchCancellationAuditError, MatchCancellationError, TypeError, ValueError) as error:
            message = cancellation_error(error)
        except Exception:
            self._log_application_failure(interaction)
            message = cancellation_internal_error(correlation_id(interaction))
        else:
            message = format_match_cancellation_success(result)
        await self._edit_terminal(interaction, message=message)

    async def cancel_preview(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: MatchCancellationPreviewView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="match-cancellation-cancel",
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
            await MatchCancellationDiscordAdapter._send_reopen_notice(interaction)

    @classmethod
    async def _edit_terminal(cls, interaction: discord.Interaction, *, message: str) -> None:
        await cls._edit_layout(interaction, view=cls._terminal_layout(message))

    @classmethod
    async def _edit_deferred(cls, interaction: discord.Interaction, message: str) -> None:
        await cls._edit_terminal(interaction, message=message)

    @staticmethod
    def _terminal_layout(message: str) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(discord.ui.TextDisplay(bounded_discord_message((message,), limit=1900))))
        return view

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
    async def _send_component_error(interaction: discord.Interaction, message: str) -> None:
        try:
            await interaction.response.send_message(
                message,
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
    def _log_application_failure(interaction: discord.Interaction) -> None:
        logger.exception(
            "Discord application call failed correlation_id=%s command=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
        )

    @staticmethod
    def _log_delivery_failure(
        interaction: discord.Interaction,
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
