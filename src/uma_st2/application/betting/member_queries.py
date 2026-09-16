"""Read-only member projections for native Match Bet placement."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.betting import (
    BetStatus,
    BetType,
    calculate_bet_stake_cap,
    canonicalize_selections,
    validate_new_bet_stake,
)
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSeason,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)
from uma_st2.shared import normalize_utc_datetime


class MatchMemberBettingQueryError(ValueError):
    """Base error for rejected member Bet target projections."""


class MatchMemberBettingInvalidSourceError(MatchMemberBettingQueryError):
    """Stored member Bet target facts are malformed."""


class MatchRaceDetailUnavailableError(MatchMemberBettingQueryError):
    """The selected Match is no longer eligible for public race detail."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _normalized_string(value: str, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must be a non-empty string of at most {max_length} characters.")
    return normalized


def _normalized_optional_string(value: str | None, *, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or null.")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters.")
    return normalized


@dataclass(frozen=True, slots=True)
class MatchBetTargetChoice:
    """One bounded native betting-open Match autocomplete row."""

    match_id: int
    match_name: str
    grade: MatchGrade
    scheduled_at: datetime
    entry_count: int

    def __post_init__(self) -> None:
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
        _require_positive_int(self.entry_count, field_name="entry_count")


@dataclass(frozen=True, slots=True)
class MatchBetInputPointState:
    """Current actor-owned Point context shown while entering a Bet amount."""

    balance: int
    maximum_stake: int

    def __post_init__(self) -> None:
        if isinstance(self.balance, bool) or not isinstance(self.balance, int) or self.balance < 0:
            raise ValueError("balance must be a non-negative integer.")
        _require_positive_int(self.maximum_stake, field_name="maximum_stake")
        if self.maximum_stake != calculate_bet_stake_cap(self.balance):
            raise ValueError("maximum_stake does not match the current balance.")


@dataclass(frozen=True, slots=True)
class MatchRaceListItem:
    """One native scheduled or betting-open Match shown by ``/match races``."""

    match_id: int
    match_name: str
    grade: MatchGrade
    scheduled_at: datetime
    status: MatchStatus
    entry_count: int

    def __post_init__(self) -> None:
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
        object.__setattr__(self, "status", MatchStatus(self.status))
        if self.status not in {MatchStatus.SCHEDULED, MatchStatus.BETTING_OPEN}:
            raise ValueError("Race list items must be scheduled or betting_open.")
        if isinstance(self.entry_count, bool) or not isinstance(self.entry_count, int) or self.entry_count < 0:
            raise ValueError("entry_count must be a non-negative integer.")


@dataclass(frozen=True, slots=True)
class MatchRaceDetailCourse:
    """Public-safe master course projection for one current Match."""

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


@dataclass(frozen=True, slots=True)
class MatchRaceDetailCondition:
    """Complete public environment projection for one current Match."""

    season: MatchSeason
    weather: MatchWeather
    time_of_day: MatchTimeOfDay
    track_condition: MatchTrackCondition

    def __post_init__(self) -> None:
        object.__setattr__(self, "season", MatchSeason(self.season))
        object.__setattr__(self, "weather", MatchWeather(self.weather))
        object.__setattr__(self, "time_of_day", MatchTimeOfDay(self.time_of_day))
        object.__setattr__(self, "track_condition", MatchTrackCondition(self.track_condition))


@dataclass(frozen=True, slots=True)
class MatchRaceDetailEntry:
    """One public-safe roster row ordered by canonical Entry number."""

    entry_number: int
    game_account_name: str
    umamusume_name: str
    affiliation: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.entry_number, field_name="entry_number")
        object.__setattr__(
            self,
            "game_account_name",
            _normalized_string(
                self.game_account_name,
                field_name="game_account_name",
                max_length=100,
            ),
        )
        object.__setattr__(
            self,
            "umamusume_name",
            _normalized_string(
                self.umamusume_name,
                field_name="umamusume_name",
                max_length=100,
            ),
        )
        object.__setattr__(
            self,
            "affiliation",
            _normalized_optional_string(
                self.affiliation,
                field_name="affiliation",
                max_length=100,
            ),
        )


@dataclass(frozen=True, slots=True)
class MatchRaceDetail:
    """Complete detached public detail for one current native Match."""

    match_id: int
    match_name: str
    description: str | None
    grade: MatchGrade
    scheduled_at: datetime
    status: MatchStatus
    course: MatchRaceDetailCourse
    condition: MatchRaceDetailCondition
    entries: tuple[MatchRaceDetailEntry, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(
            self,
            "description",
            _normalized_optional_string(
                self.description,
                field_name="description",
                max_length=4000,
            ),
        )
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        object.__setattr__(self, "status", MatchStatus(self.status))
        if self.status not in {MatchStatus.SCHEDULED, MatchStatus.BETTING_OPEN}:
            raise ValueError("Race detail must be scheduled or betting_open.")
        if not isinstance(self.course, MatchRaceDetailCourse):
            raise ValueError("course must be MatchRaceDetailCourse.")
        if not isinstance(self.condition, MatchRaceDetailCondition):
            raise ValueError("condition must be MatchRaceDetailCondition.")
        entries = tuple(sorted(self.entries, key=lambda entry: entry.entry_number))
        if any(not isinstance(entry, MatchRaceDetailEntry) for entry in entries):
            raise ValueError("entries must contain MatchRaceDetailEntry values.")
        if entries and tuple(entry.entry_number for entry in entries) != tuple(range(1, len(entries) + 1)):
            raise ValueError("Race detail Entry numbers must be contiguous from 1.")
        if self.status == MatchStatus.BETTING_OPEN and not entries:
            raise ValueError("A betting-open race detail must contain Entries.")
        object.__setattr__(self, "entries", entries)


@dataclass(frozen=True, slots=True)
class ActiveMatchBetChoice:
    """One actor-owned active Bet available for replacement autocomplete."""

    bet_id: int
    match_id: int
    match_name: str
    scheduled_at: datetime
    bet_type: BetType
    entry_numbers: tuple[int, ...]
    selection_fingerprint: str
    amount: int

    def __post_init__(self) -> None:
        _require_positive_int(self.bet_id, field_name="bet_id")
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        object.__setattr__(self, "bet_type", BetType(self.bet_type))
        object.__setattr__(
            self,
            "entry_numbers",
            canonicalize_selections(self.bet_type, self.entry_numbers),
        )
        object.__setattr__(
            self,
            "selection_fingerprint",
            _normalized_string(
                self.selection_fingerprint,
                field_name="selection_fingerprint",
                max_length=64,
            ),
        )
        validate_new_bet_stake(self.amount)


@dataclass(frozen=True, slots=True)
class MemberMatchBetHistoryItem:
    """One bounded Persona-owned native Match Bet history row."""

    bet_id: int
    match_name: str
    scheduled_at: datetime
    match_status: MatchStatus
    bet_type: BetType
    entry_numbers: tuple[int, ...]
    amount: int
    bet_status: BetStatus
    created_at: datetime

    def __post_init__(self) -> None:
        _require_positive_int(self.bet_id, field_name="bet_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        object.__setattr__(self, "match_status", MatchStatus(self.match_status))
        object.__setattr__(self, "bet_type", BetType(self.bet_type))
        object.__setattr__(
            self,
            "entry_numbers",
            canonicalize_selections(self.bet_type, self.entry_numbers),
        )
        _require_positive_int(self.amount, field_name="amount")
        object.__setattr__(self, "bet_status", BetStatus(self.bet_status))
        object.__setattr__(
            self,
            "created_at",
            normalize_utc_datetime(self.created_at, field_name="created_at"),
        )


class MatchMemberBettingQueryRepository(Protocol):
    """Read-only persistence operations for placement target selection."""

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchBetTargetChoice, ...]: ...

    def list_races(self, *, limit: int) -> tuple[MatchRaceListItem, ...]: ...

    def search_races(self, *, search: str, limit: int) -> tuple[MatchRaceListItem, ...]: ...

    def get_race_detail(self, *, match_id: int) -> MatchRaceDetail | None: ...

    def get_current_balance(self, *, actor_discord_user_id: str) -> int | None: ...

    def search_active_bets(
        self,
        *,
        actor_discord_user_id: str,
        search: str,
        limit: int,
    ) -> tuple[ActiveMatchBetChoice, ...]: ...

    def list_personal_bets(
        self,
        *,
        actor_discord_user_id: str,
        limit: int,
    ) -> tuple[MemberMatchBetHistoryItem, ...]: ...


class MatchMemberBettingQueryUnitOfWork(UnitOfWork, Protocol):
    """Read-only UoW exposing member placement target projections."""

    @property
    def match_member_betting_queries(self) -> MatchMemberBettingQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchMemberBettingQueries:
    """Application entry point for bounded Match lists and betting-open choices."""

    query_runner: QueryRunner[MatchMemberBettingQueryUnitOfWork]

    def list_races(self, *, limit: int = 10) -> tuple[MatchRaceListItem, ...]:
        """Return current native scheduled and betting-open Matches."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be between 1 and 10.")

        def query(unit_of_work: MatchMemberBettingQueryUnitOfWork) -> tuple[MatchRaceListItem, ...]:
            try:
                return unit_of_work.match_member_betting_queries.list_races(limit=limit)
            except (TypeError, ValueError) as exc:
                raise MatchMemberBettingInvalidSourceError("Stored Match race list is malformed.") from exc

        return self.query_runner.run(query)

    def search_races(self, *, search: str, limit: int = 25) -> tuple[MatchRaceListItem, ...]:
        """Return current native Match choices for optional race detail."""

        if not isinstance(search, str):
            raise ValueError("search must be a string.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25.")
        normalized = search.strip()
        if len(normalized) > 200:
            raise ValueError("search must be at most 200 characters.")

        def query(unit_of_work: MatchMemberBettingQueryUnitOfWork) -> tuple[MatchRaceListItem, ...]:
            try:
                return unit_of_work.match_member_betting_queries.search_races(
                    search=normalized,
                    limit=limit,
                )
            except (TypeError, ValueError) as exc:
                raise MatchMemberBettingInvalidSourceError("Stored Match race choices are malformed.") from exc

        return self.query_runner.run(query)

    def get_race_detail(self, *, match_id: int) -> MatchRaceDetail:
        """Return a freshly revalidated current public Match detail."""

        _require_positive_int(match_id, field_name="match_id")

        def query(unit_of_work: MatchMemberBettingQueryUnitOfWork) -> MatchRaceDetail:
            try:
                detail = unit_of_work.match_member_betting_queries.get_race_detail(match_id=match_id)
                if detail is not None and not isinstance(detail, MatchRaceDetail):
                    raise ValueError("Race detail repository returned an invalid projection.")
            except (TypeError, ValueError) as exc:
                raise MatchMemberBettingInvalidSourceError("Stored Match race detail is malformed.") from exc
            if detail is None:
                raise MatchRaceDetailUnavailableError("Match race detail is no longer available.")
            return detail

        return self.query_runner.run(query)

    def search_targets(self, *, search: str, limit: int = 25) -> tuple[MatchBetTargetChoice, ...]:
        if not isinstance(search, str):
            raise ValueError("search must be a string.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25.")
        normalized = search.strip()

        def query(unit_of_work: MatchMemberBettingQueryUnitOfWork) -> tuple[MatchBetTargetChoice, ...]:
            try:
                return unit_of_work.match_member_betting_queries.search_targets(
                    search=normalized,
                    limit=limit,
                )
            except (TypeError, ValueError) as exc:
                raise MatchMemberBettingInvalidSourceError("Stored Match Bet target list is malformed.") from exc

        return self.query_runner.run(query)

    def get_input_point_state(
        self,
        *,
        actor_discord_user_id: str,
    ) -> MatchBetInputPointState | None:
        """Return fresh read-only Point context for the Bet amount input."""

        actor_id = _normalized_string(
            actor_discord_user_id,
            field_name="actor_discord_user_id",
            max_length=32,
        )

        def query(unit_of_work: MatchMemberBettingQueryUnitOfWork) -> MatchBetInputPointState | None:
            try:
                balance = unit_of_work.match_member_betting_queries.get_current_balance(
                    actor_discord_user_id=actor_id,
                )
                if balance is None:
                    return None
                return MatchBetInputPointState(
                    balance=balance,
                    maximum_stake=calculate_bet_stake_cap(balance),
                )
            except (TypeError, ValueError) as exc:
                raise MatchMemberBettingInvalidSourceError("Stored Match Bet Point state is malformed.") from exc

        return self.query_runner.run(query)

    def search_active_bets(
        self,
        *,
        actor_discord_user_id: str,
        search: str,
        limit: int = 25,
    ) -> tuple[ActiveMatchBetChoice, ...]:
        actor_id = _normalized_string(
            actor_discord_user_id,
            field_name="actor_discord_user_id",
            max_length=32,
        )
        if not isinstance(search, str):
            raise ValueError("search must be a string.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25.")
        normalized = search.strip()

        def query(unit_of_work: MatchMemberBettingQueryUnitOfWork) -> tuple[ActiveMatchBetChoice, ...]:
            try:
                return unit_of_work.match_member_betting_queries.search_active_bets(
                    actor_discord_user_id=actor_id,
                    search=normalized,
                    limit=limit,
                )
            except (TypeError, ValueError) as exc:
                raise MatchMemberBettingInvalidSourceError("Stored active Match Bet list is malformed.") from exc

        return self.query_runner.run(query)

    def list_personal_bets(
        self,
        *,
        actor_discord_user_id: str,
        limit: int = 10,
    ) -> tuple[MemberMatchBetHistoryItem, ...]:
        actor_id = _normalized_string(
            actor_discord_user_id,
            field_name="actor_discord_user_id",
            max_length=32,
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be between 1 and 10.")

        def query(unit_of_work: MatchMemberBettingQueryUnitOfWork) -> tuple[MemberMatchBetHistoryItem, ...]:
            try:
                return unit_of_work.match_member_betting_queries.list_personal_bets(
                    actor_discord_user_id=actor_id,
                    limit=limit,
                )
            except (TypeError, ValueError) as exc:
                raise MatchMemberBettingInvalidSourceError("Stored personal Match Bet list is malformed.") from exc

        return self.query_runner.run(query)
