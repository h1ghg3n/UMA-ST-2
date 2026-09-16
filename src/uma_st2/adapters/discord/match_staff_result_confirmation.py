"""Discord adapter for explicit authoritative Match result confirmation."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from uma_st2.application.match import (
    ConfirmMatchResultSubmission,
    MatchResultConfirmationAuditError,
    MatchResultConfirmationCommands,
    MatchResultConfirmationError,
    MatchResultConfirmationIdempotencyConflictError,
    MatchResultConfirmationInvalidSourceError,
    MatchResultConfirmationStaleError,
    MatchResultConfirmationUnavailableError,
    MatchResultReviewError,
    MatchResultReviewQueries,
    MatchResultReviewTargetChoice,
    MatchResultSubmissionTarget,
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
from .strings.match_staff_result_confirmation import (
    BOUND_CONFIRM_ERROR,
    BOUND_INTERACTION_ERROR,
    CANCEL_LABEL,
    CANCELLED,
    CONFIRM_LABEL,
    CONFIRMATION_STARTED,
    IDEMPOTENCY_CONFLICT,
    NEXT_LABEL,
    NO_PAGE,
    PENDING_UNAVAILABLE,
    PREVIOUS_LABEL,
    REVIEW_REQUIRED,
    STALE,
    UNAVAILABLE,
    confirmation_error,
    confirmation_internal_error,
    evidence_error,
    format_match_result_confirmation_success,
    open_error,
    open_internal_error,
)
from .strings.match_staff_result_confirmation import (
    format_match_result_confirmation as _format_match_result_confirmation,
)
from .strings.match_staff_result_review import match_result_review_autocomplete_choices
from .strings.match_staff_workflows import RESULT_TRANSITION_ERROR

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.staff.result-confirm"
_COMPONENT_TIMEOUT_SECONDS = 600.0
_ENTRY_PAGE_SIZE = 8


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} is unavailable.")
    return value


def _page_count(target: MatchResultSubmissionTarget) -> int:
    return max(1, (len(target.entries) + _ENTRY_PAGE_SIZE - 1) // _ENTRY_PAGE_SIZE)


def format_match_result_confirmation(
    target: MatchResultSubmissionTarget,
    *,
    page_index: int,
    viewed_pages: frozenset[int],
) -> str:
    """Render one bounded final-confirmation candidate page."""

    return _format_match_result_confirmation(
        target,
        page_index=page_index,
        viewed_pages=viewed_pages,
        page_size=_ENTRY_PAGE_SIZE,
    )


class MatchResultConfirmationPageButton(discord.ui.Button):
    def __init__(
        self,
        *,
        owner: MatchResultConfirmationView,
        direction: int,
        disabled: bool,
    ) -> None:
        self._owner = owner
        self._direction = direction
        super().__init__(
            label=PREVIOUS_LABEL if direction < 0 else NEXT_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"match-result-confirm-page-{'previous' if direction < 0 else 'next'}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.change_page(interaction, direction=self._direction)


class MatchResultConfirmationConfirmButton(discord.ui.Button):
    def __init__(self, *, owner: MatchResultConfirmationView, disabled: bool) -> None:
        self._owner = owner
        super().__init__(
            label=CONFIRM_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id="match-result-confirm-final",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.confirm(interaction)


class MatchResultConfirmationCancelButton(discord.ui.Button):
    def __init__(self, *, owner: MatchResultConfirmationView) -> None:
        self._owner = owner
        super().__init__(
            label=CANCEL_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-result-confirm-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.cancel(interaction)


class MatchResultConfirmationView(discord.ui.LayoutView):
    """Private confirmation View that retains no persistence resource."""

    def __init__(
        self,
        *,
        adapter: MatchResultConfirmationDiscordAdapter,
        context: MatchStaffInteractionContext,
        target: MatchResultSubmissionTarget,
        page_index: int = 0,
        viewed_pages: frozenset[int] | None = None,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        if target.pending is None:
            raise ValueError("Confirmation target must have one current pending submission.")
        pages = _page_count(target)
        if not 0 <= page_index < pages:
            raise ValueError("page_index is outside the current result confirmation.")
        seen = frozenset({page_index}) if viewed_pages is None else viewed_pages | {page_index}
        if any(not 0 <= value < pages for value in seen):
            raise ValueError("viewed_pages contains an invalid result page.")
        self.adapter = adapter
        self.context = context
        self.target = target
        self.page_index = page_index
        self.viewed_pages = seen
        self._confirmation_started = False
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                format_match_result_confirmation(
                    target,
                    page_index=page_index,
                    viewed_pages=seen,
                )
            )
        )
        navigation = discord.ui.ActionRow()
        navigation.add_item(
            MatchResultConfirmationPageButton(
                owner=self,
                direction=-1,
                disabled=page_index == 0,
            )
        )
        navigation.add_item(
            MatchResultConfirmationPageButton(
                owner=self,
                direction=1,
                disabled=page_index + 1 >= pages,
            )
        )
        container.add_item(navigation)
        actions = discord.ui.ActionRow()
        actions.add_item(
            MatchResultConfirmationConfirmButton(
                owner=self,
                disabled=len(seen) != pages,
            )
        )
        actions.add_item(MatchResultConfirmationCancelButton(owner=self))
        container.add_item(actions)
        self.add_item(container)

    @property
    def all_pages_viewed(self) -> bool:
        return len(self.viewed_pages) == _page_count(self.target)

    def claim_confirmation(self) -> bool:
        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True

    async def change_page(self, interaction: discord.Interaction, *, direction: int) -> None:
        if not await self.adapter.authorize_bound(interaction, context=self.context):
            return
        next_page = self.page_index + direction
        if not 0 <= next_page < _page_count(self.target):
            await self.adapter.send_component_message(interaction, NO_PAGE)
            return
        self.stop()
        await self.adapter.edit_component(
            interaction,
            view=MatchResultConfirmationView(
                adapter=self.adapter,
                context=self.context,
                target=self.target,
                page_index=next_page,
                viewed_pages=self.viewed_pages,
            ),
        )

    async def confirm(self, interaction: discord.Interaction) -> None:
        await self.adapter.confirm_pending(
            interaction,
            context=self.context,
            source_view=self,
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        await self.adapter.cancel_confirmation(
            interaction,
            context=self.context,
            source_view=self,
        )


@dataclass(frozen=True, slots=True)
class MatchResultConfirmationDiscordAdapter:
    """Translate one private result-confirm flow into query and command calls."""

    queries: MatchResultReviewQueries
    commands: MatchResultConfirmationCommands
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
            targets: tuple[MatchResultReviewTargetChoice, ...] = await self.blocking_runner(
                lambda: self.queries.search_targets(search=current, limit=25)
            )
            return match_result_review_autocomplete_choices(targets)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return []

    async def start_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not await self._prepare_bound_update(interaction, context=context, response_kind="confirmation-open"):
            return
        source_view.stop()
        try:
            target = await self.blocking_runner(lambda: self.queries.get_target(match_id=match_id))
            view = MatchResultConfirmationView(
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

    async def confirm_pending(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: MatchResultConfirmationView,
    ) -> None:
        if not context.matches(interaction):
            await self.send_component_message(
                interaction,
                BOUND_CONFIRM_ERROR,
            )
            return
        if not await self.authorize_interaction(interaction, _COMMAND_NAME):
            return
        if not source_view.all_pages_viewed:
            await self.send_component_message(interaction, REVIEW_REQUIRED)
            return
        if not await self._defer_message_update(interaction, response_kind="confirmation-final"):
            return
        if not source_view.claim_confirmation():
            source_view.stop()
            await self._edit_deferred(interaction, CONFIRMATION_STARTED)
            return
        source_view.stop()
        try:
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
                lambda: self.commands.confirm(
                    ConfirmMatchResultSubmission(
                        match_id=source_view.target.match_id,
                        submission_id=pending.submission_id,
                        expected_state_fingerprint=source_view.target.state_fingerprint,
                        expected_candidate_fingerprint=pending.candidate.fingerprint,
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        idempotency_key=f"match-result-confirm:{interaction_id}",
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except MatchResultConfirmationIdempotencyConflictError:
            message = IDEMPOTENCY_CONFLICT
        except MatchResultConfirmationStaleError:
            message = STALE
        except MatchResultConfirmationUnavailableError:
            message = UNAVAILABLE
        except (
            MatchResultConfirmationAuditError,
            MatchResultConfirmationInvalidSourceError,
        ):
            self.log_application_failure(interaction)
            message = evidence_error(correlation_id(interaction))
        except (MatchResultConfirmationError, TypeError, ValueError) as error:
            message = confirmation_error(error)
        except Exception:
            self.log_application_failure(interaction)
            message = confirmation_internal_error(correlation_id(interaction))
        else:
            message = format_match_result_confirmation_success(result)
        await self._edit_terminal(interaction, message=message)

    async def cancel_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: MatchResultConfirmationView,
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
                view=self._terminal_layout(CANCELLED),
            )
        except Exception:
            self.log_delivery_failure(interaction, "cancel")

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
            await MatchResultConfirmationDiscordAdapter._send_reopen_notice(interaction)

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
