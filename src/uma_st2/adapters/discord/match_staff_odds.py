"""Private Discord View for operator-controlled periodic Match odds mode."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import discord

from uma_st2.application.match import (
    ChangeMatchOddsRefreshMode,
    MatchOddsPublicationAuditError,
    MatchOddsPublicationCommands,
    MatchOddsPublicationIdempotencyConflictError,
    MatchOddsPublicationInvalidSourceError,
    MatchOddsPublicationNoChangeError,
    MatchOddsPublicationQueries,
    MatchOddsPublicationUnavailableError,
    MatchOddsRefreshStatus,
)
from uma_st2.application.publication import MatchOddsRefreshMode

from .common import AuthorizeDiscordInteraction, BlockingApplicationRunner, correlation_id, run_blocking_application
from .match_staff import MatchStaffInteractionContext
from .strings import match_staff_odds as copy
from .strings.match_staff_workflows import RACE_TRANSITION_ERROR

logger = logging.getLogger(__name__)

_COMPONENT_TIMEOUT_SECONDS = 600.0
_COMMAND_NAME = "match.staff.race"


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive Discord snowflake.")
    return value


class MatchOddsModeNavigation(Protocol):
    async def return_to_race_panel(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView | None,
    ) -> None: ...


class MatchOddsModeButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: MatchOddsModeDiscordAdapter,
        navigation: MatchOddsModeNavigation,
        context: MatchStaffInteractionContext,
        mode: MatchOddsRefreshMode,
        disabled: bool,
    ) -> None:
        self._adapter = adapter
        self._navigation = navigation
        self._context = context
        self._mode = mode
        super().__init__(
            label=copy.NORMAL_LABEL if mode is MatchOddsRefreshMode.NORMAL else copy.LIVE_LABEL,
            style=(discord.ButtonStyle.primary if mode is MatchOddsRefreshMode.LIVE else discord.ButtonStyle.secondary),
            custom_id=f"match-staff-odds-mode-{mode.value}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.change_mode(
            interaction,
            navigation=self._navigation,
            context=self._context,
            desired_mode=self._mode,
            source_view=self.view,
        )


class MatchOddsModeBackButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        navigation: MatchOddsModeNavigation,
        context: MatchStaffInteractionContext,
    ) -> None:
        self._navigation = navigation
        self._context = context
        super().__init__(
            label=copy.BACK_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-staff-odds-mode-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._navigation.return_to_race_panel(
            interaction,
            context=self._context,
            source_view=self.view,
        )


class MatchOddsModeView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: MatchOddsModeDiscordAdapter,
        navigation: MatchOddsModeNavigation,
        context: MatchStaffInteractionContext,
        status: MatchOddsRefreshStatus,
        success: str | None = None,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_status(status, success=success)))
        mode_row = discord.ui.ActionRow()
        for mode in MatchOddsRefreshMode:
            mode_row.add_item(
                MatchOddsModeButton(
                    adapter=adapter,
                    navigation=navigation,
                    context=context,
                    mode=mode,
                    disabled=mode is status.mode,
                )
            )
        back_row = discord.ui.ActionRow()
        back_row.add_item(MatchOddsModeBackButton(navigation=navigation, context=context))
        container.add_item(mode_row)
        container.add_item(back_row)
        self.add_item(container)


@dataclass(frozen=True, slots=True)
class MatchOddsModeDiscordAdapter:
    queries: MatchOddsPublicationQueries
    commands: MatchOddsPublicationCommands
    authorize_interaction: AuthorizeDiscordInteraction
    run_application: BlockingApplicationRunner = run_blocking_application

    async def show_status(
        self,
        interaction: discord.Interaction,
        *,
        navigation: MatchOddsModeNavigation,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not context.matches(interaction):
            await self._send_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return
        if not await self._defer_message_update(interaction, response_kind="odds-status-open"):
            return
        try:
            status = await self.run_application(lambda: self.queries.get_status(guild_id=str(context.guild_id)))
        except MatchOddsPublicationUnavailableError:
            await self._send_error(interaction, copy.UNAVAILABLE)
            return
        except Exception:
            self._log_failure(interaction)
            await self._send_error(interaction, copy.INTERNAL_ERROR)
            return
        view = MatchOddsModeView(
            adapter=self,
            navigation=navigation,
            context=context,
            status=status,
        )
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(interaction, view=view, response_kind="odds-status-open")

    async def change_mode(
        self,
        interaction: discord.Interaction,
        *,
        navigation: MatchOddsModeNavigation,
        context: MatchStaffInteractionContext,
        desired_mode: MatchOddsRefreshMode,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="odds-mode-change",
        ):
            return
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            result = await self.run_application(
                lambda: self.commands.change_mode(
                    ChangeMatchOddsRefreshMode(
                        guild_id=str(context.guild_id),
                        desired_mode=desired_mode,
                        actor_discord_user_id=str(context.user_id),
                        idempotency_key=f"match-odds-mode:{interaction_id}",
                        correlation_id=str(interaction_id),
                    )
                )
            )
            status = MatchOddsRefreshStatus(
                guild_id=result.guild_id,
                mode=result.mode,
                next_refresh_at=result.next_refresh_at,
                open_match_count=result.open_match_count,
            )
        except MatchOddsPublicationUnavailableError:
            await self._send_error(interaction, copy.UNAVAILABLE)
            return
        except MatchOddsPublicationNoChangeError:
            await self._send_error(interaction, copy.NO_CHANGE)
            return
        except MatchOddsPublicationIdempotencyConflictError:
            await self._send_error(interaction, copy.IDEMPOTENCY_CONFLICT)
            return
        except MatchOddsPublicationInvalidSourceError:
            await self._send_error(interaction, copy.INVALID_SOURCE)
            return
        except MatchOddsPublicationAuditError:
            self._log_failure(interaction)
            await self._send_error(interaction, copy.INTERNAL_ERROR)
            return
        except Exception:
            self._log_failure(interaction)
            await self._send_error(interaction, copy.INTERNAL_ERROR)
            return
        view = MatchOddsModeView(
            adapter=self,
            navigation=navigation,
            context=context,
            status=status,
            success=copy.format_success(result),
        )
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(interaction, view=view, response_kind="odds-mode-change")

    async def _prepare_bound_update(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        response_kind: str,
    ) -> bool:
        if not context.matches(interaction):
            await self._send_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return False
        if not await self._defer_message_update(interaction, response_kind=response_kind):
            return False
        return await self.authorize_interaction(interaction, _COMMAND_NAME)

    @staticmethod
    async def _defer_message_update(
        interaction: discord.Interaction,
        *,
        response_kind: str,
    ) -> bool:
        try:
            await interaction.response.defer(thinking=False)
        except Exception as error:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=%s-defer error_type=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
                response_kind,
                type(error).__name__,
            )
            return False
        return True

    @staticmethod
    async def _edit_layout(
        interaction: discord.Interaction,
        *,
        view: discord.ui.LayoutView,
        response_kind: str,
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
                "Discord response failed correlation_id=%s command=%s response_kind=%s error_type=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
                response_kind,
                type(error).__name__,
            )
            await MatchOddsModeDiscordAdapter._send_error(interaction, RACE_TRANSITION_ERROR)

    @staticmethod
    async def _send_error(interaction: discord.Interaction, content: str) -> None:
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
                "Discord response failed correlation_id=%s command=%s response_kind=component-error",
                correlation_id(interaction),
                _COMMAND_NAME,
            )

    @staticmethod
    def _log_failure(interaction: object) -> None:
        logger.error(
            "Discord application call failed correlation_id=%s command=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
        )
