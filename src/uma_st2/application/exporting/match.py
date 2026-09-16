"""Application-owned Circle Match Season export projection and orchestration."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.betting import BetStatus, BetType, canonicalize_selections
from uma_st2.domain.identity import GameRegion
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchRatingDisposition,
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)
from uma_st2.shared import normalize_utc_datetime

from .artifact import ExportArtifact

MATCH_EXPORT_PROJECTION_VERSION = "match-season-projection/v2"
MATCH_EXPORT_WORKBOOK_SCHEMA_VERSION = "match-season-xlsx/v2"

_KST = ZoneInfo("Asia/Seoul")
_SEASON_KEY = re.compile(r"(?P<year>[0-9]{4})-split-(?P<split>[12])\Z")


class MatchSeasonExportError(ValueError):
    """Base error for an expected Circle Match Season export rejection."""


class MatchSeasonExportUnavailableError(MatchSeasonExportError):
    """The requested derived Match Season has no current canonical Match."""


class MatchSeasonExportInvalidSourceError(MatchSeasonExportError):
    """Stored Match facts cannot form one complete export projection."""


@dataclass(frozen=True, slots=True)
class MatchExportSeason:
    """One KST calendar-half scope represented by UTC half-open bounds."""

    key: str
    name: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class MatchExportSeasonChoice:
    """One actual derived Season exposed by export autocomplete."""

    key: str
    name: str


@dataclass(frozen=True, slots=True)
class MatchExportCourse:
    stadium_name: str
    course_id: int
    surface: MatchSurface
    distance: int
    direction: MatchDirection
    layout: StadiumCourseLayout


@dataclass(frozen=True, slots=True)
class MatchExportCondition:
    season: MatchSeason
    weather: MatchWeather
    time_of_day: MatchTimeOfDay
    track_condition: MatchTrackCondition


@dataclass(frozen=True, slots=True)
class MatchExportEntry:
    id: int
    entry_number: int
    game_account_id: int
    game_account_name: str
    game_region: GameRegion
    owner_at_event_display_name: str
    affiliation_at_event: str | None
    character_name: str
    variant_name: str | None
    running_style: str | None
    training_grade: str | None
    rank: int | None
    rating_disposition: MatchRatingDisposition | None
    popularity_rank: int | None
    margin: str | None


@dataclass(frozen=True, slots=True)
class MatchExportBet:
    id: int
    persona_display_name: str
    bet_type: BetType
    selection_entry_ids: tuple[int, ...]
    amount: int
    status: BetStatus
    created_at: datetime
    updated_at: datetime
    applied_odds: Decimal | None = None
    payout_amount: int | None = None
    payout_group_bet_ids: tuple[int, ...] = field(default_factory=tuple)
    payout_reversal_amount: int | None = None
    refund_amount: int | None = None
    refund_group_bet_ids: tuple[int, ...] = field(default_factory=tuple)
    refund_reason: str | None = None
    refunded_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MatchExportRatingTransaction:
    id: int
    match_entry_id: int
    rating_rule_version: int
    rating_before: Decimal
    amount: Decimal
    rating_after: Decimal
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MatchExportMatch:
    id: int
    name: str
    description: str | None
    source_kind: MatchSourceKind
    grade: MatchGrade
    scheduled_at: datetime
    status: MatchStatus
    terminal_reason: str | None
    finish_time_ms: int | None
    course: MatchExportCourse
    condition: MatchExportCondition | None
    settlement_evidence_present: bool = False
    cancellation_evidence_present: bool = False
    rollback_evidence_present: bool = False
    entries: tuple[MatchExportEntry, ...] = field(default_factory=tuple)
    bets: tuple[MatchExportBet, ...] = field(default_factory=tuple)
    ratings: tuple[MatchExportRatingTransaction, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MatchSeasonExportSource:
    """Complete closed-session persistence source for one derived Season."""

    season: MatchExportSeason
    source_cutoff: datetime
    matches: tuple[MatchExportMatch, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MatchExportProjection:
    """Renderer-ready complete Circle Match Season snapshot."""

    season: MatchExportSeason
    source_cutoff: datetime
    matches: tuple[MatchExportMatch, ...]
    projection_version: str = MATCH_EXPORT_PROJECTION_VERSION

    @property
    def entry_count(self) -> int:
        return sum(len(match.entries) for match in self.matches)

    @property
    def bet_count(self) -> int:
        return sum(len(match.bets) for match in self.matches)

    @property
    def rating_transaction_count(self) -> int:
        return sum(len(match.ratings) for match in self.matches)

    @property
    def workbook_data_row_count(self) -> int:
        return len(self.matches) + self.entry_count + self.bet_count + self.rating_transaction_count


class MatchSeasonExportRepository(Protocol):
    """Read-only persistence port for complete derived Match Season snapshots."""

    def search_seasons(self, *, query: str, limit: int) -> tuple[MatchExportSeasonChoice, ...]: ...

    def get_season_source(
        self,
        *,
        season: MatchExportSeason,
        source_cutoff: datetime,
    ) -> MatchSeasonExportSource | None: ...


class MatchSeasonExportUnitOfWork(UnitOfWork, Protocol):
    @property
    def match_season_exports(self) -> MatchSeasonExportRepository: ...


class MatchSeasonExportRenderer(Protocol):
    def render(
        self,
        projection: MatchExportProjection,
        *,
        generated_at: datetime,
    ) -> ExportArtifact: ...


@dataclass(frozen=True, slots=True)
class MatchSeasonExports:
    """Query, validate, and render complete Circle Match Season artifacts."""

    query_runner: QueryRunner[MatchSeasonExportUnitOfWork]
    renderer: MatchSeasonExportRenderer
    clock: Callable[[], datetime]

    def search_seasons(self, *, query: str = "", limit: int = 25) -> tuple[MatchExportSeasonChoice, ...]:
        if not isinstance(query, str):
            raise ValueError("query must be a string.")
        normalized_query = query.strip()
        if len(normalized_query) > 100:
            raise ValueError("query must contain at most 100 characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")
        try:
            choices = self.query_runner.run(
                lambda uow: uow.match_season_exports.search_seasons(
                    query=normalized_query,
                    limit=limit,
                )
            )
            if len(choices) > limit:
                raise MatchSeasonExportInvalidSourceError("Match Season choice limit was exceeded.")
            _require_unique((choice.key for choice in choices), field_name="Season choice keys")
            for choice in choices:
                expected = parse_match_export_season(choice.key)
                if choice.name != expected.name:
                    raise ValueError("Match Season choice name does not match its key.")
            return choices
        except MatchSeasonExportError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchSeasonExportInvalidSourceError("Stored Match Season choices are malformed.") from exc

    def export_season(self, *, season_key: str) -> ExportArtifact:
        try:
            season = parse_match_export_season(season_key)
        except (TypeError, ValueError) as exc:
            raise MatchSeasonExportUnavailableError("Match Season key is invalid.") from exc
        source_cutoff = normalize_utc_datetime(self.clock(), field_name="source_cutoff")
        try:
            source = self.query_runner.run(
                lambda uow: uow.match_season_exports.get_season_source(
                    season=season,
                    source_cutoff=source_cutoff,
                )
            )
            if source is None:
                raise MatchSeasonExportUnavailableError("Match Season is not available for export.")
            if source.season != season or source.source_cutoff != source_cutoff:
                raise MatchSeasonExportInvalidSourceError("Match Season source provenance does not match the request.")
            projection = _build_projection(source)
        except MatchSeasonExportError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchSeasonExportInvalidSourceError("Stored Match Season export facts are malformed.") from exc

        generated_at = normalize_utc_datetime(self.clock(), field_name="generated_at")
        artifact = self.renderer.render(projection, generated_at=generated_at)
        if (
            artifact.schema_version != MATCH_EXPORT_WORKBOOK_SCHEMA_VERSION
            or artifact.projection_version != MATCH_EXPORT_PROJECTION_VERSION
            or artifact.scope_type != "match_season"
            or artifact.scope_id != season.key
            or artifact.scope_name != season.name
            or artifact.source_cutoff != projection.source_cutoff
            or artifact.generated_at != generated_at
            or artifact.row_count != projection.workbook_data_row_count
        ):
            raise MatchSeasonExportInvalidSourceError("Match renderer returned mismatched artifact provenance.")
        return artifact


def parse_match_export_season(key: str) -> MatchExportSeason:
    """Parse one stable KST half-year key into exact UTC half-open bounds."""

    if not isinstance(key, str):
        raise TypeError("season key must be a string.")
    match = _SEASON_KEY.fullmatch(key.strip())
    if match is None:
        raise ValueError("season key must use YYYY-split-1 or YYYY-split-2.")
    year = int(match.group("year"))
    split = int(match.group("split"))
    if not 1 <= year <= 9998:
        raise ValueError("season year is outside the supported datetime range.")
    start_month = 1 if split == 1 else 7
    end_year, end_month = (year, 7) if split == 1 else (year + 1, 1)
    starts_at = datetime(year, start_month, 1, tzinfo=_KST).astimezone(UTC)
    ends_at = datetime(end_year, end_month, 1, tzinfo=_KST).astimezone(UTC)
    return MatchExportSeason(
        key=f"{year:04d}-split-{split}",
        name=f"{year:04d} Split {split}",
        starts_at=starts_at,
        ends_at=ends_at,
    )


def match_export_season_for_datetime(value: datetime) -> MatchExportSeason:
    """Classify one UTC Match schedule into its KST calendar half."""

    normalized = normalize_utc_datetime(value, field_name="scheduled_at")
    local = normalized.astimezone(_KST)
    split = 1 if local.month <= 6 else 2
    return parse_match_export_season(f"{local.year:04d}-split-{split}")


def _build_projection(source: MatchSeasonExportSource) -> MatchExportProjection:
    _validate_source(source)
    return MatchExportProjection(
        season=source.season,
        source_cutoff=source.source_cutoff,
        matches=source.matches,
    )


def _validate_source(source: MatchSeasonExportSource) -> None:
    if not isinstance(source, MatchSeasonExportSource):
        raise ValueError("Match Season source has an invalid type.")
    expected_season = parse_match_export_season(source.season.key)
    if source.season != expected_season:
        raise ValueError("Match Season bounds or display name do not match its key.")
    normalize_utc_datetime(source.source_cutoff, field_name="source_cutoff")
    if not source.matches:
        raise ValueError("Match Season source must contain at least one Match.")
    _require_unique((match.id for match in source.matches), field_name="Match IDs")
    global_entry_ids: set[int] = set()
    global_bet_ids: set[int] = set()
    global_rating_ids: set[int] = set()
    previous_order: tuple[datetime, int] | None = None
    for match in source.matches:
        _require_positive_int(match.id, field_name="match.id")
        if not match.name.strip():
            raise ValueError("Match name must be non-empty.")
        scheduled_at = normalize_utc_datetime(match.scheduled_at, field_name="match.scheduled_at")
        if not source.season.starts_at <= scheduled_at < source.season.ends_at:
            raise ValueError("Match schedule is outside the selected Season.")
        order = (scheduled_at, match.id)
        if previous_order is not None and order < previous_order:
            raise ValueError("Matches must use deterministic schedule/ID order.")
        previous_order = order
        source_kind = MatchSourceKind(match.source_kind)
        MatchGrade(match.grade)
        MatchStatus(match.status)
        _validate_course(match.course)
        if match.condition is not None:
            MatchSeason(match.condition.season)
            MatchWeather(match.condition.weather)
            MatchTimeOfDay(match.condition.time_of_day)
            MatchTrackCondition(match.condition.track_condition)
        if match.finish_time_ms is not None and (
            isinstance(match.finish_time_ms, bool)
            or not isinstance(match.finish_time_ms, int)
            or match.finish_time_ms <= 0
        ):
            raise ValueError("finish_time_ms must be a positive integer or null.")
        _validate_match_rows(
            match,
            global_entry_ids=global_entry_ids,
            global_bet_ids=global_bet_ids,
            global_rating_ids=global_rating_ids,
        )
        if source_kind is MatchSourceKind.IMPORTED_V1 and (match.bets or match.ratings):
            raise ValueError("Imported Match export must not synthesize V2 Bet or Rating rows.")
        _validate_lifecycle_evidence(match, source_kind=source_kind)


def _validate_course(course: MatchExportCourse) -> None:
    if not isinstance(course, MatchExportCourse) or not course.stadium_name.strip():
        raise ValueError("Match course identity must be complete.")
    _require_positive_int(course.course_id, field_name="course.id")
    MatchSurface(course.surface)
    MatchDirection(course.direction)
    StadiumCourseLayout(course.layout)
    _require_positive_int(course.distance, field_name="course.distance")


def _validate_lifecycle_evidence(match: MatchExportMatch, *, source_kind: MatchSourceKind) -> None:
    evidence = (
        match.settlement_evidence_present,
        match.cancellation_evidence_present,
        match.rollback_evidence_present,
    )
    if any(not isinstance(value, bool) for value in evidence):
        raise ValueError("Match lifecycle evidence markers must be booleans.")
    if source_kind is MatchSourceKind.IMPORTED_V1:
        if any(evidence):
            raise ValueError("Imported Match must not expose native lifecycle audit evidence.")
        return
    expected = {
        MatchStatus.SETTLED: (True, False, False),
        MatchStatus.CANCELLED: (False, True, False),
        MatchStatus.VOIDED: (True, False, True),
    }.get(MatchStatus(match.status), (False, False, False))
    if evidence != expected:
        raise ValueError("Native Match lifecycle status and retained audit evidence are inconsistent.")


def _validate_match_rows(
    match: MatchExportMatch,
    *,
    global_entry_ids: set[int],
    global_bet_ids: set[int],
    global_rating_ids: set[int],
) -> None:
    _require_unique((entry.id for entry in match.entries), field_name="Match Entry IDs")
    _require_unique((entry.entry_number for entry in match.entries), field_name="Match Entry numbers")
    entry_by_id: dict[int, MatchExportEntry] = {}
    for entry in match.entries:
        for field_name, value in (
            ("entry.id", entry.id),
            ("entry.entry_number", entry.entry_number),
            ("entry.game_account_id", entry.game_account_id),
        ):
            _require_positive_int(value, field_name=field_name)
        if entry.id in global_entry_ids:
            raise ValueError("Match Entry IDs must be globally unique.")
        global_entry_ids.add(entry.id)
        if (
            not entry.game_account_name.strip()
            or not entry.owner_at_event_display_name.strip()
            or not entry.character_name.strip()
        ):
            raise ValueError("Match Entry display identity must be complete.")
        GameRegion(entry.game_region)
        for field_name, value in (("rank", entry.rank), ("popularity_rank", entry.popularity_rank)):
            if value is not None:
                _require_positive_int(value, field_name=field_name)
        if entry.rating_disposition is not None:
            MatchRatingDisposition(entry.rating_disposition)
        entry_by_id[entry.id] = entry

    _require_unique((bet.id for bet in match.bets), field_name="Match Bet IDs")
    for bet in match.bets:
        _require_positive_int(bet.id, field_name="bet.id")
        if bet.id in global_bet_ids:
            raise ValueError("Bet IDs must be globally unique.")
        global_bet_ids.add(bet.id)
        if not bet.persona_display_name.strip():
            raise ValueError("Bet Persona display name must be non-empty.")
        bet_type = BetType(bet.bet_type)
        BetStatus(bet.status)
        canonical = canonicalize_selections(bet_type, bet.selection_entry_ids)
        if canonical != bet.selection_entry_ids or not set(canonical).issubset(entry_by_id):
            raise ValueError("Bet selections must reference canonical Entries in the same Match.")
        _require_positive_int(bet.amount, field_name="bet.amount")
        normalize_utc_datetime(bet.created_at, field_name="bet.created_at")
        normalize_utc_datetime(bet.updated_at, field_name="bet.updated_at")
        _validate_bet_economics(bet)

    _require_unique((rating.id for rating in match.ratings), field_name="Match Rating transaction IDs")
    for rating in match.ratings:
        _require_positive_int(rating.id, field_name="rating.id")
        if rating.id in global_rating_ids:
            raise ValueError("Rating transaction IDs must be globally unique.")
        global_rating_ids.add(rating.id)
        if rating.match_entry_id not in entry_by_id:
            raise ValueError("Rating transaction must reference an Entry in the same Match.")
        _require_positive_int(rating.rating_rule_version, field_name="rating.rule_version")
        for field_name, value in (
            ("rating_before", rating.rating_before),
            ("amount", rating.amount),
            ("rating_after", rating.rating_after),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{field_name} must be a finite Decimal.")
        if rating.rating_after != rating.rating_before + rating.amount:
            raise ValueError("Rating transaction is not balanced.")
        normalize_utc_datetime(rating.created_at, field_name="rating.created_at")


def _validate_bet_economics(bet: MatchExportBet) -> None:
    if bet.applied_odds is not None and (
        not isinstance(bet.applied_odds, Decimal)
        or not bet.applied_odds.is_finite()
        or bet.applied_odds <= 0
        or bet.applied_odds.as_tuple().exponent != -1
    ):
        raise ValueError("Applied odds must be a positive one-decimal Decimal or null.")
    for field_name, value in (
        ("payout_amount", bet.payout_amount),
        ("payout_reversal_amount", bet.payout_reversal_amount),
        ("refund_amount", bet.refund_amount),
    ):
        if value is not None:
            _require_positive_int(value, field_name=field_name)
    group = tuple(sorted(bet.payout_group_bet_ids))
    if group != bet.payout_group_bet_ids or len(set(group)) != len(group):
        raise ValueError("Payout group Bet IDs must be sorted and unique.")
    if (bet.payout_amount is None) != (not group):
        raise ValueError("Payout amount and grouped Bet provenance must be present together.")
    if bet.payout_amount is not None and bet.id not in group:
        raise ValueError("Payout group must contain the exported Bet.")
    if bet.payout_reversal_amount is not None and bet.payout_amount is None:
        raise ValueError("Payout reversal requires original payout evidence.")
    refund_group = tuple(sorted(bet.refund_group_bet_ids))
    if refund_group != bet.refund_group_bet_ids or len(set(refund_group)) != len(refund_group):
        raise ValueError("Refund group Bet IDs must be sorted and unique.")
    if (bet.refund_amount is None) != (not refund_group) or (bet.refund_amount is None) != (bet.refunded_at is None):
        raise ValueError("Bet refund amount, group, and timestamp must be present together.")
    if bet.refund_amount is not None and bet.id not in refund_group:
        raise ValueError("Refund group must contain the exported Bet.")
    if bet.refund_reason is not None and bet.refund_amount is None:
        raise ValueError("Bet refund reason requires stored refund evidence.")
    if bet.refund_reason is not None and not bet.refund_reason.strip():
        raise ValueError("Bet refund reason must be non-empty.")
    if bet.refunded_at is not None:
        normalize_utc_datetime(bet.refunded_at, field_name="bet.refunded_at")


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _require_unique(values: Iterable[object], *, field_name: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{field_name} must be unique.")
