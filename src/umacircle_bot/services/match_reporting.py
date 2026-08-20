from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import GameAccount, Persona, Race, RaceResult, RatingEvent

_OFFICIAL_RACE_STATUSES = ("result_confirmed", "settled")


@dataclass(frozen=True, slots=True)
class CharacterUsageDTO:
    character_name: str
    entry_count: int


@dataclass(frozen=True, slots=True)
class PersonaMatchActivityDTO:
    persona_id: str
    display_name: str
    participated_race_count: int
    entry_count: int
    wins: int
    podiums: int
    average_finish: Decimal | None
    character_usage: tuple[CharacterUsageDTO, ...]
    distinct_character_count: int
    void_result_count: int


@dataclass(frozen=True, slots=True)
class GameAccountRatingDTO:
    game_account_id: int
    account_display: str
    current_owner_persona_id: str | None
    current_owner_display_name: str | None
    opening_rating: Decimal
    current_rating: Decimal
    total_delta: Decimal
    rated_race_count: int
    competition_rank: int


@dataclass(slots=True)
class _ActivityAccumulator:
    display_name: str
    race_ids: set[int]
    entry_count: int
    wins: int
    podiums: int
    finish_total: int
    characters: Counter[str]
    void_result_count: int


def get_persona_match_activity_report(session: Session) -> tuple[PersonaMatchActivityDTO, ...]:
    """Return immutable-owner Persona activity without consulting current account ownership."""

    rows = session.execute(
        select(
            RaceResult.owner_at_event_persona_id,
            Persona.display_name,
            RaceResult.race_id,
            RaceResult.rank,
            RaceResult.character_name,
            RaceResult.is_result_void,
            Race.status,
        )
        .join(Race, Race.id == RaceResult.race_id)
        .join(Persona, Persona.id == RaceResult.owner_at_event_persona_id)
        .where(
            Race.race_kind == "room_match",
            RaceResult.owner_at_event_persona_id.is_not(None),
            Race.status.in_((*_OFFICIAL_RACE_STATUSES, "voided")),
        )
        .order_by(RaceResult.owner_at_event_persona_id, RaceResult.id)
    )

    grouped: dict[str, _ActivityAccumulator] = {}
    for owner_id, display_name, race_id, rank, character_name, result_void, race_status in rows:
        accumulator = grouped.setdefault(
            owner_id,
            _ActivityAccumulator(
                display_name=display_name,
                race_ids=set(),
                entry_count=0,
                wins=0,
                podiums=0,
                finish_total=0,
                characters=Counter(),
                void_result_count=0,
            ),
        )
        if race_status == "voided" or result_void:
            accumulator.void_result_count += 1
            continue

        accumulator.race_ids.add(race_id)
        accumulator.entry_count += 1
        accumulator.finish_total += rank
        accumulator.wins += rank == 1
        accumulator.podiums += rank <= 3
        if character_name is not None:
            accumulator.characters[character_name] += 1

    report = []
    for persona_id, values in grouped.items():
        usage = tuple(
            CharacterUsageDTO(character_name=name, entry_count=count)
            for name, count in sorted(
                values.characters.items(),
                key=lambda item: (-item[1], item[0].casefold(), item[0]),
            )
        )
        report.append(
            PersonaMatchActivityDTO(
                persona_id=persona_id,
                display_name=values.display_name,
                participated_race_count=len(values.race_ids),
                entry_count=values.entry_count,
                wins=values.wins,
                podiums=values.podiums,
                average_finish=(
                    Decimal(values.finish_total) / Decimal(values.entry_count) if values.entry_count else None
                ),
                character_usage=usage,
                distinct_character_count=len(usage),
                void_result_count=values.void_result_count,
            )
        )
    return tuple(sorted(report, key=lambda item: (item.display_name.casefold(), item.persona_id)))


def get_game_account_rating_report(session: Session) -> tuple[GameAccountRatingDTO, ...]:
    """Return account-scoped Rating ledgers with current owner as nullable metadata."""

    rows = session.execute(
        select(
            RatingEvent.id,
            RatingEvent.game_account_id,
            GameAccount.ingame_name,
            GameAccount.nickname,
            GameAccount.persona_id,
            Persona.display_name,
            RatingEvent.rating_before,
            RatingEvent.rating_after,
            RatingEvent.race_id,
            Race.race_kind,
            Race.status,
            RaceResult.is_result_void,
        )
        .join(GameAccount, GameAccount.id == RatingEvent.game_account_id)
        .outerjoin(Persona, Persona.id == GameAccount.persona_id)
        .join(Race, Race.id == RatingEvent.race_id)
        .join(RaceResult, RaceResult.id == RatingEvent.race_result_id)
        .order_by(RatingEvent.game_account_id, RatingEvent.id)
    )

    events_by_account: dict[int, list[object]] = defaultdict(list)
    for row in rows:
        events_by_account[row.game_account_id].append(row)

    unranked: list[GameAccountRatingDTO] = []
    for account_id, events in events_by_account.items():
        first = events[0]
        last = events[-1]
        rated_race_ids = {
            event.race_id
            for event in events
            if event.race_kind == "room_match" and event.status != "voided" and not event.is_result_void
        }
        opening = first.rating_before
        current = last.rating_after
        unranked.append(
            GameAccountRatingDTO(
                game_account_id=account_id,
                account_display=first.ingame_name or first.nickname or f"Account #{account_id}",
                current_owner_persona_id=first.persona_id,
                current_owner_display_name=first.display_name,
                opening_rating=opening,
                current_rating=current,
                total_delta=current - opening,
                rated_race_count=len(rated_race_ids),
                competition_rank=0,
            )
        )

    ordered = sorted(unranked, key=lambda item: (-item.current_rating, item.game_account_id))
    ranked: list[GameAccountRatingDTO] = []
    previous_rating: Decimal | None = None
    rank = 0
    for index, item in enumerate(ordered, start=1):
        if previous_rating is None or item.current_rating != previous_rating:
            rank = index
            previous_rating = item.current_rating
        ranked.append(
            GameAccountRatingDTO(
                game_account_id=item.game_account_id,
                account_display=item.account_display,
                current_owner_persona_id=item.current_owner_persona_id,
                current_owner_display_name=item.current_owner_display_name,
                opening_rating=item.opening_rating,
                current_rating=item.current_rating,
                total_delta=item.total_delta,
                rated_race_count=item.rated_race_count,
                competition_rank=rank,
            )
        )
    return tuple(ranked)


# Short aliases keep call sites readable while retaining explicit public names.
list_persona_match_activity = get_persona_match_activity_report
list_game_account_ratings = get_game_account_rating_report
