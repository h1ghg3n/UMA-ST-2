"""Read-only staff projections for native scheduled Match setup editing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork

from .setup import MatchSetupTarget


class MatchStaffSetupQueryError(ValueError):
    """Base error for rejected staff setup projections."""


class MatchStaffSetupInvalidSourceError(MatchStaffSetupQueryError):
    """Stored setup projection facts are malformed."""


@dataclass(frozen=True, slots=True)
class MatchSetupEditorTarget:
    """One detached setup target plus current roster display count."""

    setup: MatchSetupTarget
    entry_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.setup, MatchSetupTarget):
            raise ValueError("setup must be a MatchSetupTarget.")
        if isinstance(self.entry_count, bool) or not isinstance(self.entry_count, int) or self.entry_count < 0:
            raise ValueError("entry_count must be a non-negative integer.")


class MatchStaffSetupQueryRepository(Protocol):
    """Read-only persistence operations for editable setup targets."""

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchSetupEditorTarget, ...]: ...

    def get_target(self, *, match_id: int) -> MatchSetupEditorTarget | None: ...


class MatchStaffSetupQueryUnitOfWork(UnitOfWork, Protocol):
    """Read-only UoW exposing staff setup projections."""

    @property
    def match_staff_setup_queries(self) -> MatchStaffSetupQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchStaffSetupQueries:
    """Application entry point for bounded closed-session setup projections."""

    query_runner: QueryRunner[MatchStaffSetupQueryUnitOfWork]

    def search_targets(self, *, search: str = "", limit: int = 25) -> tuple[MatchSetupEditorTarget, ...]:
        if not isinstance(search, str):
            raise ValueError("search must be a string.")
        normalized = search.strip()
        if len(normalized) > 200:
            raise ValueError("search must be at most 200 characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")

        def query(unit_of_work: MatchStaffSetupQueryUnitOfWork) -> tuple[MatchSetupEditorTarget, ...]:
            try:
                return unit_of_work.match_staff_setup_queries.search_targets(
                    search=normalized,
                    limit=limit,
                )
            except (TypeError, ValueError) as exc:
                raise MatchStaffSetupInvalidSourceError("Stored Match setup target list is malformed.") from exc

        return self.query_runner.run(query)

    def get_target(self, *, match_id: int) -> MatchSetupEditorTarget | None:
        if isinstance(match_id, bool) or not isinstance(match_id, int) or match_id <= 0:
            raise ValueError("match_id must be a positive integer.")

        def query(unit_of_work: MatchStaffSetupQueryUnitOfWork) -> MatchSetupEditorTarget | None:
            try:
                return unit_of_work.match_staff_setup_queries.get_target(match_id=match_id)
            except (TypeError, ValueError) as exc:
                raise MatchStaffSetupInvalidSourceError("Stored Match setup target is malformed.") from exc

        return self.query_runner.run(query)
