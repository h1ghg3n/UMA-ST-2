"""Read-only staff projections for native Match condition setting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork

from .conditions import MatchConditionTarget


class MatchStaffConditionQueryRepository(Protocol):
    """Read-only persistence operations for eligible condition targets."""

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchConditionTarget, ...]: ...

    def get_target(self, *, match_id: int) -> MatchConditionTarget | None: ...


class MatchStaffConditionQueryUnitOfWork(UnitOfWork, Protocol):
    """Feature query UoW exposing only Match condition projections."""

    @property
    def match_staff_condition_queries(self) -> MatchStaffConditionQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchStaffConditionQueries:
    """Application entry point for bounded closed-session condition projections."""

    query_runner: QueryRunner[MatchStaffConditionQueryUnitOfWork]

    def search_targets(self, *, search: str = "", limit: int = 25) -> tuple[MatchConditionTarget, ...]:
        if not isinstance(search, str):
            raise ValueError("search must be a string.")
        normalized = search.strip()
        if len(normalized) > 200:
            raise ValueError("search must be at most 200 characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")
        return self.query_runner.run(
            lambda unit_of_work: unit_of_work.match_staff_condition_queries.search_targets(
                search=normalized,
                limit=limit,
            )
        )

    def get_target(self, *, match_id: int) -> MatchConditionTarget | None:
        if isinstance(match_id, bool) or not isinstance(match_id, int) or match_id <= 0:
            raise ValueError("match_id must be a positive integer.")
        return self.query_runner.run(
            lambda unit_of_work: unit_of_work.match_staff_condition_queries.get_target(match_id=match_id)
        )
