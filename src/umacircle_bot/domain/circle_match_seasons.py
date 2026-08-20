from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum

CIRCLE_MATCH_SEASON_TIMEZONE_NAME = "Asia/Seoul"
_KST = timezone(timedelta(hours=9), name="KST")


@dataclass(frozen=True, slots=True)
class CircleMatchSeason:
    key: str
    name: str
    starts_at: datetime
    ends_at: datetime
    alias: str | None = None

    @property
    def display_name(self) -> str:
        if self.alias is None:
            return self.name
        normalized_alias = " ".join(self.alias.split())
        if not normalized_alias:
            return self.name
        return f"{self.name} ({normalized_alias})"


class CircleMatchSeasonStatus(StrEnum):
    HISTORICAL = "historical"
    CURRENT = "current"
    FUTURE = "future"


def circle_match_season_for(starts_at: datetime, *, alias: str | None = None) -> CircleMatchSeason:
    """Return the calendar-half Circle Match season containing an instant.

    Circle Match seasons use inclusive KST starts and exclusive KST ends:
    January 1 to July 1 is Split 1, and July 1 to the next January 1 is Split 2.
    Returned boundaries are UTC instants suitable for database queries.
    """

    if not isinstance(starts_at, datetime) or starts_at.tzinfo is None or starts_at.utcoffset() is None:
        raise ValueError("Circle Match season requires a timezone-aware start time")

    local = starts_at.astimezone(_KST)
    if local.month <= 6:
        split = 1
        local_start = datetime(local.year, 1, 1, tzinfo=_KST)
        local_end = datetime(local.year, 7, 1, tzinfo=_KST)
    else:
        split = 2
        local_start = datetime(local.year, 7, 1, tzinfo=_KST)
        local_end = datetime(local.year + 1, 1, 1, tzinfo=_KST)
    return CircleMatchSeason(
        key=f"{local.year}-split-{split}",
        name=f"{local.year} Split {split}",
        starts_at=local_start.astimezone(UTC),
        ends_at=local_end.astimezone(UTC),
        alias=alias,
    )


def circle_match_season_key(starts_at: datetime) -> str:
    return circle_match_season_for(starts_at).key


def circle_match_season_status(*, starts_at: datetime, as_of: datetime) -> CircleMatchSeasonStatus:
    """Classify a season relative to the calendar half containing ``as_of``."""

    season = circle_match_season_for(starts_at)
    current = circle_match_season_for(as_of)
    if season.key == current.key:
        return CircleMatchSeasonStatus.CURRENT
    if season.starts_at < current.starts_at:
        return CircleMatchSeasonStatus.HISTORICAL
    return CircleMatchSeasonStatus.FUTURE
