"""Read-only staff projections for native Match betting-open."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.match import MATCH_ENTRY_MAXIMUM_COUNT, MatchGrade, MatchSourceKind, MatchStatus

from .betting_open import (
    MATCH_BETTING_OPEN_MINIMUM_ENTRY_COUNT,
    MatchBettingOpenRatingRuleCoverage,
    MatchBettingOpenTarget,
    match_betting_open_rating_rule_readiness_issue,
)


class MatchStaffBettingOpenQueryError(ValueError):
    """Base error for rejected staff betting-open projections."""


class MatchStaffBettingOpenUnavailableError(MatchStaffBettingOpenQueryError):
    """The selected Match is no longer an eligible opening target."""


class MatchStaffBettingOpenInvalidSourceError(MatchStaffBettingOpenQueryError):
    """Stored opening facts are malformed."""


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


@dataclass(frozen=True, slots=True)
class MatchBettingOpenTargetChoice:
    """One bounded native scheduled Match selector row."""

    match_id: int
    match_name: str
    grade: MatchGrade
    entry_count: int
    has_complete_condition: bool
    rating_rule_coverage: MatchBettingOpenRatingRuleCoverage

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        if isinstance(self.entry_count, bool) or not isinstance(self.entry_count, int) or self.entry_count < 0:
            raise ValueError("entry_count must be a non-negative integer.")
        if not isinstance(self.has_complete_condition, bool):
            raise ValueError("has_complete_condition must be a boolean.")
        if not isinstance(self.rating_rule_coverage, MatchBettingOpenRatingRuleCoverage):
            raise ValueError("rating_rule_coverage must be MatchBettingOpenRatingRuleCoverage.")

    @property
    def is_ready(self) -> bool:
        return (
            self.has_complete_condition
            and MATCH_BETTING_OPEN_MINIMUM_ENTRY_COUNT <= self.entry_count <= MATCH_ENTRY_MAXIMUM_COUNT
            and match_betting_open_rating_rule_readiness_issue(
                grade=self.grade,
                field_size=self.entry_count,
                coverage=self.rating_rule_coverage,
            )
            is None
        )


class MatchStaffBettingOpenQueryRepository(Protocol):
    """Read-only persistence operations for opening target selection and Preview."""

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchBettingOpenTargetChoice, ...]: ...

    def get_target(self, *, match_id: int, guild_id: str) -> MatchBettingOpenTarget | None: ...


class MatchStaffBettingOpenQueryUnitOfWork(UnitOfWork, Protocol):
    """Read-only UoW exposing betting-open query projections."""

    @property
    def match_staff_betting_open_queries(self) -> MatchStaffBettingOpenQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchStaffBettingOpenQueries:
    """Application entry point for closed-session betting-open projections."""

    query_runner: QueryRunner[MatchStaffBettingOpenQueryUnitOfWork]

    def search_targets(self, *, search: str, limit: int = 25) -> tuple[MatchBettingOpenTargetChoice, ...]:
        if not isinstance(search, str):
            raise ValueError("search must be a string.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25.")
        normalized = search.strip()

        def query(unit_of_work: MatchStaffBettingOpenQueryUnitOfWork) -> tuple[MatchBettingOpenTargetChoice, ...]:
            try:
                return unit_of_work.match_staff_betting_open_queries.search_targets(
                    search=normalized,
                    limit=limit,
                )
            except (TypeError, ValueError) as exc:
                raise MatchStaffBettingOpenInvalidSourceError(
                    "Stored Match betting-open target list is malformed."
                ) from exc

        return self.query_runner.run(query)

    def get_target(self, *, match_id: int, guild_id: str) -> MatchBettingOpenTarget:
        _require_positive_int(match_id, field_name="match_id")
        guild_id = _normalized_string(guild_id, field_name="guild_id", max_length=32)

        def query(unit_of_work: MatchStaffBettingOpenQueryUnitOfWork) -> MatchBettingOpenTarget:
            try:
                target = unit_of_work.match_staff_betting_open_queries.get_target(
                    match_id=match_id,
                    guild_id=guild_id,
                )
            except (TypeError, ValueError) as exc:
                raise MatchStaffBettingOpenInvalidSourceError("Stored Match betting-open source is malformed.") from exc
            if target is None:
                raise MatchStaffBettingOpenUnavailableError("Match betting-open target does not exist.")
            if target.source_kind != MatchSourceKind.NATIVE_V2 or target.status != MatchStatus.SCHEDULED:
                raise MatchStaffBettingOpenUnavailableError("Betting-open target must be a native scheduled Match.")
            return target

        return self.query_runner.run(query)
