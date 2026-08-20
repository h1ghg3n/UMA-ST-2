from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import Race, RaceEntry
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.services.race_eligibility import room_match_betting_predicates


@dataclass(frozen=True)
class OpenMatchRace:
    race_id: int
    name: str
    starts_at: datetime | None
    participant_count: int | None


def list_open_match_races(
    session: Session,
    *,
    limit: int = 20,
    as_of: datetime | None = None,
    bet_close_minutes: int = 0,
) -> list[OpenMatchRace]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("race list limit must be between 1 and 100")
    if (
        not isinstance(bet_close_minutes, int)
        or isinstance(bet_close_minutes, bool)
        or not 0 <= bet_close_minutes <= 10_080
    ):
        raise ValueError("bet close minutes must be an integer between 0 and 10080")
    current_time = as_of or datetime.now(UTC)
    if not isinstance(current_time, datetime) or current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("race list time must be timezone-aware")
    entry_count = (
        select(func.count(RaceEntry.id))
        .where(
            RaceEntry.race_id == Race.id,
            RaceEntry.entry_kind == "room_match",
        )
        .correlate(Race)
        .scalar_subquery()
    )
    rows = session.execute(
        select(Race.id, Race.name, Race.starts_at, entry_count)
        .where(*room_match_betting_predicates(as_of=current_time, bet_close_minutes=bet_close_minutes))
        .order_by(Race.starts_at.is_(None), Race.starts_at, Race.id)
        .limit(limit)
    )
    return [
        OpenMatchRace(
            race_id=race_id,
            name=name,
            starts_at=database_datetime_as_utc(starts_at) if starts_at is not None else None,
            participant_count=participant_count,
        )
        for race_id, name, starts_at, participant_count in rows
    ]
