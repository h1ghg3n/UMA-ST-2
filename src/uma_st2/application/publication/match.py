"""Provider-independent native Match betting-open publication projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Final

from uma_st2.domain.betting import (
    PROVISIONAL_ODDS_QUANTUM,
    WEIGHTED_ODDS_RULE_VERSION,
    BetPoolStake,
    BetType,
    calculate_zero_pool_market_odds,
)
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchRatingDisposition,
    MatchSeason,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)
from uma_st2.domain.publication import PublicationStatus
from uma_st2.domain.rating import normalize_rating_storage
from uma_st2.shared import normalize_utc_datetime

from .intents import PublicationIntent, publication_payload_fingerprint

MATCH_PUBLICATION_PAYLOAD_SCHEMA_VERSION: Final = 1
MATCH_RESULT_PUBLICATION_PAYLOAD_SCHEMA_VERSION: Final = 2
MATCH_ANNOUNCEMENT_DESTINATION_KIND: Final = "match_announcement"
MATCH_PUBLICATION_SOURCE_KIND: Final = "match"
MATCH_BETTING_OPENED_EVENT_TYPE: Final = "match_betting_opened"
MATCH_RESULT_CONFIRMED_EVENT_TYPE: Final = "room_match_result_confirmed"
MATCH_BETS_REFUNDED_EVENT_TYPE: Final = "room_match_bets_refunded"
MATCH_SETTLEMENT_VOIDED_EVENT_TYPE: Final = "room_match_settlement_voided"

_BET_TYPE_ORDER: Final = {
    BetType.WIN: 0,
    BetType.QUINELLA: 1,
    BetType.TRIO: 2,
}


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _normalized_string(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    normalized = value.strip()
    if optional and not normalized:
        return None
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must be a non-empty string of at most {max_length} characters.")
    return normalized


@dataclass(frozen=True, slots=True)
class MatchPublicationDestination:
    """Current guild setting snapshotted by the final opening command."""

    guild_id: str
    announcements_enabled: bool
    target_channel_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "guild_id",
            _normalized_string(self.guild_id, field_name="guild_id", max_length=32),
        )
        if not isinstance(self.announcements_enabled, bool):
            raise ValueError("announcements_enabled must be a boolean.")
        object.__setattr__(
            self,
            "target_channel_id",
            _normalized_string(
                self.target_channel_id,
                field_name="target_channel_id",
                max_length=32,
                optional=True,
            ),
        )

    @property
    def initial_status(self) -> PublicationStatus:
        if not self.announcements_enabled:
            return PublicationStatus.SUPPRESSED
        if self.target_channel_id is None:
            return PublicationStatus.AWAITING_CHANNEL
        return PublicationStatus.READY

    @property
    def snapshotted_channel_id(self) -> str | None:
        return self.target_channel_id if self.initial_status == PublicationStatus.READY else None

    def to_payload(self) -> dict[str, object]:
        return {
            "guild_id": self.guild_id,
            "announcements_enabled": self.announcements_enabled,
            "target_channel_id": self.target_channel_id,
            "initial_status": self.initial_status.value,
        }


@dataclass(frozen=True, slots=True)
class MatchOpeningCourse:
    """Canonical course context copied into the opening projection."""

    course_id: int
    stadium_id: int
    stadium_name: str
    surface: MatchSurface
    distance: int
    direction: MatchDirection
    layout: StadiumCourseLayout

    def __post_init__(self) -> None:
        _require_positive_int(self.course_id, field_name="course_id")
        _require_positive_int(self.stadium_id, field_name="stadium_id")
        object.__setattr__(
            self,
            "stadium_name",
            _normalized_string(self.stadium_name, field_name="stadium_name", max_length=100),
        )
        object.__setattr__(self, "surface", MatchSurface(self.surface))
        _require_positive_int(self.distance, field_name="distance")
        object.__setattr__(self, "direction", MatchDirection(self.direction))
        object.__setattr__(self, "layout", StadiumCourseLayout(self.layout))

    def to_payload(self) -> dict[str, object]:
        return {
            "course_id": self.course_id,
            "stadium_id": self.stadium_id,
            "stadium_name": self.stadium_name,
            "surface": self.surface.value,
            "distance": self.distance,
            "direction": self.direction.value,
            "layout": self.layout.value,
        }


@dataclass(frozen=True, slots=True)
class MatchOpeningCondition:
    """Complete canonical Match environment copied into the opening projection."""

    season: MatchSeason
    weather: MatchWeather
    time_of_day: MatchTimeOfDay
    track_condition: MatchTrackCondition

    def __post_init__(self) -> None:
        object.__setattr__(self, "season", MatchSeason(self.season))
        object.__setattr__(self, "weather", MatchWeather(self.weather))
        object.__setattr__(self, "time_of_day", MatchTimeOfDay(self.time_of_day))
        object.__setattr__(self, "track_condition", MatchTrackCondition(self.track_condition))

    def to_payload(self) -> dict[str, str]:
        return {
            "season": self.season.value,
            "weather": self.weather.value,
            "time_of_day": self.time_of_day.value,
            "track_condition": self.track_condition.value,
        }


@dataclass(frozen=True, slots=True)
class MatchOpeningEntry:
    """One configured Entry and safe display snapshots for the opening projection."""

    entry_id: int
    entry_number: int
    game_account_name: str
    horse_name: str
    affiliation: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.entry_id, field_name="entry_id")
        _require_positive_int(self.entry_number, field_name="entry_number")
        object.__setattr__(
            self,
            "game_account_name",
            _normalized_string(self.game_account_name, field_name="game_account_name", max_length=100),
        )
        object.__setattr__(
            self,
            "horse_name",
            _normalized_string(self.horse_name, field_name="horse_name", max_length=100),
        )
        object.__setattr__(
            self,
            "affiliation",
            _normalized_string(
                self.affiliation,
                field_name="affiliation",
                max_length=100,
                optional=True,
            ),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "entry_number": self.entry_number,
            "game_account_name": self.game_account_name,
            "horse_name": self.horse_name,
            "affiliation": self.affiliation,
        }


@dataclass(frozen=True, slots=True)
class MatchOpeningMarket:
    """Availability and uniform zero-pool odds for one opening market."""

    bet_type: BetType
    available: bool
    selection_count: int
    uniform_odds: Decimal | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "bet_type", BetType(self.bet_type))
        if not isinstance(self.available, bool):
            raise ValueError("available must be a boolean.")
        if isinstance(self.selection_count, bool) or not isinstance(self.selection_count, int):
            raise ValueError("selection_count must be an integer.")
        if self.available:
            if self.selection_count <= 0 or not isinstance(self.uniform_odds, Decimal):
                raise ValueError("An available opening market requires selections and Decimal odds.")
            if self.uniform_odds <= 0 or self.uniform_odds.as_tuple().exponent != -4:
                raise ValueError("uniform_odds must be a positive four-decimal value.")
        elif self.selection_count != 0 or self.uniform_odds is not None:
            raise ValueError("An unavailable opening market cannot have selections or odds.")

    def to_payload(self) -> dict[str, object]:
        return {
            "bet_type": self.bet_type.value,
            "available": self.available,
            "selection_count": self.selection_count,
            "uniform_odds": format(self.uniform_odds, ".4f") if self.uniform_odds is not None else None,
        }


def build_zero_pool_opening_markets(field_size: int) -> tuple[MatchOpeningMarket, ...]:
    """Project lossless market-wide opening odds from an empty active Bet pool."""

    available = {market.bet_type: market for market in calculate_zero_pool_market_odds(field_size)}
    markets: list[MatchOpeningMarket] = []
    for bet_type in BetType:
        market = available.get(bet_type)
        if market is None:
            markets.append(MatchOpeningMarket(bet_type, False, 0, None))
            continue
        markets.append(MatchOpeningMarket(bet_type, True, market.selection_count, market.odds))
    return tuple(markets)


@dataclass(frozen=True, slots=True)
class MatchOpeningPublicationSource:
    """Complete authority captured by the betting-open command transaction."""

    destination: MatchPublicationDestination
    match_id: int
    match_name: str
    description: str | None
    grade: MatchGrade
    scheduled_at: datetime
    course: MatchOpeningCourse
    condition: MatchOpeningCondition
    entries: tuple[MatchOpeningEntry, ...] = field(default_factory=tuple)
    markets: tuple[MatchOpeningMarket, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.destination, MatchPublicationDestination):
            raise ValueError("destination must be MatchPublicationDestination.")
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(
            self,
            "description",
            _normalized_string(
                self.description,
                field_name="description",
                max_length=4000,
                optional=True,
            ),
        )
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        if not isinstance(self.course, MatchOpeningCourse):
            raise ValueError("course must be MatchOpeningCourse.")
        if not isinstance(self.condition, MatchOpeningCondition):
            raise ValueError("condition must be MatchOpeningCondition.")
        entries = tuple(sorted(self.entries, key=lambda entry: entry.entry_number))
        if not entries or tuple(entry.entry_number for entry in entries) != tuple(range(1, len(entries) + 1)):
            raise ValueError("Opening publication Entries must be non-empty and contiguous from 1.")
        if len({entry.entry_id for entry in entries}) != len(entries):
            raise ValueError("Opening publication Entry IDs must be unique.")
        object.__setattr__(self, "entries", entries)
        markets = tuple(sorted(self.markets, key=lambda market: _BET_TYPE_ORDER[market.bet_type]))
        if tuple(market.bet_type for market in markets) != tuple(BetType):
            raise ValueError("Opening publication must state availability for every bet type.")
        if markets != build_zero_pool_opening_markets(len(entries)):
            raise ValueError("Opening publication markets do not match the canonical zero-pool projection.")
        object.__setattr__(self, "markets", markets)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_PUBLICATION_PAYLOAD_SCHEMA_VERSION,
            "publication_type": "match_betting_opening",
            "match": {
                "id": self.match_id,
                "name": self.match_name,
                "description": self.description,
                "grade": self.grade.value,
                "scheduled_at": self.scheduled_at.isoformat(),
            },
            "course": self.course.to_payload(),
            "condition": self.condition.to_payload(),
            "entries": [entry.to_payload() for entry in self.entries],
            "odds": {
                "rule_version": WEIGHTED_ODDS_RULE_VERSION,
                "provisional_precision": format(PROVISIONAL_ODDS_QUANTUM, ".4f"),
                "zero_pool": True,
                "markets": [market.to_payload() for market in self.markets],
            },
        }


def build_match_opening_publication_intent(
    source: MatchOpeningPublicationSource,
) -> PublicationIntent:
    """Build one durable logical opening publication without provider I/O."""

    if not isinstance(source, MatchOpeningPublicationSource):
        raise ValueError("source must be MatchOpeningPublicationSource.")
    payload = source.to_payload()
    return PublicationIntent(
        guild_id=source.destination.guild_id,
        destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=MATCH_BETTING_OPENED_EVENT_TYPE,
        event_key=f"match:{source.match_id}:betting-open:v1",
        source_kind=MATCH_PUBLICATION_SOURCE_KIND,
        source_id=source.match_id,
        target_channel_id=source.destination.snapshotted_channel_id,
        payload_json=payload,
        payload_fingerprint=publication_payload_fingerprint(payload),
        status=source.destination.initial_status,
    )


@dataclass(frozen=True, slots=True)
class MatchRefundPublicationSource:
    """Public-safe authority captured by a cancellation that refunded active Bets."""

    destination: MatchPublicationDestination
    match_id: int
    match_name: str
    grade: MatchGrade
    scheduled_at: datetime
    refunded_at: datetime
    reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.destination, MatchPublicationDestination):
            raise ValueError("destination must be MatchPublicationDestination.")
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        object.__setattr__(
            self,
            "refunded_at",
            normalize_utc_datetime(self.refunded_at, field_name="refunded_at"),
        )
        object.__setattr__(
            self,
            "reason",
            _normalized_string(
                self.reason,
                field_name="reason",
                max_length=255,
                optional=True,
            ),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_PUBLICATION_PAYLOAD_SCHEMA_VERSION,
            "publication_type": "match_bet_refund_completed",
            "match": {
                "name": self.match_name,
                "grade": self.grade.value,
                "scheduled_at": self.scheduled_at.isoformat(),
            },
            "refund": {
                "completed_at": self.refunded_at.isoformat(),
                "full_original_stake": True,
                "reason": self.reason,
            },
        }


def build_match_refund_publication_intent(
    source: MatchRefundPublicationSource,
) -> PublicationIntent:
    """Build one logical refund-completion publication without provider I/O."""

    if not isinstance(source, MatchRefundPublicationSource):
        raise ValueError("source must be MatchRefundPublicationSource.")
    payload = source.to_payload()
    return PublicationIntent(
        guild_id=source.destination.guild_id,
        destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=MATCH_BETS_REFUNDED_EVENT_TYPE,
        event_key=f"match:{source.match_id}:bet-refund:v1",
        source_kind=MATCH_PUBLICATION_SOURCE_KIND,
        source_id=source.match_id,
        target_channel_id=source.destination.snapshotted_channel_id,
        payload_json=payload,
        payload_fingerprint=publication_payload_fingerprint(payload),
        status=source.destination.initial_status,
    )


@dataclass(frozen=True, slots=True)
class MatchSettlementVoidedPublicationSource:
    """Public-safe authority captured by one terminal settlement rollback."""

    destination: MatchPublicationDestination
    match_id: int
    match_name: str
    grade: MatchGrade
    scheduled_at: datetime
    rolled_back_at: datetime
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.destination, MatchPublicationDestination):
            raise ValueError("destination must be MatchPublicationDestination.")
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        object.__setattr__(
            self,
            "rolled_back_at",
            normalize_utc_datetime(self.rolled_back_at, field_name="rolled_back_at"),
        )
        object.__setattr__(
            self,
            "reason",
            _normalized_string(self.reason, field_name="reason", max_length=255),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_PUBLICATION_PAYLOAD_SCHEMA_VERSION,
            "publication_type": "match_settlement_voided",
            "match": {
                "name": self.match_name,
                "grade": self.grade.value,
                "scheduled_at": self.scheduled_at.isoformat(),
            },
            "rollback": {
                "completed_at": self.rolled_back_at.isoformat(),
                "reason": self.reason,
                "prior_settlement_voided": True,
                "compensation_completed": True,
            },
        }


def build_match_settlement_voided_publication_intent(
    source: MatchSettlementVoidedPublicationSource,
) -> PublicationIntent:
    """Build one append-only rollback correction without provider I/O."""

    if not isinstance(source, MatchSettlementVoidedPublicationSource):
        raise ValueError("source must be MatchSettlementVoidedPublicationSource.")
    payload = source.to_payload()
    return PublicationIntent(
        guild_id=source.destination.guild_id,
        destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=MATCH_SETTLEMENT_VOIDED_EVENT_TYPE,
        event_key=f"match:{source.match_id}:settlement-voided:v1",
        source_kind=MATCH_PUBLICATION_SOURCE_KIND,
        source_id=source.match_id,
        target_channel_id=source.destination.snapshotted_channel_id,
        payload_json=payload,
        payload_fingerprint=publication_payload_fingerprint(payload),
        status=source.destination.initial_status,
    )


@dataclass(frozen=True, slots=True)
class MatchResultCourse:
    """Public-safe settled Match course snapshot."""

    stadium_name: str
    surface: MatchSurface
    distance: int
    direction: MatchDirection
    layout: StadiumCourseLayout

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "stadium_name",
            _normalized_string(self.stadium_name, field_name="stadium_name", max_length=100),
        )
        object.__setattr__(self, "surface", MatchSurface(self.surface))
        _require_positive_int(self.distance, field_name="distance")
        object.__setattr__(self, "direction", MatchDirection(self.direction))
        object.__setattr__(self, "layout", StadiumCourseLayout(self.layout))

    def to_payload(self) -> dict[str, object]:
        return {
            "stadium_name": self.stadium_name,
            "surface": self.surface.value,
            "distance": self.distance,
            "direction": self.direction.value,
            "layout": self.layout.value,
        }


@dataclass(frozen=True, slots=True)
class MatchResultEntry:
    """One official rank and settlement-time public display snapshot."""

    entry_number: int
    official_rank: int
    player_name: str
    character_name: str
    affiliation: str | None
    rating_before: Decimal
    rating_delta: Decimal
    rating_after: Decimal
    rating_disposition: MatchRatingDisposition = MatchRatingDisposition.RATED
    rating_rank: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.entry_number, field_name="entry_number")
        _require_positive_int(self.official_rank, field_name="official_rank")
        object.__setattr__(
            self,
            "player_name",
            _normalized_string(self.player_name, field_name="player_name", max_length=100),
        )
        object.__setattr__(
            self,
            "character_name",
            _normalized_string(self.character_name, field_name="character_name", max_length=100),
        )
        object.__setattr__(
            self,
            "affiliation",
            _normalized_string(
                self.affiliation,
                field_name="affiliation",
                max_length=100,
                optional=True,
            ),
        )
        before = normalize_rating_storage(self.rating_before)
        delta = normalize_rating_storage(self.rating_delta)
        after = normalize_rating_storage(self.rating_after)
        if before < 0 or after < 0 or before + delta != after:
            raise ValueError("Rating before, delta, and after must form one exact non-negative transition.")
        object.__setattr__(self, "rating_before", before)
        object.__setattr__(self, "rating_delta", delta)
        object.__setattr__(self, "rating_after", after)
        disposition = MatchRatingDisposition(self.rating_disposition)
        rating_rank = self.rating_rank
        if disposition is MatchRatingDisposition.RATED:
            if rating_rank is None:
                rating_rank = self.official_rank
            _require_positive_int(rating_rank, field_name="rating_rank")
        elif rating_rank is not None:
            raise ValueError("A non-rated result cannot have a derived Rating rank.")
        object.__setattr__(self, "rating_disposition", disposition)
        object.__setattr__(self, "rating_rank", rating_rank)

    def to_payload(self) -> dict[str, object]:
        return {
            "entry_number": self.entry_number,
            "official_rank": self.official_rank,
            "player_name": self.player_name,
            "character_name": self.character_name,
            "affiliation": self.affiliation,
            "rating_disposition": self.rating_disposition.value,
            "rating_rank": self.rating_rank,
            "rating": {
                "before": format(self.rating_before, ".4f"),
                "delta": format(self.rating_delta, ".4f"),
                "after": format(self.rating_after, ".4f"),
            },
        }


@dataclass(frozen=True, slots=True)
class MatchResultOdds:
    """One winning public selection and immutable settlement odds."""

    bet_type: BetType
    selection_entry_numbers: tuple[int, ...]
    confirmed_odds: Decimal

    def __post_init__(self) -> None:
        selection = BetPoolStake(
            bet_type=BetType(self.bet_type),
            selection_ids=tuple(self.selection_entry_numbers),
            amount=10,
        )
        object.__setattr__(self, "bet_type", selection.bet_type)
        object.__setattr__(self, "selection_entry_numbers", selection.selection_ids)
        if (
            not isinstance(self.confirmed_odds, Decimal)
            or not self.confirmed_odds.is_finite()
            or self.confirmed_odds <= 0
            or self.confirmed_odds.as_tuple().exponent != -1
        ):
            raise ValueError("confirmed_odds must be a positive one-decimal value.")

    def to_payload(self) -> dict[str, object]:
        return {
            "bet_type": self.bet_type.value,
            "winning_selection": list(self.selection_entry_numbers),
            "confirmed_odds": format(self.confirmed_odds, ".1f"),
        }


@dataclass(frozen=True, slots=True)
class MatchResultPublicationSource:
    """Complete committed authority for one settled-result publication."""

    destination: MatchPublicationDestination
    match_id: int
    match_name: str
    grade: MatchGrade
    scheduled_at: datetime
    course: MatchResultCourse
    condition: MatchOpeningCondition
    entries: tuple[MatchResultEntry, ...]
    odds: tuple[MatchResultOdds, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.destination, MatchPublicationDestination):
            raise ValueError("destination must be MatchPublicationDestination.")
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        if not isinstance(self.course, MatchResultCourse):
            raise ValueError("course must be MatchResultCourse.")
        if not isinstance(self.condition, MatchOpeningCondition):
            raise ValueError("condition must be MatchOpeningCondition.")

        entries = tuple(sorted(self.entries, key=lambda entry: entry.official_rank))
        if not entries or tuple(entry.official_rank for entry in entries) != tuple(range(1, len(entries) + 1)):
            raise ValueError("Result publication ranks must be complete and contiguous from 1.")
        if len({entry.entry_number for entry in entries}) != len(entries):
            raise ValueError("Result publication Entry numbers must be unique.")
        object.__setattr__(self, "entries", entries)

        odds = tuple(sorted(self.odds, key=lambda item: _BET_TYPE_ORDER[item.bet_type]))
        expected_types = tuple(BetType)[: min(len(entries), len(BetType))]
        if tuple(item.bet_type for item in odds) != expected_types:
            raise ValueError("Result publication must preserve every settled odds market exactly once.")
        for selection_count, item in enumerate(odds, start=1):
            expected_numbers = tuple(sorted(entry.entry_number for entry in entries[:selection_count]))
            if item.selection_entry_numbers != expected_numbers:
                raise ValueError("Result publication odds must identify the official winning Entry set.")
        object.__setattr__(self, "odds", odds)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_RESULT_PUBLICATION_PAYLOAD_SCHEMA_VERSION,
            "publication_type": "match_settled_result",
            "match": {
                "name": self.match_name,
                "grade": self.grade.value,
                "scheduled_at": self.scheduled_at.isoformat(),
            },
            "course": self.course.to_payload(),
            "condition": self.condition.to_payload(),
            "results": [entry.to_payload() for entry in self.entries],
            "odds": {"markets": [item.to_payload() for item in self.odds]},
        }


def build_match_result_publication_intent(
    source: MatchResultPublicationSource,
) -> PublicationIntent:
    """Build the one logical public result intent from committed settlement evidence."""

    if not isinstance(source, MatchResultPublicationSource):
        raise ValueError("source must be MatchResultPublicationSource.")
    payload = source.to_payload()
    return PublicationIntent(
        guild_id=source.destination.guild_id,
        destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=MATCH_RESULT_CONFIRMED_EVENT_TYPE,
        event_key=f"match:{source.match_id}:settled-result:v1",
        source_kind=MATCH_PUBLICATION_SOURCE_KIND,
        source_id=source.match_id,
        target_channel_id=source.destination.snapshotted_channel_id,
        payload_json=payload,
        payload_fingerprint=publication_payload_fingerprint(payload),
        status=source.destination.initial_status,
    )
