from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from umacircle_bot.domain.errors import (
    InvalidRaceTransitionError,
    MatchRaceError,
    RaceMutationConflictError,
)


class MatchRaceStatus(StrEnum):
    SETUP = "setup"
    BETTING_OPEN = "betting_open"
    BETTING_CLOSED = "betting_closed"
    RESULT_REVIEW = "result_review"
    RESULT_CONFIRMED = "result_confirmed"
    SETTLED = "settled"
    VOIDED = "voided"


ROOM_MATCH_RACE_STATUSES = frozenset(MatchRaceStatus)


@dataclass(frozen=True, slots=True)
class MatchRaceSnapshot:
    race_kind: str
    status: MatchRaceStatus
    starts_at: datetime | None
    has_condition: bool
    entry_count: int
    has_results: bool


@dataclass(frozen=True, slots=True)
class BettingEligibilityDecision:
    allowed: bool
    code: str | None = None


def normalize_match_race_status(value: str | MatchRaceStatus) -> MatchRaceStatus:
    if isinstance(value, MatchRaceStatus):
        return value
    if not isinstance(value, str):
        raise MatchRaceError("race status must be text")
    try:
        return MatchRaceStatus(value)
    except ValueError as exc:
        raise MatchRaceError("unsupported room-match race status") from exc


def validate_setup_mutation(*, status: str | MatchRaceStatus, has_results: bool) -> None:
    normalized_status = normalize_match_race_status(status)
    if has_results:
        raise RaceMutationConflictError("race results already exist")
    if normalized_status is not MatchRaceStatus.SETUP:
        raise RaceMutationConflictError("race setup can only be changed before betting opens")


def validate_betting_open(
    snapshot: MatchRaceSnapshot,
) -> MatchRaceStatus:
    if snapshot.status is not MatchRaceStatus.SETUP:
        raise InvalidRaceTransitionError("betting can only open from setup")
    if snapshot.has_results:
        raise RaceMutationConflictError("race results already exist")
    if not snapshot.has_condition:
        raise RaceMutationConflictError("race condition snapshot is required")
    if snapshot.entry_count < 1:
        raise RaceMutationConflictError("at least one race entry is required")
    return MatchRaceStatus.BETTING_OPEN


def validate_betting_close(
    *,
    status: str | MatchRaceStatus,
    betting_opened_at: datetime | None,
    as_of: datetime,
) -> MatchRaceStatus:
    normalized_status = normalize_match_race_status(status)
    if normalized_status is not MatchRaceStatus.BETTING_OPEN:
        raise InvalidRaceTransitionError("betting can only close while betting is open")
    if betting_opened_at is None:
        raise RaceMutationConflictError("betting open timestamp is missing")
    if _normalize_utc_datetime(as_of) < _normalize_utc_datetime(betting_opened_at):
        raise RaceMutationConflictError("betting close time cannot precede betting open time")
    return MatchRaceStatus.BETTING_CLOSED


def evaluate_new_bet_eligibility(
    snapshot: MatchRaceSnapshot,
) -> BettingEligibilityDecision:
    if snapshot.race_kind != "room_match":
        return BettingEligibilityDecision(False, "wrong_race_kind")
    if snapshot.status is not MatchRaceStatus.BETTING_OPEN:
        return BettingEligibilityDecision(False, "betting_not_open")
    if snapshot.has_results:
        return BettingEligibilityDecision(False, "results_exist")
    if snapshot.entry_count < 1:
        return BettingEligibilityDecision(False, "no_entries")
    return BettingEligibilityDecision(True)


def _normalize_utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MatchRaceError("race time must be timezone-aware")
    return value.astimezone(UTC)
