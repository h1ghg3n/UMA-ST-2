"""Provider-independent periodic native Match provisional-odds projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import combinations
from typing import Final

from uma_st2.domain.betting import PROVISIONAL_ODDS_QUANTUM, WEIGHTED_ODDS_RULE_VERSION, BetType
from uma_st2.domain.match import MatchGrade
from uma_st2.shared import normalize_utc_datetime

from .intents import PublicationIntent, publication_payload_fingerprint
from .match import MATCH_ANNOUNCEMENT_DESTINATION_KIND, MatchPublicationDestination

MATCH_ODDS_REFRESH_PAYLOAD_SCHEMA_VERSION: Final = 1
MATCH_ODDS_REFRESH_EVENT_TYPE: Final = "room_match_odds_refreshed"
MATCH_ODDS_REFRESH_SOURCE_KIND: Final = "match_odds_refresh"
MATCH_ODDS_REFRESH_PUBLICATION_TYPE: Final = "match_odds_refresh"
MATCH_ODDS_NORMAL_INTERVAL: Final = timedelta(minutes=10)
MATCH_ODDS_LIVE_INTERVAL: Final = timedelta(minutes=1)

_BET_TYPE_CARDINALITY: Final = {
    BetType.WIN: 1,
    BetType.QUINELLA: 2,
    BetType.TRIO: 3,
}


class MatchOddsRefreshMode(StrEnum):
    """Guild-scoped operational coverage mode, independent of Match lifecycle."""

    NORMAL = "normal"
    LIVE = "live"

    @property
    def interval(self) -> timedelta:
        return MATCH_ODDS_NORMAL_INTERVAL if self is MatchOddsRefreshMode.NORMAL else MATCH_ODDS_LIVE_INTERVAL


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _bounded_text(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must contain 1 to {max_length} characters.")
    return normalized


@dataclass(frozen=True, slots=True)
class MatchOddsRefreshSelection:
    """One complete public selection and current four-place provisional odds."""

    bet_type: BetType
    entry_numbers: tuple[int, ...]
    provisional_odds: Decimal

    def __post_init__(self) -> None:
        bet_type = BetType(self.bet_type)
        entry_numbers = tuple(self.entry_numbers)
        expected_count = _BET_TYPE_CARDINALITY[bet_type]
        if len(entry_numbers) != expected_count:
            raise ValueError("Periodic odds selection cardinality is invalid.")
        if any(_positive_int(value, field_name="entry_number") != value for value in entry_numbers):
            raise ValueError("Periodic odds Entry numbers are invalid.")
        canonical = tuple(sorted(entry_numbers))
        if len(set(canonical)) != len(canonical):
            raise ValueError("Periodic odds selection contains duplicate Entry numbers.")
        if not isinstance(self.provisional_odds, Decimal) or not self.provisional_odds.is_finite():
            raise ValueError("provisional_odds must be a finite Decimal.")
        if self.provisional_odds <= 0 or self.provisional_odds.as_tuple().exponent != -4:
            raise ValueError("provisional_odds must be a positive four-decimal value.")
        object.__setattr__(self, "bet_type", bet_type)
        object.__setattr__(self, "entry_numbers", canonical)

    def to_payload(self) -> dict[str, object]:
        return {
            "entry_numbers": list(self.entry_numbers),
            "provisional_odds": format(self.provisional_odds, ".4f"),
        }


@dataclass(frozen=True, slots=True)
class MatchOddsRefreshMatch:
    """One open Match section in a guild-wide refresh projection."""

    match_id: int
    match_name: str
    grade: MatchGrade
    scheduled_at: datetime
    selections: tuple[MatchOddsRefreshSelection, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _bounded_text(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        selections = tuple(
            sorted(
                self.selections,
                key=lambda item: (tuple(BetType).index(item.bet_type), item.entry_numbers),
            )
        )
        if not selections:
            raise ValueError("An open Match odds projection must contain selections.")

        win_numbers = tuple(item.entry_numbers[0] for item in selections if item.bet_type is BetType.WIN)
        if win_numbers != tuple(range(1, len(win_numbers) + 1)):
            raise ValueError("Periodic odds Win selections must define contiguous Entry numbers.")
        expected: list[tuple[BetType, tuple[int, ...]]] = []
        for bet_type in BetType:
            cardinality = _BET_TYPE_CARDINALITY[bet_type]
            expected.extend((bet_type, selection) for selection in combinations(win_numbers, cardinality))
        actual = [(item.bet_type, item.entry_numbers) for item in selections]
        if actual != expected:
            raise ValueError("Periodic odds selections must contain every available combination exactly once.")
        object.__setattr__(self, "selections", selections)

    def to_payload(self) -> dict[str, object]:
        markets: list[dict[str, object]] = []
        for bet_type in BetType:
            selections = tuple(item for item in self.selections if item.bet_type is bet_type)
            if not selections:
                continue
            markets.append(
                {
                    "bet_type": bet_type.value,
                    "selections": [selection.to_payload() for selection in selections],
                }
            )
        return {
            "match_id": self.match_id,
            "name": self.match_name,
            "grade": self.grade.value,
            "scheduled_at": self.scheduled_at.isoformat(),
            "markets": markets,
        }


@dataclass(frozen=True, slots=True)
class MatchOddsRefreshPublicationSource:
    """Complete current authority for one immutable guild-wide refresh intent."""

    destination: MatchPublicationDestination
    mode: MatchOddsRefreshMode
    sequence: int
    generated_at: datetime
    matches: tuple[MatchOddsRefreshMatch, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.destination, MatchPublicationDestination):
            raise ValueError("destination must be MatchPublicationDestination.")
        object.__setattr__(self, "mode", MatchOddsRefreshMode(self.mode))
        _positive_int(self.sequence, field_name="sequence")
        object.__setattr__(
            self,
            "generated_at",
            normalize_utc_datetime(self.generated_at, field_name="generated_at"),
        )
        matches = tuple(sorted(self.matches, key=lambda item: (item.scheduled_at, item.match_id)))
        if not matches or len({item.match_id for item in matches}) != len(matches):
            raise ValueError("Periodic odds refresh requires unique open Matches.")
        object.__setattr__(self, "matches", matches)

    def projection_payload(self) -> dict[str, object]:
        """Return meaningful content excluding generation time and delivery identity."""

        return {
            "mode": self.mode.value,
            "cadence_seconds": int(self.mode.interval.total_seconds()),
            "rule_version": WEIGHTED_ODDS_RULE_VERSION,
            "provisional_precision": format(PROVISIONAL_ODDS_QUANTUM, ".4f"),
            "matches": [match.to_payload() for match in self.matches],
        }

    @property
    def projection_fingerprint(self) -> str:
        return publication_payload_fingerprint(self.projection_payload())

    def to_payload(self) -> dict[str, object]:
        projection = self.projection_payload()
        return {
            "schema_version": MATCH_ODDS_REFRESH_PAYLOAD_SCHEMA_VERSION,
            "publication_type": MATCH_ODDS_REFRESH_PUBLICATION_TYPE,
            "coverage": {
                "mode": projection["mode"],
                "cadence_seconds": projection["cadence_seconds"],
                "generated_at": self.generated_at.isoformat(),
            },
            "odds": {
                "rule_version": projection["rule_version"],
                "provisional_precision": projection["provisional_precision"],
            },
            "matches": projection["matches"],
        }


def build_match_odds_refresh_publication_intent(
    source: MatchOddsRefreshPublicationSource,
) -> PublicationIntent:
    """Build one durable periodic refresh intent without provider I/O."""

    if not isinstance(source, MatchOddsRefreshPublicationSource):
        raise ValueError("source must be MatchOddsRefreshPublicationSource.")
    payload = source.to_payload()
    return PublicationIntent(
        guild_id=source.destination.guild_id,
        destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=MATCH_ODDS_REFRESH_EVENT_TYPE,
        event_key=f"match-odds-refresh:{source.sequence}:v1",
        source_kind=MATCH_ODDS_REFRESH_SOURCE_KIND,
        source_id=source.sequence,
        target_channel_id=source.destination.snapshotted_channel_id,
        payload_json=payload,
        payload_fingerprint=publication_payload_fingerprint(payload),
        status=source.destination.initial_status,
    )
