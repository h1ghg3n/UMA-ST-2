"""Discord adapter for native V2 member Match Bet placement."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from uma_st2.application.betting import (
    BetPlacementApprovalPendingError,
    BetPlacementAuditError,
    BetPlacementCommands,
    BetPlacementDuplicateError,
    BetPlacementError,
    BetPlacementIdempotencyConflictError,
    BetPlacementIdentityError,
    BetPlacementInsufficientBalanceError,
    BetPlacementInvalidSourceError,
    BetPlacementSelectionUnavailableError,
    BetPlacementStakeLimitError,
    BetPlacementUnavailableError,
    BetPlacementWalletUnavailableError,
    BetReplacementApprovalPendingError,
    BetReplacementAuditError,
    BetReplacementCommands,
    BetReplacementDuplicateError,
    BetReplacementError,
    BetReplacementIdempotencyConflictError,
    BetReplacementIdentityError,
    BetReplacementInsufficientBalanceError,
    BetReplacementInvalidSourceError,
    BetReplacementNoChangeError,
    BetReplacementSelectionUnavailableError,
    BetReplacementStakeLimitError,
    BetReplacementUnavailableError,
    BetReplacementWalletUnavailableError,
    MatchMemberBettingQueries,
    MatchRaceDetailUnavailableError,
    PlaceMatchBet,
    ReplaceMatchBet,
)
from uma_st2.domain.betting import BetType

from .common import (
    AuthorizeDiscordAutocomplete,
    BlockingApplicationRunner,
    PrepareDiscordCommand,
    bounded_discord_message,
    correlation_id,
    run_blocking_application,
    send_deferred_response_safely,
    send_ephemeral_internal_error_after_defer_safely,
    send_private_error_after_defer_safely,
    send_private_response_after_public_defer_safely,
)
from .strings.account import MEMBER_APPROVAL_PENDING
from .strings.match_member import (
    BET_DUPLICATE,
    BET_IDEMPOTENCY_CONFLICT,
    BET_IDENTITY_UNAVAILABLE,
    BET_INSUFFICIENT_BALANCE,
    BET_MATCH_UNAVAILABLE,
    BET_REPLACEMENT_DUPLICATE,
    BET_REPLACEMENT_IDEMPOTENCY_CONFLICT,
    BET_REPLACEMENT_IDENTITY_UNAVAILABLE,
    BET_REPLACEMENT_INSUFFICIENT_BALANCE,
    BET_REPLACEMENT_NO_CHANGE,
    BET_REPLACEMENT_SELECTION_UNAVAILABLE,
    BET_REPLACEMENT_UNAVAILABLE,
    BET_REPLACEMENT_WALLET_UNAVAILABLE,
    BET_SELECTION_UNAVAILABLE,
    BET_WALLET_UNAVAILABLE,
    ENTRY_NUMBERS_FORMAT_ERROR,
    ENTRY_NUMBERS_TYPE_ERROR,
    RACE_DETAIL_UNAVAILABLE,
    bet_audit_error,
    bet_input_error,
    bet_internal_error,
    bet_replacement_audit_error,
    bet_replacement_input_error,
    bet_replacement_internal_error,
    bet_replacement_stake_limit,
    bet_stake_limit,
    format_match_bet_replacement_success,
    format_match_bet_success,
    format_match_race_detail_pages,
    format_match_race_list,
    format_personal_match_bets,
    match_active_bet_autocomplete_choices,
    match_bet_amount_autocomplete_choices,
    match_bet_autocomplete_choices,
    match_race_autocomplete_choices,
)

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.bet"
_BET_CHANGE_COMMAND_NAME = "match.bet-change"
_RACES_COMMAND_NAME = "match.races"
_BETS_COMMAND_NAME = "match.bets"


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} is unavailable.")
    return value


def parse_match_bet_numbers(value: str) -> tuple[int, ...]:
    """Parse member Entry numbers while preserving the V1 comma/hyphen input style."""

    if not isinstance(value, str):
        raise ValueError(ENTRY_NUMBERS_TYPE_ERROR)
    parts = tuple(part.strip() for part in value.replace(",", "-").split("-"))
    if not parts or any(not part or not part.isascii() or not part.isdigit() or int(part) <= 0 for part in parts):
        raise ValueError(ENTRY_NUMBERS_FORMAT_ERROR)
    return tuple(int(part) for part in parts)


@dataclass(frozen=True, slots=True)
class MatchMemberBettingDiscordAdapter:
    """Translate direct member slash placement into bounded application calls."""

    queries: MatchMemberBettingQueries
    commands: BetPlacementCommands
    replacement_commands: BetReplacementCommands
    prepare_command: PrepareDiscordCommand
    authorize_autocomplete: AuthorizeDiscordAutocomplete
    blocking_runner: BlockingApplicationRunner = run_blocking_application

    async def list_races(self, interaction: discord.Interaction, *, match_id: int | None = None) -> None:
        """Render the public current Match list or one selected complete detail."""

        if not await self.prepare_command(interaction, _RACES_COMMAND_NAME, ephemeral=False):
            return
        try:
            if match_id is None:
                matches = await self.blocking_runner(lambda: self.queries.list_races(limit=10))
                pages = (format_match_race_list(matches),)
            else:
                detail = await self.blocking_runner(lambda: self.queries.get_race_detail(match_id=match_id))
                pages = format_match_race_detail_pages(detail)
        except MatchRaceDetailUnavailableError:
            await send_private_response_after_public_defer_safely(
                interaction,
                _RACES_COMMAND_NAME,
                RACE_DETAIL_UNAVAILABLE,
            )
            return
        except Exception:
            await send_private_error_after_defer_safely(interaction, _RACES_COMMAND_NAME)
            return
        await self._send_public_race_pages(interaction, pages)

    async def autocomplete_races(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        """Return current scheduled/open native Matches for optional detail."""

        try:
            if not await self.authorize_autocomplete(interaction, _RACES_COMMAND_NAME):
                return []
            matches = await self.blocking_runner(lambda: self.queries.search_races(search=current, limit=25))
            return match_race_autocomplete_choices(matches)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _RACES_COMMAND_NAME,
            )
            return []

    async def list_personal_bets(self, interaction: discord.Interaction) -> None:
        """Authorize and render the private current Persona-owned Bet list."""

        if not await self.prepare_command(interaction, _BETS_COMMAND_NAME, ephemeral=True):
            return
        try:
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            bets = await self.blocking_runner(
                lambda: self.queries.list_personal_bets(
                    actor_discord_user_id=str(actor_id),
                    limit=10,
                )
            )
            content = format_personal_match_bets(bets)
        except Exception:
            await send_ephemeral_internal_error_after_defer_safely(interaction, _BETS_COMMAND_NAME)
            return
        await send_deferred_response_safely(interaction, _BETS_COMMAND_NAME, content)

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        try:
            if not await self.authorize_autocomplete(interaction, _COMMAND_NAME):
                return []
            targets = await self.blocking_runner(lambda: self.queries.search_targets(search=current, limit=25))
            return match_bet_autocomplete_choices(targets)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return []

    async def autocomplete_amount(
        self,
        interaction: discord.Interaction,
        current: int | str,
    ) -> list[app_commands.Choice[int]]:
        """Show fresh private Point context while the member enters an amount."""

        try:
            if not await self.authorize_autocomplete(interaction, _COMMAND_NAME):
                return []
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            point_state = await self.blocking_runner(
                lambda: self.queries.get_input_point_state(
                    actor_discord_user_id=str(actor_id),
                )
            )
            return match_bet_amount_autocomplete_choices(
                current_amount=current,
                point_state=point_state,
            )
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s option=amount",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return []

    async def autocomplete_active_bets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        try:
            if not await self.authorize_autocomplete(interaction, _BET_CHANGE_COMMAND_NAME):
                return []
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            bets = await self.blocking_runner(
                lambda: self.queries.search_active_bets(
                    actor_discord_user_id=str(actor_id),
                    search=current,
                    limit=25,
                )
            )
            return match_active_bet_autocomplete_choices(bets)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _BET_CHANGE_COMMAND_NAME,
            )
            return []

    async def place_bet(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        bet_type: str,
        numbers: str,
        amount: int,
    ) -> None:
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            interaction_id = _required_snowflake(
                getattr(interaction, "id", None),
                field_name="interaction ID",
            )
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            guild_id = _required_snowflake(
                getattr(interaction, "guild_id", None),
                field_name="guild ID",
            )
            entry_numbers = parse_match_bet_numbers(numbers)
            result = await self.blocking_runner(
                lambda: self.commands.place_bet(
                    PlaceMatchBet(
                        match_id=match_id,
                        bet_type=BetType(bet_type),
                        entry_numbers=entry_numbers,
                        amount=amount,
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(guild_id),
                        idempotency_key=f"match-bet:{interaction_id}",
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except BetPlacementDuplicateError:
            message = BET_DUPLICATE
        except BetPlacementInsufficientBalanceError:
            message = BET_INSUFFICIENT_BALANCE
        except BetPlacementStakeLimitError as error:
            message = bet_stake_limit(error.maximum_stake)
        except BetPlacementApprovalPendingError:
            message = MEMBER_APPROVAL_PENDING
        except BetPlacementIdentityError:
            message = BET_IDENTITY_UNAVAILABLE
        except BetPlacementWalletUnavailableError:
            message = BET_WALLET_UNAVAILABLE
        except BetPlacementSelectionUnavailableError:
            message = BET_SELECTION_UNAVAILABLE
        except BetPlacementUnavailableError:
            message = BET_MATCH_UNAVAILABLE
        except BetPlacementIdempotencyConflictError:
            message = BET_IDEMPOTENCY_CONFLICT
        except (BetPlacementAuditError, BetPlacementInvalidSourceError):
            self._log_application_failure(interaction)
            message = bet_audit_error(correlation_id(interaction))
        except (BetPlacementError, TypeError, ValueError) as error:
            message = bet_input_error(error)
        except Exception:
            self._log_application_failure(interaction)
            message = bet_internal_error(correlation_id(interaction))
        else:
            message = format_match_bet_success(result)
        await self._edit_deferred(interaction, message)

    async def replace_bet(
        self,
        interaction: discord.Interaction,
        *,
        bet_id: int,
        bet_type: str,
        numbers: str,
        amount: int,
    ) -> None:
        if not await self.prepare_command(interaction, _BET_CHANGE_COMMAND_NAME, ephemeral=True):
            return
        try:
            interaction_id = _required_snowflake(
                getattr(interaction, "id", None),
                field_name="interaction ID",
            )
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            guild_id = _required_snowflake(
                getattr(interaction, "guild_id", None),
                field_name="guild ID",
            )
            entry_numbers = parse_match_bet_numbers(numbers)
            result = await self.blocking_runner(
                lambda: self.replacement_commands.replace_bet(
                    ReplaceMatchBet(
                        bet_id=bet_id,
                        bet_type=BetType(bet_type),
                        entry_numbers=entry_numbers,
                        amount=amount,
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(guild_id),
                        idempotency_key=f"match-bet-change:{interaction_id}",
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except BetReplacementNoChangeError:
            message = BET_REPLACEMENT_NO_CHANGE
        except BetReplacementDuplicateError:
            message = BET_REPLACEMENT_DUPLICATE
        except BetReplacementInsufficientBalanceError:
            message = BET_REPLACEMENT_INSUFFICIENT_BALANCE
        except BetReplacementStakeLimitError as error:
            message = bet_replacement_stake_limit(error.maximum_stake)
        except BetReplacementApprovalPendingError:
            message = MEMBER_APPROVAL_PENDING
        except BetReplacementIdentityError:
            message = BET_REPLACEMENT_IDENTITY_UNAVAILABLE
        except BetReplacementWalletUnavailableError:
            message = BET_REPLACEMENT_WALLET_UNAVAILABLE
        except BetReplacementSelectionUnavailableError:
            message = BET_REPLACEMENT_SELECTION_UNAVAILABLE
        except BetReplacementUnavailableError:
            message = BET_REPLACEMENT_UNAVAILABLE
        except BetReplacementIdempotencyConflictError:
            message = BET_REPLACEMENT_IDEMPOTENCY_CONFLICT
        except (BetReplacementAuditError, BetReplacementInvalidSourceError):
            self._log_application_failure(interaction, command_name=_BET_CHANGE_COMMAND_NAME)
            message = bet_replacement_audit_error(correlation_id(interaction))
        except (BetReplacementError, TypeError, ValueError) as error:
            message = bet_replacement_input_error(error)
        except Exception:
            self._log_application_failure(interaction, command_name=_BET_CHANGE_COMMAND_NAME)
            message = bet_replacement_internal_error(correlation_id(interaction))
        else:
            message = format_match_bet_replacement_success(result)
        await self._edit_deferred(interaction, message, command_name=_BET_CHANGE_COMMAND_NAME)

    @staticmethod
    async def _send_public_race_pages(
        interaction: discord.Interaction,
        pages: tuple[str, ...],
    ) -> None:
        if not pages:
            raise ValueError("Race response pages must not be empty.")
        if not await send_deferred_response_safely(
            interaction,
            _RACES_COMMAND_NAME,
            pages[0],
        ):
            return
        for page in pages[1:]:
            try:
                await interaction.followup.send(
                    page,
                    ephemeral=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                logger.error(
                    "Discord response failed correlation_id=%s command=%s response_kind=public-followup",
                    correlation_id(interaction),
                    _RACES_COMMAND_NAME,
                )
                return

    @staticmethod
    async def _edit_deferred(
        interaction: discord.Interaction,
        content: str,
        *,
        command_name: str = _COMMAND_NAME,
    ) -> None:
        try:
            await interaction.edit_original_response(
                content=bounded_discord_message((content,), limit=1900),
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=None,
            )
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=deferred",
                correlation_id(interaction),
                command_name,
            )

    @staticmethod
    def _log_application_failure(interaction: object, *, command_name: str = _COMMAND_NAME) -> None:
        logger.error(
            "Discord application call failed correlation_id=%s command=%s",
            correlation_id(interaction),
            command_name,
        )
