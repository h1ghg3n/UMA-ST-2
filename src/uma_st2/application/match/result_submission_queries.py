"""Read-only staff projections for Match result candidate submission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork

from .result_submission import MatchResultSubmissionTarget


class MatchStaffResultSubmissionQueryError(ValueError):
    """Base error for rejected staff result submission projections."""


class MatchStaffResultSubmissionUnavailableError(MatchStaffResultSubmissionQueryError):
    """The selected Match is not currently available for result submission."""


class MatchStaffResultSubmissionInvalidSourceError(MatchStaffResultSubmissionQueryError):
    """Stored result submission target facts are malformed."""


@dataclass(frozen=True, slots=True)
class MatchResultSubmissionTargetChoice:
    """One bounded eligible Match autocomplete row."""

    match_id: int
    match_name: str
    status: str
    entry_count: int
    next_revision_number: int

    def __post_init__(self) -> None:
        if isinstance(self.match_id, bool) or not isinstance(self.match_id, int) or self.match_id <= 0:
            raise ValueError("match_id must be a positive integer.")
        if not isinstance(self.match_name, str) or not self.match_name.strip() or len(self.match_name.strip()) > 200:
            raise ValueError("match_name must be a non-empty string of at most 200 characters.")
        object.__setattr__(self, "match_name", self.match_name.strip())
        if self.status not in {"betting_closed", "result_confirmed"}:
            raise ValueError("status must be an eligible result submission status.")
        if isinstance(self.entry_count, bool) or not isinstance(self.entry_count, int) or self.entry_count <= 0:
            raise ValueError("entry_count must be a positive integer.")
        if (
            isinstance(self.next_revision_number, bool)
            or not isinstance(self.next_revision_number, int)
            or self.next_revision_number <= 0
        ):
            raise ValueError("next_revision_number must be a positive integer.")


class MatchStaffResultSubmissionQueryRepository(Protocol):
    """Read-only persistence operations for result submission UI."""

    def search_targets(
        self,
        *,
        search: str,
        limit: int,
    ) -> tuple[MatchResultSubmissionTargetChoice, ...]: ...

    def get_target(self, *, match_id: int) -> MatchResultSubmissionTarget | None: ...


class MatchStaffResultSubmissionQueryUnitOfWork(UnitOfWork, Protocol):
    """Read-only UoW exposing staff result submission projections."""

    @property
    def match_staff_result_submission_queries(
        self,
    ) -> MatchStaffResultSubmissionQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchStaffResultSubmissionQueries:
    """Application entry point for bounded result submission UI reads."""

    query_runner: QueryRunner[MatchStaffResultSubmissionQueryUnitOfWork]

    def search_targets(
        self,
        *,
        search: str,
        limit: int = 25,
    ) -> tuple[MatchResultSubmissionTargetChoice, ...]:
        if not isinstance(search, str):
            raise ValueError("search must be a string.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25.")
        normalized = search.strip()

        def query(
            unit_of_work: MatchStaffResultSubmissionQueryUnitOfWork,
        ) -> tuple[MatchResultSubmissionTargetChoice, ...]:
            try:
                return unit_of_work.match_staff_result_submission_queries.search_targets(
                    search=normalized,
                    limit=limit,
                )
            except (TypeError, ValueError) as exc:
                raise MatchStaffResultSubmissionInvalidSourceError(
                    "Stored result submission target list is malformed."
                ) from exc

        return self.query_runner.run(query)

    def get_target(self, *, match_id: int) -> MatchResultSubmissionTarget:
        if isinstance(match_id, bool) or not isinstance(match_id, int) or match_id <= 0:
            raise ValueError("match_id must be a positive integer.")

        def query(
            unit_of_work: MatchStaffResultSubmissionQueryUnitOfWork,
        ) -> MatchResultSubmissionTarget:
            try:
                target = unit_of_work.match_staff_result_submission_queries.get_target(match_id=match_id)
            except (TypeError, ValueError) as exc:
                raise MatchStaffResultSubmissionInvalidSourceError(
                    "Stored result submission target is malformed."
                ) from exc
            if target is None:
                raise MatchStaffResultSubmissionUnavailableError("Match is not available for result submission.")
            return target

        return self.query_runner.run(query)
