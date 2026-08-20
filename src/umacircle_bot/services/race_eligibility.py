from datetime import datetime

from sqlalchemy import exists

from umacircle_bot.db.models import Race, RaceEntry, RaceResult
from umacircle_bot.domain.races import MatchRaceStatus


def room_match_betting_predicates(*, as_of: datetime, bet_close_minutes: int) -> tuple[object, ...]:
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("race eligibility time must be timezone-aware")
    if (
        not isinstance(bet_close_minutes, int)
        or isinstance(bet_close_minutes, bool)
        or not 0 <= bet_close_minutes <= 10_080
    ):
        raise ValueError("bet close minutes must be an integer between 0 and 10080")
    return (
        Race.race_kind == "room_match",
        Race.external_source.is_(None),
        Race.status == MatchRaceStatus.BETTING_OPEN.value,
        exists().where(
            RaceEntry.race_id == Race.id,
            RaceEntry.entry_kind == "room_match",
        ),
        ~exists().where(RaceResult.race_id == Race.id),
    )
