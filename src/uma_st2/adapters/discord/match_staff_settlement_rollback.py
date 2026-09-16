"""Discord adapter for terminal native Match settlement rollback."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from uma_st2.application.match import (
    MatchSettlementRollbackAuditError,
    MatchSettlementRollbackBalanceError,
    MatchSettlementRollbackCommands,
    MatchSettlementRollbackError,
    MatchSettlementRollbackEvidenceExpiredError,
    MatchSettlementRollbackIdempotencyConflictError,
    MatchSettlementRollbackPlan,
    MatchSettlementRollbackQueries,
    MatchSettlementRollbackStaleError,
    MatchSettlementRollbackUnavailableError,
    RollbackMatchSettlement,
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
from .strings.match_staff_settlement_rollback import (
    BALANCE_ERROR,
    BOUND_CONFIRM_ERROR,
    BOUND_INTERACTION_ERROR,
    CANCEL_LABEL,
    CANCELLED,
    CONFIRM_LABEL,
    CONFIRMATION_STARTED,
    EVIDENCE_EXPIRED,
    IDEMPOTENCY_CONFLICT,
    NEXT_LABEL,
    PREVIOUS_LABEL,
    REASON_FORMAT_ERROR,
    REASON_REQUIRED,
    STALE,
    UNAVAILABLE,
    format_match_settlement_rollback_plan,
    format_match_settlement_rollback_success,
    match_settlement_rollback_autocomplete_choices,
    preview_error,
    preview_internal_error,
    rollback_error,
    rollback_internal_error,
)
from .strings.match_staff_workflows import SETTLEMENT_TRANSITION_ERROR

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.staff.settlement-rollback"
_COMPONENT_TIMEOUT_SECONDS = 600.0
_RATING_PAGE_SIZE = 8


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} is unavailable.")
    return value


def _required_reason(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(REASON_REQUIRED)
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        raise ValueError(REASON_FORMAT_ERROR)
    return normalized


@dataclass(frozen=True, slots=True)
class MatchSettlementRollbackPreview:
    """Adapter-local reason plus one detached rollback plan."""

    plan: MatchSettlementRollbackPlan
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.plan, MatchSettlementRollbackPlan):
            raise ValueError("plan must be MatchSettlementRollbackPlan.")
        object.__setattr__(self, "reason", _required_reason(self.reason))


def _rating_page_count(preview: MatchSettlementRollbackPreview) -> int:
    return max(1, (len(preview.plan.ratings) + _RATING_PAGE_SIZE - 1) // _RATING_PAGE_SIZE)


def format_match_settlement_rollback_preview(
    preview: MatchSettlementRollbackPreview,
    *,
    page_index: int = 0,
) -> str:
    """Render one bounded private danger-confirmation page."""

    pages = _rating_page_count(preview)
    if not 0 <= page_index < pages:
        raise ValueError("page_index is outside the settlement rollback Preview.")
    return format_match_settlement_rollback_plan(
        plan=preview.plan,
        reason=preview.reason,
        page_index=page_index,
        page_count=pages,
        page_size=_RATING_PAGE_SIZE,
    )


class MatchSettlementRollbackPageButton(discord.ui.Button):
    def __init__(
        self,
        *,
        owner: MatchSettlementRollbackPreviewView,
        direction: int,
        disabled: bool,
    ) -> None:
        self._owner = owner
        self._direction = direction
        super().__init__(
            label=PREVIOUS_LABEL if direction < 0 else NEXT_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"match-settlement-rollback-page-{'previous' if direction < 0 else 'next'}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.change_page(interaction, direction=self._direction)


class MatchSettlementRollbackConfirmButton(discord.ui.Button):
    def __init__(self, *, owner: MatchSettlementRollbackPreviewView) -> None:
        self._owner = owner
        super().__init__(
            label=CONFIRM_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id="match-settlement-rollback-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.confirm(interaction)


class MatchSettlementRollbackCancelButton(discord.ui.Button):
    def __init__(self, *, owner: MatchSettlementRollbackPreviewView) -> None:
        self._owner = owner
        super().__init__(
            label=CANCEL_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-settlement-rollback-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.cancel(interaction)


class MatchSettlementRollbackPreviewView(discord.ui.LayoutView):
    """Bound Preview retaining only detached Application DTOs."""

    def __init__(
        self,
        *,
        adapter: MatchSettlementRollbackDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchSettlementRollbackPreview,
        page_index: int = 0,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        pages = _rating_page_count(preview)
        if not 0 <= page_index < pages:
            raise ValueError("page_index is outside the settlement rollback Preview.")
        self.adapter = adapter
        self.context = context
        self.preview = preview
        self.page_index = page_index
        self._confirmation_started = False
        container = discord.ui.Container(
            discord.ui.TextDisplay(format_match_settlement_rollback_preview(preview, page_index=page_index))
        )
        navigation = discord.ui.ActionRow()
        navigation.add_item(MatchSettlementRollbackPageButton(owner=self, direction=-1, disabled=page_index == 0))
        navigation.add_item(
            MatchSettlementRollbackPageButton(
                owner=self,
                direction=1,
                disabled=page_index + 1 >= pages,
            )
        )
        container.add_item(navigation)
        actions = discord.ui.ActionRow()
        actions.add_item(MatchSettlementRollbackConfirmButton(owner=self))
        actions.add_item(MatchSettlementRollbackCancelButton(owner=self))
        container.add_item(actions)
        self.add_item(container)

    def claim_confirmation(self) -> bool:
        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True

    async def change_page(self, interaction: discord.Interaction, *, direction: int) -> None:
        await self.adapter.change_preview_page(
            interaction,
            context=self.context,
            preview=self.preview,
            page_index=self.page_index + direction,
            source_view=self,
        )

    async def confirm(self, interaction: discord.Interaction) -> None:
        await self.adapter.confirm_rollback(
            interaction,
            context=self.context,
            preview=self.preview,
            source_view=self,
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        await self.adapter.cancel_preview(
            interaction,
            context=self.context,
            source_view=self,
        )


@dataclass(frozen=True, slots=True)
class MatchSettlementRollbackDiscordAdapter:
    """Translate one bound staff rollback flow into query and command calls."""

    queries: MatchSettlementRollbackQueries
    commands: MatchSettlementRollbackCommands
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
            return match_settlement_rollback_autocomplete_choices(targets)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return []

    async def preview_rollback(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        reason: str,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="rollback-preview",
        ):
            return
        source_view.stop()
        try:
            normalized_reason = _required_reason(reason)
            plan = await self.blocking_runner(lambda: self.queries.get_preview(match_id=match_id))
            preview = MatchSettlementRollbackPreview(plan=plan, reason=normalized_reason)
            await self._edit_layout(
                interaction,
                view=MatchSettlementRollbackPreviewView(
                    adapter=self,
                    context=context,
                    preview=preview,
                ),
            )
        except (MatchSettlementRollbackError, TypeError, ValueError) as error:
            await self._edit_deferred(interaction, preview_error(error))
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                preview_internal_error(correlation_id(interaction)),
            )

    async def confirm_rollback(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        preview: MatchSettlementRollbackPreview,
        source_view: MatchSettlementRollbackPreviewView,
    ) -> None:
        if not context.matches(interaction):
            await self._send_component_error(
                interaction,
                BOUND_CONFIRM_ERROR,
            )
            return
        if not await self._defer_message_update(interaction, response_kind="rollback-final"):
            return
        if not await self.authorize_interaction(interaction, _COMMAND_NAME):
            return
        if not source_view.claim_confirmation():
            source_view.stop()
            await self._edit_deferred(interaction, CONFIRMATION_STARTED)
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(
                getattr(interaction, "id", None),
                field_name="interaction ID",
            )
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            result = await self.blocking_runner(
                lambda: self.commands.rollback_settlement(
                    RollbackMatchSettlement(
                        match_id=preview.plan.target.match_id,
                        expected_rollback_fingerprint=preview.plan.rollback_fingerprint,
                        idempotency_key=f"match-settlement-rollback:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        reason=preview.reason,
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except MatchSettlementRollbackIdempotencyConflictError:
            message = IDEMPOTENCY_CONFLICT
        except MatchSettlementRollbackStaleError:
            message = STALE
        except MatchSettlementRollbackEvidenceExpiredError:
            message = EVIDENCE_EXPIRED
        except MatchSettlementRollbackBalanceError:
            message = BALANCE_ERROR
        except MatchSettlementRollbackUnavailableError:
            message = UNAVAILABLE
        except (MatchSettlementRollbackAuditError, MatchSettlementRollbackError, TypeError, ValueError) as error:
            message = rollback_error(error)
        except Exception:
            self._log_application_failure(interaction)
            message = rollback_internal_error(correlation_id(interaction))
        else:
            message = format_match_settlement_rollback_success(result)
        await self._edit_terminal(interaction, message=message)

    async def cancel_preview(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: MatchSettlementRollbackPreviewView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="rollback-cancel",
        ):
            return
        source_view.stop()
        await self._edit_terminal(interaction, message=CANCELLED)

    async def change_preview_page(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        preview: MatchSettlementRollbackPreview,
        page_index: int,
        source_view: MatchSettlementRollbackPreviewView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="rollback-page",
        ):
            return
        source_view.stop()
        await self._edit_layout(
            interaction,
            view=MatchSettlementRollbackPreviewView(
                adapter=self,
                context=context,
                preview=preview,
                page_index=page_index,
            ),
        )

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
            MatchSettlementRollbackDiscordAdapter._log_delivery_failure(interaction, "layout", error=error)
            await MatchSettlementRollbackDiscordAdapter._send_reopen_notice(interaction)

    @classmethod
    async def _edit_terminal(cls, interaction: discord.Interaction, *, message: str) -> None:
        await cls._edit_layout(interaction, view=cls._terminal_layout(message))

    @classmethod
    async def _edit_deferred(cls, interaction: discord.Interaction, message: str) -> None:
        await cls._edit_terminal(interaction, message=message)

    @staticmethod
    def _terminal_layout(message: str) -> discord.ui.LayoutView:
        return buttonless_terminal_layout(message, timeout_seconds=_COMPONENT_TIMEOUT_SECONDS)

    @staticmethod
    async def _send_reopen_notice(interaction: discord.Interaction) -> None:
        try:
            await interaction.followup.send(
                SETTLEMENT_TRANSITION_ERROR,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            MatchSettlementRollbackDiscordAdapter._log_delivery_failure(interaction, "reopen-notice")

    @staticmethod
    async def _send_component_error(interaction: discord.Interaction, message: str) -> None:
        try:
            await interaction.response.send_message(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            MatchSettlementRollbackDiscordAdapter._log_delivery_failure(interaction, "component-error")

    @staticmethod
    def _log_application_failure(interaction: object) -> None:
        logger.exception(
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
