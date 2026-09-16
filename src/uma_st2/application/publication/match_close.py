"""Provider-independent native Match betting-close final-odds projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import combinations
from typing import Final

from uma_st2.domain.betting import (
    APPLIED_ODDS_QUANTUM,
    PROVISIONAL_ODDS_QUANTUM,
    WEIGHTED_ODDS_RULE_VERSION,
    BetType,
)
from uma_st2.domain.match import MatchGrade
from uma_st2.shared import normalize_utc_datetime

from .intents import PublicationIntent, publication_payload_fingerprint
from .match import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_PUBLICATION_SOURCE_KIND,
    MatchPublicationDestination,
)

MATCH_BETTING_CLOSE_PAYLOAD_SCHEMA_VERSION: Final = 1
MATCH_BETTING_CLOSED_EVENT_TYPE: Final = "room_match_betting_closed"
MATCH_BETTING_CLOSE_PUBLICATION_TYPE: Final = "match_betting_closed"

_BET_TYPE_CARDINALITY: Final = {
    BetType.WIN: 1,
    BetType.QUINELLA: 2,
    BetType.TRIO: 3,
}


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
class MatchBettingCloseSelection:
    """One complete public selection and its one-place final applied odds."""

    bet_type: BetType
    entry_numbers: tuple[int, ...]
    confirmed_odds: Decimal

    def __post_init__(self) -> None:
        bet_type = BetType(self.bet_type)
        entry_numbers = tuple(self.entry_numbers)
        if len(entry_numbers) != _BET_TYPE_CARDINALITY[bet_type]:
            raise ValueError("Betting-close selection cardinality is invalid.")
        if any(_positive_int(value, field_name="entry_number") != value for value in entry_numbers):
            raise ValueError("Betting-close Entry numbers are invalid.")
        canonical = tuple(sorted(entry_numbers))
        if len(set(canonical)) != len(canonical):
            raise ValueError("Betting-close selection contains duplicate Entry numbers.")
        if not isinstance(self.confirmed_odds, Decimal) or not self.confirmed_odds.is_finite():
            raise ValueError("confirmed_odds must be a finite Decimal.")
        if self.confirmed_odds <= 0 or self.confirmed_odds.as_tuple().exponent != -1:
            raise ValueError("confirmed_odds must be a positive one-decimal value.")
        object.__setattr__(self, "bet_type", bet_type)
        object.__setattr__(self, "entry_numbers", canonical)

    def to_payload(self) -> dict[str, object]:
        return {
            "entry_numbers": list(self.entry_numbers),
            "confirmed_odds": format(self.confirmed_odds, ".1f"),
        }


@dataclass(frozen=True, slots=True)
class MatchBettingClosePublicationSource:
    """Complete locked pool projection captured by one successful close command."""

    destination: MatchPublicationDestination
    match_id: int
    match_name: str
    grade: MatchGrade
    scheduled_at: datetime
    closed_at: datetime
    selections: tuple[MatchBettingCloseSelection, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.destination, MatchPublicationDestination):
            raise ValueError("destination must be MatchPublicationDestination.")
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
        object.__setattr__(
            self,
            "closed_at",
            normalize_utc_datetime(self.closed_at, field_name="closed_at"),
        )
        selections = tuple(
            sorted(
                self.selections,
                key=lambda item: (tuple(BetType).index(item.bet_type), item.entry_numbers),
            )
        )
        if not selections:
            raise ValueError("Betting-close publication must contain selections.")

        win_numbers = tuple(item.entry_numbers[0] for item in selections if item.bet_type is BetType.WIN)
        if win_numbers != tuple(range(1, len(win_numbers) + 1)):
            raise ValueError("Betting-close Win selections must define contiguous Entry numbers.")
        expected = tuple(
            (bet_type, selection)
            for bet_type in BetType
            for selection in combinations(win_numbers, _BET_TYPE_CARDINALITY[bet_type])
        )
        actual = tuple((item.bet_type, item.entry_numbers) for item in selections)
        if actual != expected:
            raise ValueError("Betting-close selections must contain every available combination exactly once.")
        object.__setattr__(self, "selections", selections)

    @property
    def field_multiplier(self) -> Decimal:
        field_size = sum(item.bet_type is BetType.WIN for item in self.selections)
        return Decimal("0.5") if field_size < 9 else Decimal("1.0")

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
            "schema_version": MATCH_BETTING_CLOSE_PAYLOAD_SCHEMA_VERSION,
            "publication_type": MATCH_BETTING_CLOSE_PUBLICATION_TYPE,
            "match": {
                "id": self.match_id,
                "name": self.match_name,
                "grade": self.grade.value,
                "scheduled_at": self.scheduled_at.isoformat(),
                "closed_at": self.closed_at.isoformat(),
            },
            "odds": {
                "rule_version": WEIGHTED_ODDS_RULE_VERSION,
                "provisional_precision": format(PROVISIONAL_ODDS_QUANTUM, ".4f"),
                "applied_precision": format(APPLIED_ODDS_QUANTUM, ".1f"),
                "field_multiplier": format(self.field_multiplier, ".1f"),
                "markets": markets,
            },
        }


def build_match_betting_close_publication_intent(
    source: MatchBettingClosePublicationSource,
) -> PublicationIntent:
    """Build one durable logical betting-close publication without provider I/O."""

    if not isinstance(source, MatchBettingClosePublicationSource):
        raise ValueError("source must be MatchBettingClosePublicationSource.")
    payload = source.to_payload()
    return PublicationIntent(
        guild_id=source.destination.guild_id,
        destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=MATCH_BETTING_CLOSED_EVENT_TYPE,
        event_key=f"match:{source.match_id}:betting-close:v1",
        source_kind=MATCH_PUBLICATION_SOURCE_KIND,
        source_id=source.match_id,
        target_channel_id=source.destination.snapshotted_channel_id,
        payload_json=payload,
        payload_fingerprint=publication_payload_fingerprint(payload),
        status=source.destination.initial_status,
    )
