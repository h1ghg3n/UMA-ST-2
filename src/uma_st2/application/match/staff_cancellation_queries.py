"""Read-only staff projections for native whole-Match cancellation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.shared import normalize_utc_datetime

from .cancellation import MATCH_CANCELLABLE_STATUSES


class MatchStaffCancellationQueryError(ValueError):
    """Base error for rejected cancellation projections."""


class MatchStaffCancellationUnavailableError(MatchStaffCancellationQueryError):
    """The selected Match is no longer an eligible cancellation target."""


class MatchStaffCancellationInvalidSourceError(MatchStaffCancellationQueryError):
    """Stored cancellation target facts are malformed."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _require_non_negative_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


def _normalized_string(value: str, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must be a non-empty string of at most {max_length} characters.")
    return normalized


@dataclass(frozen=True, slots=True)
class MatchCancellationTargetChoice:
    """One bounded native pre-settlement Match selector row."""

    match_id: int
    match_name: str
    status: MatchStatus
    active_bet_count: int

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "status", MatchStatus(self.status))
        _require_non_negative_int(self.active_bet_count, field_name="active_bet_count")


@dataclass(frozen=True, slots=True)
class MatchCancellationPreviewTarget:
    """Closed-session aggregate used by the private cancellation Preview."""

    match_id: int
    match_name: str
    source_kind: MatchSourceKind
    status: MatchStatus
    terminal_reason: str | None
    grade: MatchGrade
    scheduled_at: datetime
    entry_count: int
    active_bet_count: int
    active_stake_total: int
    affected_persona_count: int

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchStatus(self.status))
        if self.terminal_reason is not None:
            object.__setattr__(
                self,
                "terminal_reason",
                _normalized_string(self.terminal_reason, field_name="terminal_reason", max_length=255),
            )
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        for field_name in (
            "entry_count",
            "active_bet_count",
            "active_stake_total",
            "affected_persona_count",
        ):
            _require_non_negative_int(getattr(self, field_name), field_name=field_name)
        if (self.active_bet_count == 0) != (self.active_stake_total == 0):
            raise ValueError("Active Bet count and stake total must both be zero or both be positive.")
        if self.affected_persona_count > self.active_bet_count:
            raise ValueError("Affected Persona count cannot exceed active Bet count.")
        if (self.active_bet_count == 0) != (self.affected_persona_count == 0):
            raise ValueError("Active Bet and affected Persona counts must both be zero or both be positive.")


class MatchStaffCancellationQueryRepository(Protocol):
    """Read-only persistence operations for cancellation selection and Preview."""

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchCancellationTargetChoice, ...]: ...

    def get_target(self, *, match_id: int) -> MatchCancellationPreviewTarget | None: ...


class MatchStaffCancellationQueryUnitOfWork(UnitOfWork, Protocol):
    """Read-only UoW exposing whole-Match cancellation projections."""

    @property
    def match_staff_cancellation_queries(self) -> MatchStaffCancellationQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchStaffCancellationQueries:
    """Application entry point for closed-session cancellation projections."""

    query_runner: QueryRunner[MatchStaffCancellationQueryUnitOfWork]

    def search_targets(self, *, search: str, limit: int = 25) -> tuple[MatchCancellationTargetChoice, ...]:
        if not isinstance(search, str):
            raise ValueError("search must be a string.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25.")
        normalized = search.strip()

        def query(unit_of_work: MatchStaffCancellationQueryUnitOfWork) -> tuple[MatchCancellationTargetChoice, ...]:
            try:
                return unit_of_work.match_staff_cancellation_queries.search_targets(
                    search=normalized,
                    limit=limit,
                )
            except (TypeError, ValueError) as exc:
                raise MatchStaffCancellationInvalidSourceError(
                    "Stored Match cancellation target list is malformed."
                ) from exc

        return self.query_runner.run(query)

    def get_target(self, *, match_id: int) -> MatchCancellationPreviewTarget:
        _require_positive_int(match_id, field_name="match_id")

        def query(unit_of_work: MatchStaffCancellationQueryUnitOfWork) -> MatchCancellationPreviewTarget:
            try:
                target = unit_of_work.match_staff_cancellation_queries.get_target(match_id=match_id)
            except (TypeError, ValueError) as exc:
                raise MatchStaffCancellationInvalidSourceError(
                    "Stored Match cancellation source is malformed."
                ) from exc
            if target is None:
                raise MatchStaffCancellationUnavailableError("Match cancellation target does not exist.")
            if (
                target.source_kind != MatchSourceKind.NATIVE_V2
                or target.status not in MATCH_CANCELLABLE_STATUSES
                or target.terminal_reason is not None
            ):
                raise MatchStaffCancellationUnavailableError(
                    "Cancellation target must be a native pre-settlement Match."
                )
            return target

        return self.query_runner.run(query)
