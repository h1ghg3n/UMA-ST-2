"""Discord adapter for private current Room Match Rating standings."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord

from uma_st2.application.rating import (
    MatchRatingFilterConflictError,
    MatchRatingInvalidSourceError,
    MatchRatingQueries,
    MatchRatingQueryError,
    MatchRatingResultTooLargeError,
)

from .common import (
    BlockingApplicationRunner,
    PrepareDiscordCommand,
    correlation_id,
    run_blocking_application,
    send_deferred_response_safely,
    send_ephemeral_internal_error_after_defer_safely,
)
from .strings.match_member_rating import (
    RATING_EMPTY,
    RATING_FILTER_CONFLICT,
    RATING_FILTER_INVALID,
    RATING_RESULT_TOO_LARGE,
    format_match_rating_pages,
)

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.ratings"


@dataclass(frozen=True, slots=True)
class MatchRatingDiscordAdapter:
    """Authorize, query, and completely render current Rating standings."""

    queries: MatchRatingQueries
    prepare_command: PrepareDiscordCommand
    blocking_runner: BlockingApplicationRunner = run_blocking_application

    async def list_ratings(
        self,
        interaction: discord.Interaction,
        *,
        rank: int | None = None,
        persona: str | None = None,
    ) -> None:
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            standings = await self.blocking_runner(lambda: self.queries.list_ratings(rank=rank, persona=persona))
            pages = format_match_rating_pages(standings)
        except MatchRatingFilterConflictError:
            await send_deferred_response_safely(interaction, _COMMAND_NAME, RATING_FILTER_CONFLICT)
            return
        except MatchRatingResultTooLargeError:
            await send_deferred_response_safely(interaction, _COMMAND_NAME, RATING_RESULT_TOO_LARGE)
            return
        except MatchRatingInvalidSourceError:
            await send_ephemeral_internal_error_after_defer_safely(interaction, _COMMAND_NAME)
            return
        except MatchRatingQueryError:
            await send_deferred_response_safely(interaction, _COMMAND_NAME, RATING_FILTER_INVALID)
            return
        except Exception:
            await send_ephemeral_internal_error_after_defer_safely(interaction, _COMMAND_NAME)
            return

        if not pages:
            await send_deferred_response_safely(interaction, _COMMAND_NAME, RATING_EMPTY)
            return
        await self._send_pages(interaction, pages)

    @staticmethod
    async def _send_pages(
        interaction: discord.Interaction,
        pages: tuple[discord.Embed, ...],
    ) -> None:
        try:
            await interaction.edit_original_response(
                content="\u200b",
                embeds=[pages[0]],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=None,
            )
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=private-embed",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return
        for page in pages[1:]:
            try:
                await interaction.followup.send(
                    "\u200b",
                    embed=page,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                logger.error(
                    "Discord response failed correlation_id=%s command=%s response_kind=private-embed-followup",
                    correlation_id(interaction),
                    _COMMAND_NAME,
                )
                return
