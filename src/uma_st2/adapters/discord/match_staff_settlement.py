"""Discord adapter for atomic native V2 Match settlement."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from uma_st2.application.match import (
    MatchSettlementAuditError,
    MatchSettlementCommands,
    MatchSettlementError,
    MatchSettlementIdempotencyConflictError,
    MatchSettlementPlan,
    MatchSettlementQueries,
    MatchSettlementRatingSelectionTarget,
    MatchSettlementRuleUnavailableError,
    MatchSettlementStaleError,
    MatchSettlementUnavailableError,
    MatchSettlementWalletUnavailableError,
    SettleMatch,
)
from uma_st2.domain.match import MatchGrade

from .common import (
    AuthorizeDiscordAutocomplete,
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    buttonless_terminal_layout,
    correlation_id,
    run_blocking_application,
)
from .match_staff import MatchStaffInteractionContext
from .strings.match_staff_settlement import (
    BOUND_CONFIRM_ERROR,
    BOUND_INTERACTION_ERROR,
    CANCEL_LABEL,
    CANCELLED,
    CONFIRM_LABEL,
    CONFIRMATION_STARTED,
    IDEMPOTENCY_CONFLICT,
    NEXT_LABEL,
    PREVIOUS_LABEL,
    RATING_PREVIEW_LABEL,
    RATING_SELECTION_BACK_LABEL,
    RATING_SELECTION_LABEL,
    REASON_TOO_LONG,
    REASON_TYPE_ERROR,
    RULE_UNAVAILABLE,
    STALE,
    UNAVAILABLE,
    WALLET_UNAVAILABLE,
    format_match_settlement_plan,
    format_match_settlement_rating_selection,
    format_match_settlement_success,
    match_settlement_autocomplete_choices,
    match_settlement_rating_selection_options,
    preview_error,
    preview_internal_error,
    settlement_error,
    settlement_internal_error,
)
from .strings.match_staff_workflows import SETTLEMENT_TRANSITION_ERROR

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.staff.settlement"
_COMPONENT_TIMEOUT_SECONDS = 600.0
_RATING_PAGE_SIZE = 8


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
class MatchSettlementPreview:
    """Adapter-local wrapper around one complete read-only settlement plan."""

    plan: MatchSettlementPlan
    selection_target: MatchSettlementRatingSelectionTarget
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.plan, MatchSettlementPlan):
            raise ValueError("plan must be MatchSettlementPlan.")
        if (
            not isinstance(self.selection_target, MatchSettlementRatingSelectionTarget)
            or self.selection_target.match_id != self.plan.target.match_id
            or tuple(entry.match_entry_id for entry in self.selection_target.entries)
            != tuple(entry.match_entry_id for entry in self.plan.target.entries)
        ):
            raise ValueError("selection_target must describe the Preview official board.")
        object.__setattr__(self, "reason", _optional_reason(self.reason))


@dataclass(frozen=True, slots=True)
class MatchSettlementRatingSelectionDraft:
    """Adapter-local explicit Rating exclusion selection."""

    target: MatchSettlementRatingSelectionTarget
    excluded_rating_entry_ids: tuple[int, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, MatchSettlementRatingSelectionTarget):
            raise ValueError("target must be MatchSettlementRatingSelectionTarget.")
        excluded = tuple(sorted(self.excluded_rating_entry_ids))
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in excluded):
            raise ValueError("excluded_rating_entry_ids must contain positive integers.")
        if len(set(excluded)) != len(excluded) or not set(excluded).issubset(
            {entry.match_entry_id for entry in self.target.entries}
        ):
            raise ValueError("excluded_rating_entry_ids must identify unique current Entries.")
        object.__setattr__(self, "excluded_rating_entry_ids", excluded)
        object.__setattr__(self, "reason", _optional_reason(self.reason))


def _rating_page_count(preview: MatchSettlementPreview) -> int:
    return max(1, (len(preview.plan.ratings) + _RATING_PAGE_SIZE - 1) // _RATING_PAGE_SIZE)


def format_match_settlement_preview(
    preview: MatchSettlementPreview,
    *,
    page_index: int = 0,
) -> str:
    """Render one bounded private settlement Preview page."""

    pages = _rating_page_count(preview)
    if not 0 <= page_index < pages:
        raise ValueError("page_index is outside the settlement Preview.")
    return format_match_settlement_plan(
        plan=preview.plan,
        reason=preview.reason,
        page_index=page_index,
        page_count=pages,
        page_size=_RATING_PAGE_SIZE,
    )


class MatchSettlementRatingSelect(discord.ui.Select):
    def __init__(self, *, owner: MatchSettlementRatingSelectionView) -> None:
        self._owner = owner
        options = match_settlement_rating_selection_options(
            owner.draft.target,
            excluded_rating_entry_ids=owner.draft.excluded_rating_entry_ids,
        )
        super().__init__(
            placeholder=RATING_SELECTION_LABEL,
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id="match-settlement-rating-selection",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.update_selection(interaction, values=tuple(self.values))


class MatchSettlementRatingPreviewButton(discord.ui.Button):
    def __init__(self, *, owner: MatchSettlementRatingSelectionView) -> None:
        self._owner = owner
        super().__init__(
            label=RATING_PREVIEW_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id="match-settlement-rating-preview",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.preview(interaction)


class MatchSettlementRatingSelectionCancelButton(discord.ui.Button):
    def __init__(self, *, owner: MatchSettlementRatingSelectionView) -> None:
        self._owner = owner
        super().__init__(
            label=CANCEL_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-settlement-rating-selection-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.cancel(interaction)


class MatchSettlementRatingSelectionView(discord.ui.LayoutView):
    """Complete zero-write Rating selection bound to one opener/context."""

    def __init__(
        self,
        *,
        adapter: MatchSettlementDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSettlementRatingSelectionDraft,
        error: str | None = None,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.adapter = adapter
        self.context = context
        self.draft = draft
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                format_match_settlement_rating_selection(
                    target=draft.target,
                    excluded_rating_entry_ids=draft.excluded_rating_entry_ids,
                    reason=draft.reason,
                    error=error,
                )
            )
        )
        selection = discord.ui.ActionRow()
        selection.add_item(MatchSettlementRatingSelect(owner=self))
        container.add_item(selection)
        actions = discord.ui.ActionRow()
        actions.add_item(MatchSettlementRatingPreviewButton(owner=self))
        actions.add_item(MatchSettlementRatingSelectionCancelButton(owner=self))
        container.add_item(actions)
        self.add_item(container)

    async def update_selection(self, interaction: discord.Interaction, *, values: tuple[str, ...]) -> None:
        await self.adapter.update_rating_selection(
            interaction,
            context=self.context,
            draft=self.draft,
            values=values,
            source_view=self,
        )

    async def preview(self, interaction: discord.Interaction) -> None:
        await self.adapter.apply_rating_selection(
            interaction,
            context=self.context,
            draft=self.draft,
            source_view=self,
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        await self.adapter.cancel_preview(interaction, context=self.context, source_view=self)


class MatchSettlementPageButton(discord.ui.Button):
    def __init__(
        self,
        *,
        owner: MatchSettlementPreviewView,
        direction: int,
        disabled: bool,
    ) -> None:
        self._owner = owner
        self._direction = direction
        super().__init__(
            label=PREVIOUS_LABEL if direction < 0 else NEXT_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"match-settlement-page-{'previous' if direction < 0 else 'next'}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.change_page(interaction, direction=self._direction)


class MatchSettlementConfirmButton(discord.ui.Button):
    def __init__(self, *, owner: MatchSettlementPreviewView) -> None:
        self._owner = owner
        super().__init__(
            label=CONFIRM_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id="match-settlement-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.confirm(interaction)


class MatchSettlementRatingSelectionBackButton(discord.ui.Button):
    def __init__(self, *, owner: MatchSettlementPreviewView) -> None:
        self._owner = owner
        super().__init__(
            label=RATING_SELECTION_BACK_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-settlement-rating-selection-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.back_to_selection(interaction)


class MatchSettlementCancelButton(discord.ui.Button):
    def __init__(self, *, owner: MatchSettlementPreviewView) -> None:
        self._owner = owner
        super().__init__(
            label=CANCEL_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-settlement-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.cancel(interaction)


class MatchSettlementPreviewView(discord.ui.LayoutView):
    """Bound Preview holding only detached Application DTOs."""

    def __init__(
        self,
        *,
        adapter: MatchSettlementDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchSettlementPreview,
        page_index: int = 0,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        pages = _rating_page_count(preview)
        if not 0 <= page_index < pages:
            raise ValueError("page_index is outside the settlement Preview.")
        self.adapter = adapter
        self.context = context
        self.preview = preview
        self.page_index = page_index
        self._confirmation_started = False
        container = discord.ui.Container(
            discord.ui.TextDisplay(format_match_settlement_preview(preview, page_index=page_index))
        )
        navigation = discord.ui.ActionRow()
        navigation.add_item(MatchSettlementPageButton(owner=self, direction=-1, disabled=page_index == 0))
        navigation.add_item(MatchSettlementPageButton(owner=self, direction=1, disabled=page_index + 1 >= pages))
        container.add_item(navigation)
        actions = discord.ui.ActionRow()
        if preview.plan.target.grade is not MatchGrade.OP:
            actions.add_item(MatchSettlementRatingSelectionBackButton(owner=self))
        actions.add_item(MatchSettlementConfirmButton(owner=self))
        actions.add_item(MatchSettlementCancelButton(owner=self))
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
        await self.adapter.confirm_settlement(
            interaction,
            context=self.context,
            preview=self.preview,
            source_view=self,
        )

    async def back_to_selection(self, interaction: discord.Interaction) -> None:
        await self.adapter.back_to_rating_selection(
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
class MatchSettlementDiscordAdapter:
    """Translate one bound staff settlement flow into query and command calls."""

    queries: MatchSettlementQueries
    commands: MatchSettlementCommands
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
            return match_settlement_autocomplete_choices(targets)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return []

    async def preview_settlement(
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
            response_kind="settlement-preview",
        ):
            return
        source_view.stop()
        try:
            selection_target = await self.blocking_runner(lambda: self.queries.get_rating_selection(match_id=match_id))
            if selection_target.grade is not MatchGrade.OP:
                draft = MatchSettlementRatingSelectionDraft(
                    target=selection_target,
                    reason=reason,
                )
                await self._edit_layout(
                    interaction,
                    view=MatchSettlementRatingSelectionView(
                        adapter=self,
                        context=context,
                        draft=draft,
                    ),
                )
                return
            plan = await self.blocking_runner(
                lambda: self.queries.get_preview(
                    match_id=match_id,
                    excluded_rating_entry_ids=(),
                )
            )
            preview = MatchSettlementPreview(
                plan=plan,
                selection_target=MatchSettlementRatingSelectionTarget.from_settlement_target(plan.target),
                reason=reason,
            )
            await self._edit_layout(
                interaction,
                view=MatchSettlementPreviewView(adapter=self, context=context, preview=preview),
            )
        except (MatchSettlementError, TypeError, ValueError) as error:
            await self._edit_deferred(interaction, preview_error(error))
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                preview_internal_error(correlation_id(interaction)),
            )

    async def update_rating_selection(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchSettlementRatingSelectionDraft,
        values: tuple[str, ...],
        source_view: MatchSettlementRatingSelectionView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="rating-selection-update",
        ):
            return
        source_view.stop()
        try:
            excluded = tuple(int(value) for value in values)
            updated = MatchSettlementRatingSelectionDraft(
                target=draft.target,
                excluded_rating_entry_ids=excluded,
                reason=draft.reason,
            )
            await self._edit_layout(
                interaction,
                view=MatchSettlementRatingSelectionView(
                    adapter=self,
                    context=context,
                    draft=updated,
                ),
            )
        except (TypeError, ValueError):
            await self._edit_terminal(interaction, message=BOUND_INTERACTION_ERROR)

    async def apply_rating_selection(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchSettlementRatingSelectionDraft,
        source_view: MatchSettlementRatingSelectionView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="rating-selection-preview",
        ):
            return
        source_view.stop()
        try:
            plan = await self.blocking_runner(
                lambda: self.queries.get_preview(
                    match_id=draft.target.match_id,
                    excluded_rating_entry_ids=draft.excluded_rating_entry_ids,
                )
            )
            preview = MatchSettlementPreview(
                plan=plan,
                selection_target=MatchSettlementRatingSelectionTarget.from_settlement_target(plan.target),
                reason=draft.reason,
            )
            view: discord.ui.LayoutView = MatchSettlementPreviewView(
                adapter=self,
                context=context,
                preview=preview,
            )
        except (MatchSettlementError, TypeError, ValueError) as error:
            view = MatchSettlementRatingSelectionView(
                adapter=self,
                context=context,
                draft=draft,
                error=preview_error(error),
            )
        except Exception:
            self._log_application_failure(interaction)
            view = MatchSettlementRatingSelectionView(
                adapter=self,
                context=context,
                draft=draft,
                error=preview_internal_error(correlation_id(interaction)),
            )
        await self._edit_layout(interaction, view=view)

    async def back_to_rating_selection(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        preview: MatchSettlementPreview,
        source_view: MatchSettlementPreviewView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="rating-selection-back",
        ):
            return
        source_view.stop()
        draft = MatchSettlementRatingSelectionDraft(
            target=preview.selection_target,
            excluded_rating_entry_ids=preview.plan.excluded_rating_entry_ids,
            reason=preview.reason,
        )
        await self._edit_layout(
            interaction,
            view=MatchSettlementRatingSelectionView(
                adapter=self,
                context=context,
                draft=draft,
            ),
        )

    async def confirm_settlement(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        preview: MatchSettlementPreview,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not context.matches(interaction):
            await self._send_component_error(
                interaction,
                BOUND_CONFIRM_ERROR,
            )
            return
        if not await self._defer_message_update(interaction, response_kind="settlement-final"):
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
                lambda: self.commands.settle_match(
                    SettleMatch(
                        match_id=preview.plan.target.match_id,
                        expected_settlement_fingerprint=preview.plan.settlement_fingerprint,
                        excluded_rating_entry_ids=preview.plan.excluded_rating_entry_ids,
                        idempotency_key=f"match-settlement:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        reason=preview.reason,
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except MatchSettlementIdempotencyConflictError:
            message = IDEMPOTENCY_CONFLICT
        except MatchSettlementStaleError:
            message = STALE
        except MatchSettlementRuleUnavailableError:
            message = RULE_UNAVAILABLE
        except MatchSettlementWalletUnavailableError:
            message = WALLET_UNAVAILABLE
        except MatchSettlementUnavailableError:
            message = UNAVAILABLE
        except (MatchSettlementAuditError, MatchSettlementError, TypeError, ValueError) as error:
            message = settlement_error(error)
        except Exception:
            self._log_application_failure(interaction)
            message = settlement_internal_error(correlation_id(interaction))
        else:
            message = format_match_settlement_success(result)
        await self._edit_terminal(interaction, message=message)

    async def cancel_preview(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="settlement-cancel",
        ):
            return
        source_view.stop()
        await self._edit_terminal(interaction, message=CANCELLED)

    async def change_preview_page(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        preview: MatchSettlementPreview,
        page_index: int,
        source_view: MatchSettlementPreviewView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="settlement-page",
        ):
            return
        source_view.stop()
        await self._edit_layout(
            interaction,
            view=MatchSettlementPreviewView(
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
            MatchSettlementDiscordAdapter._log_delivery_failure(interaction, "layout", error=error)
            await MatchSettlementDiscordAdapter._send_reopen_notice(interaction)

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
            MatchSettlementDiscordAdapter._log_delivery_failure(interaction, "reopen-notice")

    @staticmethod
    async def _send_component_error(interaction: discord.Interaction, message: str) -> None:
        try:
            await interaction.response.send_message(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            MatchSettlementDiscordAdapter._log_delivery_failure(interaction, "component-error")

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
