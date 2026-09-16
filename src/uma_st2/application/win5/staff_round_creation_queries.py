"""Read-only staff projections for WIN5 Round creation interactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.win5 import Win5SeasonStatus
from uma_st2.shared import normalize_utc_datetime


class Win5StaffRoundCreationQueryError(ValueError):
    """Base error for expected staff Round-creation query rejection."""


class Win5StaffRoundCreationInvalidSourceError(Win5StaffRoundCreationQueryError):
    """Stored Season facts cannot form a safe Round-creation selector."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _require_non_negative_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


def _require_bounded_string(value: str, *, field_name: str, max_length: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"{field_name} must be a non-empty string of at most {max_length} characters.")


@dataclass(frozen=True, slots=True)
class Win5RoundCreationSeasonChoice:
    """One draft/active Season available for a new Round."""

    id: int
    name: str
    status: Win5SeasonStatus
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_bounded_string(self.name, field_name="name", max_length=100)
        object.__setattr__(self, "status", Win5SeasonStatus(self.status))
        for field_name in ("starts_at", "ends_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    normalize_utc_datetime(value, field_name=field_name),
                )


@dataclass(frozen=True, slots=True)
class Win5RoundCreationSeasonPage:
    """Closed-session Season selector page with stable navigation flags."""

    offset: int
    limit: int
    choices: tuple[Win5RoundCreationSeasonChoice, ...] = field(default_factory=tuple)
    has_previous: bool = False
    has_next: bool = False

    def __post_init__(self) -> None:
        _require_non_negative_int(self.offset, field_name="offset")
        _require_positive_int(self.limit, field_name="limit")


class Win5StaffRoundCreationQueryRepository(Protocol):
    """Read-only persistence operations for Round-creation Season choices."""

    def list_round_creation_seasons(
        self,
        *,
        offset: int,
        limit: int,
    ) -> tuple[Win5RoundCreationSeasonChoice, ...]: ...


class Win5StaffRoundCreationQueryUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only staff Round-creation projections."""

    @property
    def win5_staff_round_creation_queries(self) -> Win5StaffRoundCreationQueryRepository: ...


@dataclass(frozen=True, slots=True)
class Win5StaffRoundCreationQueries:
    """Application entry point for paged eligible Season choices."""

    query_runner: QueryRunner[Win5StaffRoundCreationQueryUnitOfWork]

    def list_round_creation_seasons(
        self,
        *,
        offset: int = 0,
        limit: int = 25,
    ) -> Win5RoundCreationSeasonPage:
        _require_non_negative_int(offset, field_name="offset")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")
        choices = self.query_runner.run(
            lambda unit_of_work: unit_of_work.win5_staff_round_creation_queries.list_round_creation_seasons(
                offset=offset,
                limit=limit + 1,
            )
        )
        for choice in choices:
            if choice.status not in {Win5SeasonStatus.DRAFT, Win5SeasonStatus.ACTIVE}:
                raise Win5StaffRoundCreationInvalidSourceError("Round-creation selector returned an ineligible Season.")
        return Win5RoundCreationSeasonPage(
            offset=offset,
            limit=limit,
            choices=choices[:limit],
            has_previous=offset > 0,
            has_next=len(choices) > limit,
        )
