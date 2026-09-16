"""Read-only staff projections for native Match creation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.match import MatchDirection, MatchSurface, StadiumCourseLayout


class MatchStaffCreationQueryError(ValueError):
    """Base error for rejected Match-creation projections."""


class MatchStaffCreationResultOverflowError(MatchStaffCreationQueryError):
    """Current course master data exceeds the bounded interaction projection."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


@dataclass(frozen=True, slots=True)
class MatchCourseChoice:
    """One exact current stadium-course combination available to the adapter."""

    id: int
    stadium_id: int
    stadium_name: str
    surface: MatchSurface
    distance: int
    direction: MatchDirection
    layout: StadiumCourseLayout

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.stadium_id, field_name="stadium_id")
        _require_positive_int(self.distance, field_name="distance")
        if not isinstance(self.stadium_name, str) or not self.stadium_name.strip() or len(self.stadium_name) > 100:
            raise ValueError("stadium_name must be a non-empty string of at most 100 characters.")
        object.__setattr__(self, "stadium_name", self.stadium_name.strip())
        object.__setattr__(self, "surface", MatchSurface(self.surface))
        object.__setattr__(self, "direction", MatchDirection(self.direction))
        object.__setattr__(self, "layout", StadiumCourseLayout(self.layout))


class MatchStaffCreationQueryRepository(Protocol):
    """Read-only persistence operations for current course choices."""

    def list_course_choices(self, *, limit: int) -> tuple[MatchCourseChoice, ...]: ...


class MatchStaffCreationQueryUnitOfWork(UnitOfWork, Protocol):
    """Feature query UoW exposing only Match-creation projections."""

    @property
    def match_staff_creation_queries(self) -> MatchStaffCreationQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchStaffCreationQueries:
    """Application entry point for one bounded closed-session course projection."""

    query_runner: QueryRunner[MatchStaffCreationQueryUnitOfWork]

    def list_course_choices(self, *, limit: int = 500) -> tuple[MatchCourseChoice, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer from 1 through 500.")
        choices = self.query_runner.run(
            lambda unit_of_work: unit_of_work.match_staff_creation_queries.list_course_choices(limit=limit + 1)
        )
        if len(choices) > limit:
            raise MatchStaffCreationResultOverflowError(
                "Current stadium course master data exceeds the bounded Match creation projection."
            )
        return choices
