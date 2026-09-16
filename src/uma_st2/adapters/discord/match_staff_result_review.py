"""Discord adapter for private Match result review and pending rejection."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from uma_st2.application.match import (
    MatchResultRejectionCommands,
    MatchResultReviewAuditError,
    MatchResultReviewError,
    MatchResultReviewIdempotencyConflictError,
    MatchResultReviewInvalidSourceError,
    MatchResultReviewQueries,
    MatchResultReviewStaleError,
    MatchResultReviewUnavailableError,
    MatchResultSubmissionTarget,
    RejectMatchResultSubmission,
)

from .common import (
    AuthorizeDiscordAutocomplete,
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    bounded_discord_message,
    buttonless_terminal_layout,
    correlation_id,
    run_blocking_application,
)
from .match_staff import MatchStaffInteractionContext
from .strings.match_staff_result_review import (
    BOUND_INTERACTION_ERROR,
    BOUND_REJECT_ERROR,
    CLOSE_LABEL,
    CLOSED,
    CORRECTION_GUIDE,
    CORRECTION_GUIDE_LABEL,
    IDEMPOTENCY_CONFLICT,
    NEXT_LABEL,
    PENDING_UNAVAILABLE,
    PREVIOUS_LABEL,
    REASON_REQUIRED,
    REJECT_LABEL,
    REJECT_MODAL_TITLE,
    REJECT_REASON_LABEL,
    REJECT_REASON_PLACEHOLDER,
    REJECTION_STARTED,
    STALE,
    UNAVAILABLE,
    evidence_error,
    format_match_result_rejection_success,
    match_result_review_autocomplete_choices,
    open_error,
    open_internal_error,
    rejection_error,
    rejection_internal_error,
)
from .strings.match_staff_result_review import (
    format_match_result_review as _format_match_result_review,
)
from .strings.match_staff_workflows import RESULT_TRANSITION_ERROR

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.staff.result-review"
_COMPONENT_TIMEOUT_SECONDS = 600.0
_ENTRY_PAGE_SIZE = 8


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} is unavailable.")
    return value


def _page_count(target: MatchResultSubmissionTarget) -> int:
    return max(1, (len(target.entries) + _ENTRY_PAGE_SIZE - 1) // _ENTRY_PAGE_SIZE)


def format_match_result_review(
    target: MatchResultSubmissionTarget,
    *,
    page_index: int,
) -> str:
    """Render one private bounded current-pending candidate page."""

    return _format_match_result_review(target, page_index=page_index, page_size=_ENTRY_PAGE_SIZE)


class MatchResultReviewPageButton(discord.ui.Button):
    def __init__(
        self,
        *,
        owner: MatchResultReviewView,
        direction: int,
        disabled: bool,
    ) -> None:
        self._owner = owner
        self._direction = direction
        super().__init__(
            label=PREVIOUS_LABEL if direction < 0 else NEXT_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"match-result-review-page-{'previous' if direction < 0 else 'next'}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.change_page(interaction, direction=self._direction)


class MatchResultCorrectionGuideButton(discord.ui.Button):
    def __init__(self, *, owner: MatchResultReviewView) -> None:
        self._owner = owner
        super().__init__(
            label=CORRECTION_GUIDE_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-result-review-correction-guide",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.show_correction_guide(interaction)


class MatchResultRejectButton(discord.ui.Button):
    def __init__(self, *, owner: MatchResultReviewView) -> None:
        self._owner = owner
        super().__init__(
            label=REJECT_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id="match-result-review-reject",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.open_rejection(interaction)


class MatchResultReviewCloseButton(discord.ui.Button):
    def __init__(self, *, owner: MatchResultReviewView) -> None:
        self._owner = owner
        super().__init__(
            label=CLOSE_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-result-review-close",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.close(interaction)


class MatchResultRejectModal(discord.ui.Modal):
    def __init__(self, *, owner: MatchResultReviewView) -> None:
        super().__init__(
            title=REJECT_MODAL_TITLE,
            timeout=_COMPONENT_TIMEOUT_SECONDS,
        )
        self._owner = owner
        self.reason = discord.ui.TextInput(
            label=REJECT_REASON_LABEL,
            placeholder=REJECT_REASON_PLACEHOLDER,
            required=True,
            max_length=255,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._owner.reject(
            interaction,
            reason=str(self.reason.value),
        )


class MatchResultReviewView(discord.ui.LayoutView):
    """Private current-pending review with no View-lived persistence resource."""

    def __init__(
        self,
        *,
        adapter: MatchResultReviewDiscordAdapter,
        context: MatchStaffInteractionContext,
        target: MatchResultSubmissionTarget,
        page_index: int = 0,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        if target.pending is None:
            raise ValueError("Review target must have one current pending submission.")
        pages = _page_count(target)
        if not 0 <= page_index < pages:
            raise ValueError("page_index is outside the current result review.")
        self.adapter = adapter
        self.context = context
        self.target = target
        self.page_index = page_index
        self._rejection_started = False
        container = discord.ui.Container(
            discord.ui.TextDisplay(format_match_result_review(target, page_index=page_index))
        )
        navigation = discord.ui.ActionRow()
        navigation.add_item(
            MatchResultReviewPageButton(
                owner=self,
                direction=-1,
                disabled=page_index == 0,
            )
        )
        navigation.add_item(
            MatchResultReviewPageButton(
                owner=self,
                direction=1,
                disabled=page_index + 1 >= pages,
            )
        )
        container.add_item(navigation)
        actions = discord.ui.ActionRow()
        actions.add_item(MatchResultCorrectionGuideButton(owner=self))
        actions.add_item(MatchResultRejectButton(owner=self))
        actions.add_item(MatchResultReviewCloseButton(owner=self))
        container.add_item(actions)
        self.add_item(container)

    def claim_rejection(self) -> bool:
        if self._rejection_started:
            return False
        self._rejection_started = True
        return True

    async def change_page(self, interaction: discord.Interaction, *, direction: int) -> None:
        if not await self.adapter.authorize_bound(interaction, context=self.context):
            return
        self.stop()
        await self.adapter.edit_component(
            interaction,
            view=MatchResultReviewView(
                adapter=self.adapter,
                context=self.context,
                target=self.target,
                page_index=self.page_index + direction,
            ),
        )

    async def show_correction_guide(self, interaction: discord.Interaction) -> None:
        if not await self.adapter.authorize_bound(interaction, context=self.context):
            return
        await self.adapter.send_component_message(
            interaction,
            CORRECTION_GUIDE,
        )

    async def open_rejection(self, interaction: discord.Interaction) -> None:
        if not await self.adapter.authorize_bound(interaction, context=self.context):
            return
        try:
            await interaction.response.send_modal(MatchResultRejectModal(owner=self))
        except Exception:
            self.adapter.log_delivery_failure(interaction, "reject-modal")

    async def reject(self, interaction: discord.Interaction, *, reason: str) -> None:
        await self.adapter.reject_pending(
            interaction,
            context=self.context,
            source_view=self,
            reason=reason,
        )

    async def close(self, interaction: discord.Interaction) -> None:
        await self.adapter.close_review(
            interaction,
            context=self.context,
            source_view=self,
        )


@dataclass(frozen=True, slots=True)
class MatchResultReviewDiscordAdapter:
    """Translate one private current-pending review and rejection flow."""

    queries: MatchResultReviewQueries
    commands: MatchResultRejectionCommands
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
            return match_result_review_autocomplete_choices(targets)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return []

    async def start_review(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not await self._prepare_bound_update(interaction, context=context, response_kind="review-open"):
            return
        source_view.stop()
        try:
            target = await self.blocking_runner(lambda: self.queries.get_target(match_id=match_id))
            view = MatchResultReviewView(
                adapter=self,
                context=context,
                target=target,
            )
        except (MatchResultReviewError, TypeError, ValueError) as error:
            await self._edit_deferred(interaction, open_error(error))
            return
        except Exception:
            self.log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                open_internal_error(correlation_id(interaction)),
            )
            return
        await self._edit_layout(interaction, view=view)

    async def reject_pending(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: MatchResultReviewView,
        reason: str,
    ) -> None:
        if not context.matches(interaction):
            await self.send_component_message(
                interaction,
                BOUND_REJECT_ERROR,
            )
            return
        if not await self.authorize_interaction(interaction, _COMMAND_NAME):
            return
        if not await self._defer_message_update(interaction, response_kind="rejection-final"):
            return
        if not source_view.claim_rejection():
            source_view.stop()
            await self._edit_deferred(interaction, REJECTION_STARTED)
            return
        source_view.stop()
        try:
            normalized_reason = reason.strip()
            if not normalized_reason:
                raise ValueError(REASON_REQUIRED)
            pending = source_view.target.pending
            if pending is None:
                raise ValueError(PENDING_UNAVAILABLE)
            interaction_id = _required_snowflake(
                getattr(interaction, "id", None),
                field_name="interaction ID",
            )
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            result = await self.blocking_runner(
                lambda: self.commands.reject(
                    RejectMatchResultSubmission(
                        match_id=source_view.target.match_id,
                        submission_id=pending.submission_id,
                        expected_state_fingerprint=source_view.target.state_fingerprint,
                        expected_candidate_fingerprint=pending.candidate.fingerprint,
                        reason=normalized_reason,
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        idempotency_key=f"match-result-reject:{interaction_id}",
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except MatchResultReviewIdempotencyConflictError:
            message = IDEMPOTENCY_CONFLICT
        except MatchResultReviewStaleError:
            message = STALE
        except MatchResultReviewUnavailableError:
            message = UNAVAILABLE
        except (MatchResultReviewAuditError, MatchResultReviewInvalidSourceError):
            self.log_application_failure(interaction)
            message = evidence_error(correlation_id(interaction))
        except (MatchResultReviewError, TypeError, ValueError) as error:
            message = rejection_error(error)
        except Exception:
            self.log_application_failure(interaction)
            message = rejection_internal_error(correlation_id(interaction))
        else:
            message = format_match_result_rejection_success(result)
        await self._edit_terminal(interaction, message=message)

    async def close_review(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: MatchResultReviewView,
    ) -> None:
        if not await self.authorize_bound(interaction, context=context):
            return
        source_view.stop()
        try:
            await interaction.response.edit_message(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=self._terminal_layout(CLOSED),
            )
        except Exception:
            self.log_delivery_failure(interaction, "close")

    async def authorize_bound(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
    ) -> bool:
        if not context.matches(interaction):
            await self.send_component_message(
                interaction,
                BOUND_INTERACTION_ERROR,
            )
            return False
        return await self.authorize_interaction(interaction, _COMMAND_NAME)

    async def _prepare_bound_update(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        response_kind: str,
    ) -> bool:
        if not context.matches(interaction):
            await self.send_component_message(interaction, BOUND_INTERACTION_ERROR)
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
            self.log_delivery_failure(interaction, f"{response_kind}-defer", error=error)
            return False
        return True

    @staticmethod
    async def edit_component(
        interaction: discord.Interaction,
        *,
        view: discord.ui.LayoutView,
    ) -> None:
        try:
            await interaction.response.edit_message(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=component",
                correlation_id(interaction),
                _COMMAND_NAME,
            )

    @staticmethod
    async def send_component_message(interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.response.send_message(
                bounded_discord_message((content,), limit=1900),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=component-message",
                correlation_id(interaction),
                _COMMAND_NAME,
            )

    @staticmethod
    async def _edit_layout(
        interaction: discord.Interaction,
        *,
        view: discord.ui.LayoutView,
    ) -> None:
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
            await MatchResultReviewDiscordAdapter._send_reopen_notice(interaction)

    @classmethod
    async def _edit_terminal(cls, interaction: discord.Interaction, *, message: str) -> None:
        await cls._edit_layout(interaction, view=cls._terminal_layout(message))

    @classmethod
    async def _edit_deferred(cls, interaction: discord.Interaction, content: str) -> None:
        await cls._edit_terminal(interaction, message=content)

    @staticmethod
    def _terminal_layout(message: str) -> discord.ui.LayoutView:
        return buttonless_terminal_layout(message, timeout_seconds=_COMPONENT_TIMEOUT_SECONDS)

    @staticmethod
    async def _send_reopen_notice(interaction: discord.Interaction) -> None:
        try:
            await interaction.followup.send(
                RESULT_TRANSITION_ERROR,
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
    def log_application_failure(interaction: object) -> None:
        logger.error(
            "Discord application call failed correlation_id=%s command=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
        )

    @staticmethod
    def log_delivery_failure(
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
