"""Read-only staff projections for native Match betting-close."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.shared import normalize_utc_datetime


class MatchStaffBettingCloseQueryError(ValueError):
    """Base error for rejected staff betting-close projections."""


class MatchStaffBettingCloseUnavailableError(MatchStaffBettingCloseQueryError):
    """The selected Match is no longer an eligible close target."""


class MatchStaffBettingCloseInvalidSourceError(MatchStaffBettingCloseQueryError):
    """Stored close target facts are malformed."""


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
class MatchBettingClosePreviewTarget:
    """Public-safe aggregate used only by the staff Preview query path."""

    match_id: int
    match_name: str
    source_kind: MatchSourceKind
    status: MatchStatus
    grade: MatchGrade
    scheduled_at: datetime
    entry_count: int
    active_bet_count: int
    active_stake_total: int

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchStatus(self.status))
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        _require_positive_int(self.entry_count, field_name="entry_count")
        for field_name in ("active_bet_count", "active_stake_total"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer.")
        if (self.active_bet_count == 0) != (self.active_stake_total == 0):
            raise ValueError("Active Bet count and stake total must both be zero or both be positive.")


@dataclass(frozen=True, slots=True)
class MatchBettingCloseTargetChoice:
    """One bounded native betting-open Match selector row."""

    match_id: int
    match_name: str
    entry_count: int
    active_bet_count: int

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        for field_name in ("entry_count", "active_bet_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer.")


class MatchStaffBettingCloseQueryRepository(Protocol):
    """Read-only persistence operations for close target selection and Preview."""

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchBettingCloseTargetChoice, ...]: ...

    def get_target(self, *, match_id: int) -> MatchBettingClosePreviewTarget | None: ...


class MatchStaffBettingCloseQueryUnitOfWork(UnitOfWork, Protocol):
    """Read-only UoW exposing betting-close projections."""

    @property
    def match_staff_betting_close_queries(self) -> MatchStaffBettingCloseQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchStaffBettingCloseQueries:
    """Application entry point for closed-session betting-close projections."""

    query_runner: QueryRunner[MatchStaffBettingCloseQueryUnitOfWork]

    def search_targets(self, *, search: str, limit: int = 25) -> tuple[MatchBettingCloseTargetChoice, ...]:
        if not isinstance(search, str):
            raise ValueError("search must be a string.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25.")
        normalized = search.strip()

        def query(unit_of_work: MatchStaffBettingCloseQueryUnitOfWork) -> tuple[MatchBettingCloseTargetChoice, ...]:
            try:
                return unit_of_work.match_staff_betting_close_queries.search_targets(
                    search=normalized,
                    limit=limit,
                )
            except (TypeError, ValueError) as exc:
                raise MatchStaffBettingCloseInvalidSourceError(
                    "Stored Match betting-close target list is malformed."
                ) from exc

        return self.query_runner.run(query)

    def get_target(self, *, match_id: int) -> MatchBettingClosePreviewTarget:
        _require_positive_int(match_id, field_name="match_id")

        def query(unit_of_work: MatchStaffBettingCloseQueryUnitOfWork) -> MatchBettingClosePreviewTarget:
            try:
                target = unit_of_work.match_staff_betting_close_queries.get_target(match_id=match_id)
            except (TypeError, ValueError) as exc:
                raise MatchStaffBettingCloseInvalidSourceError(
                    "Stored Match betting-close source is malformed."
                ) from exc
            if target is None:
                raise MatchStaffBettingCloseUnavailableError("Match betting-close target does not exist.")
            if target.source_kind != MatchSourceKind.NATIVE_V2 or target.status != MatchStatus.BETTING_OPEN:
                raise MatchStaffBettingCloseUnavailableError(
                    "Betting-close target must be a native betting-open Match."
                )
            return target

        return self.query_runner.run(query)
