from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from umacircle_bot.config import get_settings
from umacircle_bot.domain.errors import IdentityStateError
from umacircle_bot.services.accounts import resolve_owned_game_account
from umacircle_bot.services.application import run_application_command, run_application_query
from umacircle_bot.services.autocomplete_queries import (
    AutocompleteChoice,
    RoomRaceChoicePurpose,
    autocomplete_owned_game_accounts,
    autocomplete_room_races,
)
from umacircle_bot.services.betting import place_match_bet
from umacircle_bot.services.dtos import MatchBetDTO
from umacircle_bot.services.race_queries import OpenMatchRace, list_open_match_races


@dataclass(frozen=True, slots=True)
class PlaceMemberMatchBetCommand:
    discord_user_id: str
    race_id: int
    bet_type: str
    numbers: tuple[int, ...]
    amount: int
    idempotency_key: str
    game_account_id: int | None = None


def query_open_member_match_races() -> tuple[OpenMatchRace, ...]:
    """Return the bounded member-visible betting race list."""

    settings = get_settings()
    return run_application_query(
        lambda session: tuple(
            list_open_match_races(
                session,
                limit=10,
                bet_close_minutes=settings.bet_close_minutes,
            )
        )
    )


def query_member_match_bet_races(*, query: str = "") -> tuple[AutocompleteChoice, ...]:
    """Return bettable Room Match races through the read-only boundary."""

    return run_application_query(
        lambda session: autocomplete_room_races(
            session,
            purpose=RoomRaceChoicePurpose.BET,
            query=query,
        )
    )


def query_member_match_bet_accounts(
    *,
    discord_user_id: str,
    query: str = "",
) -> tuple[AutocompleteChoice, ...]:
    """Return only GameAccounts available as this member's Bet provenance."""

    return run_application_query(
        lambda session: autocomplete_owned_game_accounts(
            session,
            discord_user_id=discord_user_id,
            query=query,
        )
    )


def execute_member_match_bet(command: PlaceMemberMatchBetCommand) -> MatchBetDTO:
    """Resolve the member account and place one Bet in a single transaction."""

    settings = get_settings()

    def operation(session: Session) -> MatchBetDTO:
        account = resolve_owned_game_account(
            session,
            discord_user_id=command.discord_user_id,
            game_account_id=command.game_account_id,
        )
        if account.persona_id is None:
            raise IdentityStateError("owned game account has no Persona")
        return place_match_bet(
            session,
            game_account_id=account.id,
            race_id=command.race_id,
            bet_type=command.bet_type,
            numbers=command.numbers,
            amount=command.amount,
            bet_close_minutes=settings.bet_close_minutes,
            minimum_amount=settings.min_match_bet_amount,
            maximum_amount=settings.max_match_bet_amount,
            idempotency_key=command.idempotency_key,
            expected_persona_id=account.persona_id,
            actor_discord_user_id=command.discord_user_id,
        )

    return run_application_command(operation)
