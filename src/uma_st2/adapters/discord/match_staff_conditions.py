"""Discord adapter for native Match condition setting."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from uma_st2.application.match import (
    MatchConditionAuditError,
    MatchConditionCommands,
    MatchConditionError,
    MatchConditionIdempotencyConflictError,
    MatchConditionReasonRequiredError,
    MatchConditionStaleError,
    MatchConditionTarget,
    MatchConditionUnavailableError,
    MatchConditionValues,
    MatchStaffConditionQueries,
    SetMatchConditions,
)
from uma_st2.domain.match import MatchSeason, MatchTimeOfDay, MatchTrackCondition, MatchWeather

from .common import (
    AuthorizeDiscordAutocomplete,
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    PrepareDiscordCommand,
    buttonless_terminal_layout,
    correlation_id,
    run_blocking_application,
)
from .match_staff import MatchStaffInteractionContext
from .strings.match_staff_conditions import (
    BOUND_CANCEL_ERROR,
    BOUND_CONFIRM_ERROR,
    CANCEL_LABEL,
    CANCELLED,
    CHANGE_REASON_REQUIRED,
    CHANGE_UNAVAILABLE,
    CONFIRM_LABEL,
    CONFIRMATION_STARTED,
    IDEMPOTENCY_CONFLICT,
    NO_CHANGE,
    REASON_TOO_LONG,
    STALE,
    TARGET_UNAVAILABLE,
    format_match_condition_success,
    format_match_condition_target,
    match_condition_autocomplete_choices,
    preview_error,
    preview_internal_error,
    save_error,
    save_internal_error,
)

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.staff.race-condition-set"
_COMPONENT_TIMEOUT_SECONDS = 600.0


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} is unavailable.")
    return value


def _normalized_reason(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 255:
        raise ValueError(REASON_TOO_LONG)
    return normalized


def format_match_condition_preview(preview: MatchConditionPreview) -> str:
    """Render one complete mention-safe before/after condition preview."""

    return format_match_condition_target(
        target=preview.target,
        values=preview.values,
        reason=preview.reason,
    )


@dataclass(frozen=True, slots=True)
class MatchConditionPreview:
    """Closed query DTO plus adapter-local desired values carried to Confirm."""

    target: MatchConditionTarget
    values: MatchConditionValues
    reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.target, MatchConditionTarget):
            raise ValueError("target must be MatchConditionTarget.")
        if not isinstance(self.values, MatchConditionValues):
            raise ValueError("values must be MatchConditionValues.")
        object.__setattr__(self, "reason", _normalized_reason(self.reason))


class MatchConditionConfirmButton(discord.ui.Button[discord.ui.LayoutView]):
    """Invoke the final condition mutation from the bound Preview."""

    def __init__(
        self,
        *,
        adapter: MatchConditionDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchConditionPreview,
        source_view: MatchConditionConfirmView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._source_view = source_view
        super().__init__(
            label=CONFIRM_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id="match-condition-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.confirm_conditions(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self._source_view,
        )


class MatchConditionCancelButton(discord.ui.Button[discord.ui.LayoutView]):
    """Close the bound condition Preview without a canonical write."""

    def __init__(
        self,
        *,
        adapter: MatchConditionDiscordAdapter,
        context: MatchStaffInteractionContext,
        source_view: MatchConditionConfirmView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._source_view = source_view
        super().__init__(
            label=CANCEL_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-condition-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.cancel_conditions(
            interaction,
            context=self._context,
            source_view=self._source_view,
        )


class MatchConditionConfirmView(discord.ui.LayoutView):
    """Final bound confirmation for one complete condition mutation."""

    def __init__(
        self,
        *,
        adapter: MatchConditionDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchConditionPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self.preview = preview
        self._confirmation_started = False
        container = discord.ui.Container(discord.ui.TextDisplay(format_match_condition_preview(preview)))
        actions = discord.ui.ActionRow()
        actions.add_item(
            MatchConditionConfirmButton(
                adapter=adapter,
                context=context,
                preview=preview,
                source_view=self,
            )
        )
        actions.add_item(
            MatchConditionCancelButton(
                adapter=adapter,
                context=context,
                source_view=self,
            )
        )
        container.add_item(actions)
        self.add_item(container)

    def claim_confirmation(self) -> bool:
        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True


@dataclass(frozen=True, slots=True)
class MatchConditionDiscordAdapter:
    """Translate Match condition interactions to bounded application ports."""

    queries: MatchStaffConditionQueries
    commands: MatchConditionCommands
    prepare_command: PrepareDiscordCommand
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
            return match_condition_autocomplete_choices(targets)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return []

    async def preview_conditions(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        season: MatchSeason | str,
        weather: MatchWeather | str,
        time_of_day: MatchTimeOfDay | str,
        track_condition: MatchTrackCondition | str,
        reason: str | None,
    ) -> None:
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            context = MatchStaffInteractionContext.from_interaction(interaction)
            target = await self.blocking_runner(lambda: self.queries.get_target(match_id=match_id))
            if target is None:
                raise MatchConditionUnavailableError(TARGET_UNAVAILABLE)
            values = MatchConditionValues(
                season=MatchSeason(season),
                weather=MatchWeather(weather),
                time_of_day=MatchTimeOfDay(time_of_day),
                track_condition=MatchTrackCondition(track_condition),
            )
            normalized_reason = _normalized_reason(reason)
            if target.condition is not None:
                if target.condition.values == values:
                    raise MatchConditionError(NO_CHANGE)
                if normalized_reason is None:
                    raise MatchConditionReasonRequiredError(CHANGE_REASON_REQUIRED)
            preview = MatchConditionPreview(target=target, values=values, reason=normalized_reason)
            await self._edit_layout(
                interaction,
                view=MatchConditionConfirmView(adapter=self, context=context, preview=preview),
            )
        except (MatchConditionError, TypeError, ValueError) as error:
            await self._edit_deferred(interaction, preview_error(error))
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                preview_internal_error(correlation_id(interaction)),
            )

    async def confirm_conditions(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        preview: MatchConditionPreview,
        source_view: MatchConditionConfirmView,
    ) -> None:
        if not context.matches(interaction):
            await self._send_component_error(
                interaction,
                BOUND_CONFIRM_ERROR,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        if not source_view.claim_confirmation():
            await self._edit_deferred(interaction, CONFIRMATION_STARTED)
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            command = SetMatchConditions(
                match_id=preview.target.match_id,
                values=preview.values,
                expected_condition=preview.target.condition,
                expected_condition_version=preview.target.condition_version,
                idempotency_key=f"match-condition:{interaction_id}",
                actor_discord_user_id=str(actor_id),
                guild_id=str(context.guild_id),
                correlation_id=str(interaction_id),
                reason=preview.reason,
            )
            result = await self.blocking_runner(lambda: self.commands.set_conditions(command))
        except MatchConditionIdempotencyConflictError:
            await self._edit_deferred(interaction, IDEMPOTENCY_CONFLICT)
        except MatchConditionStaleError:
            await self._edit_deferred(interaction, STALE)
        except MatchConditionUnavailableError:
            await self._edit_deferred(interaction, CHANGE_UNAVAILABLE)
        except (MatchConditionAuditError, MatchConditionError, TypeError, ValueError) as error:
            await self._edit_deferred(interaction, save_error(error))
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                save_internal_error(correlation_id(interaction)),
            )
        else:
            await self._edit_deferred(interaction, format_match_condition_success(result))

    async def cancel_conditions(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: MatchConditionConfirmView,
    ) -> None:
        if not context.matches(interaction):
            await self._send_component_error(
                interaction,
                BOUND_CANCEL_ERROR,
            )
            return
        if not await self.authorize_interaction(interaction, _COMMAND_NAME):
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
            self._log_delivery_failure(interaction, "condition-cancel")

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
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=layout",
                correlation_id(interaction),
                _COMMAND_NAME,
            )

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
    def _log_application_failure(interaction: object) -> None:
        logger.error(
            "Discord application call failed correlation_id=%s command=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
        )

    @staticmethod
    def _log_delivery_failure(interaction: object, response_kind: str) -> None:
        logger.error(
            "Discord response failed correlation_id=%s command=%s response_kind=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
            response_kind,
        )

    @staticmethod
    def _terminal_layout(message: str) -> discord.ui.LayoutView:
        return buttonless_terminal_layout(message, timeout_seconds=_COMPONENT_TIMEOUT_SECONDS)
