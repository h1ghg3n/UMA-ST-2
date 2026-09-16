"""Discord adapter for native Match result-publication recovery."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from uma_st2.application.match import (
    MatchResultPublicationAlreadyExistsError,
    MatchResultPublicationAuditError,
    MatchResultPublicationCommands,
    MatchResultPublicationError,
    MatchResultPublicationIdempotencyConflictError,
    MatchResultPublicationInvalidSourceError,
    MatchResultPublicationQueries,
    MatchResultPublicationUnavailableError,
    PublishMatchResult,
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
from .strings.match_staff_publication import (
    PUBLICATION_ALREADY_EXISTS,
    PUBLICATION_IDEMPOTENCY_CONFLICT,
    PUBLICATION_UNAVAILABLE,
    format_match_result_publication_success,
    match_result_publication_autocomplete_choices,
    publication_evidence_error,
    publication_input_error,
    publication_internal_error,
)
from .strings.match_staff_workflows import BOUND_INTERACTION_ERROR, SETTLEMENT_TRANSITION_ERROR

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.staff.publish"
_COMPONENT_TIMEOUT_SECONDS = 600.0


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} is unavailable.")
    return value


@dataclass(frozen=True, slots=True)
class MatchResultPublicationDiscordAdapter:
    """Translate direct staff publish into one bounded Application command."""

    queries: MatchResultPublicationQueries
    commands: MatchResultPublicationCommands
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
            return match_result_publication_autocomplete_choices(targets)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return []

    async def publish_result(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not context.matches(interaction):
            await self._send_component_error(interaction, BOUND_INTERACTION_ERROR)
            return
        if not await self._defer_message_update(interaction):
            return
        if not await self.authorize_interaction(interaction, _COMMAND_NAME):
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            guild_id = _required_snowflake(getattr(interaction, "guild_id", None), field_name="guild ID")
            result = await self.blocking_runner(
                lambda: self.commands.publish_result(
                    PublishMatchResult(
                        match_id=match_id,
                        idempotency_key=f"match-result-publish:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except MatchResultPublicationAlreadyExistsError:
            message = PUBLICATION_ALREADY_EXISTS
        except MatchResultPublicationUnavailableError:
            message = PUBLICATION_UNAVAILABLE
        except MatchResultPublicationIdempotencyConflictError:
            message = PUBLICATION_IDEMPOTENCY_CONFLICT
        except (MatchResultPublicationAuditError, MatchResultPublicationInvalidSourceError):
            self._log_application_failure(interaction)
            message = publication_evidence_error(correlation_id(interaction))
        except (MatchResultPublicationError, TypeError, ValueError) as error:
            message = publication_input_error(error)
        except Exception:
            self._log_application_failure(interaction)
            message = publication_internal_error(correlation_id(interaction))
        else:
            message = format_match_result_publication_success(result)
        await self._edit_deferred(interaction, message)

    @staticmethod
    async def _defer_message_update(interaction: discord.Interaction) -> bool:
        try:
            await interaction.response.defer(thinking=False)
        except Exception as error:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=publication-defer error_type=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
                type(error).__name__,
            )
            return False
        return True

    @staticmethod
    async def _edit_deferred(interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.edit_original_response(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=buttonless_terminal_layout(content, timeout_seconds=_COMPONENT_TIMEOUT_SECONDS),
            )
        except Exception as error:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=deferred error_type=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
                type(error).__name__,
            )
            await MatchResultPublicationDiscordAdapter._send_reopen_notice(interaction)

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
    async def _send_reopen_notice(interaction: discord.Interaction) -> None:
        try:
            await interaction.followup.send(
                SETTLEMENT_TRANSITION_ERROR,
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
