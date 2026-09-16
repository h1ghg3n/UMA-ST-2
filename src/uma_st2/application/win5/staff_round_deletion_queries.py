"""Read-only staff projections for guarded setup-Round deletion."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.win5 import Win5RoundStatus, Win5RoundType, Win5SeasonStatus

from .round_deletion import Win5SetupRoundDeletionSnapshot, Win5SetupRoundDependencyState


class Win5StaffRoundDeletionQueryError(ValueError):
    """Base error for expected staff deletion-query rejection."""


class Win5StaffRoundDeletionUnavailableError(Win5StaffRoundDeletionQueryError):
    """The requested Round is no longer eligible for hard deletion."""


class Win5StaffRoundDeletionInvalidSourceError(Win5StaffRoundDeletionQueryError):
    """Stored facts cannot form a safe destructive preview."""


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
class Win5SetupRoundDeletionTargetChoice:
    """One currently eligible setup Round in the destructive selector."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    round_id: int
    round_name: str
    round_type: Win5RoundType
    round_status: Win5RoundStatus
    race_count: int
    entry_count: int

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_bounded_string(self.season_name, field_name="season_name", max_length=100)
        _require_bounded_string(self.round_name, field_name="round_name", max_length=100)
        object.__setattr__(self, "season_status", Win5SeasonStatus(self.season_status))
        object.__setattr__(self, "round_type", Win5RoundType(self.round_type))
        object.__setattr__(self, "round_status", Win5RoundStatus(self.round_status))
        _require_non_negative_int(self.race_count, field_name="race_count")
        _require_non_negative_int(self.entry_count, field_name="entry_count")


@dataclass(frozen=True, slots=True)
class Win5SetupRoundDeletionTargetPage:
    """Closed-session deletion selector page with stable navigation flags."""

    offset: int
    limit: int
    choices: tuple[Win5SetupRoundDeletionTargetChoice, ...] = field(default_factory=tuple)
    has_previous: bool = False
    has_next: bool = False

    def __post_init__(self) -> None:
        _require_non_negative_int(self.offset, field_name="offset")
        _require_positive_int(self.limit, field_name="limit")


@dataclass(frozen=True, slots=True)
class Win5SetupRoundDeletionTargetSource:
    """Current graph plus downstream counts used to build a deletion preview."""

    snapshot: Win5SetupRoundDeletionSnapshot
    dependencies: Win5SetupRoundDependencyState


class Win5StaffRoundDeletionQueryRepository(Protocol):
    """Read-only persistence operations for setup-Round deletion interactions."""

    def list_setup_round_deletion_targets(
        self,
        *,
        offset: int,
        limit: int,
    ) -> tuple[Win5SetupRoundDeletionTargetChoice, ...]: ...

    def get_setup_round_deletion_target_source(
        self,
        *,
        round_id: int,
    ) -> Win5SetupRoundDeletionTargetSource | None: ...


class Win5StaffRoundDeletionQueryUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only staff setup-Round deletion projections."""

    @property
    def win5_staff_round_deletion_queries(self) -> Win5StaffRoundDeletionQueryRepository: ...


@dataclass(frozen=True, slots=True)
class Win5StaffRoundDeletionQueries:
    """Application entry point for paged targets and fresh destructive previews."""

    query_runner: QueryRunner[Win5StaffRoundDeletionQueryUnitOfWork]

    def list_setup_round_deletion_targets(
        self,
        *,
        offset: int = 0,
        limit: int = 25,
    ) -> Win5SetupRoundDeletionTargetPage:
        _require_non_negative_int(offset, field_name="offset")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")
        choices = self.query_runner.run(
            lambda unit_of_work: unit_of_work.win5_staff_round_deletion_queries.list_setup_round_deletion_targets(
                offset=offset,
                limit=limit + 1,
            )
        )
        for choice in choices:
            if (
                choice.season_status not in {Win5SeasonStatus.DRAFT, Win5SeasonStatus.ACTIVE}
                or choice.round_status != Win5RoundStatus.SETUP
            ):
                raise Win5StaffRoundDeletionInvalidSourceError(
                    "Setup-Round deletion selector returned an ineligible target."
                )
        return Win5SetupRoundDeletionTargetPage(
            offset=offset,
            limit=limit,
            choices=choices[:limit],
            has_previous=offset > 0,
            has_next=len(choices) > limit,
        )

    def get_setup_round_deletion_target(self, *, round_id: int) -> Win5SetupRoundDeletionSnapshot:
        _require_positive_int(round_id, field_name="round_id")

        def query(unit_of_work: Win5StaffRoundDeletionQueryUnitOfWork) -> Win5SetupRoundDeletionSnapshot:
            try:
                source = unit_of_work.win5_staff_round_deletion_queries.get_setup_round_deletion_target_source(
                    round_id=round_id
                )
            except (TypeError, ValueError) as exc:
                raise Win5StaffRoundDeletionInvalidSourceError("Stored setup Round graph facts are malformed.") from exc
            if source is None:
                raise Win5StaffRoundDeletionUnavailableError("WIN5 Round does not exist.")
            snapshot = source.snapshot
            if (
                snapshot.season_status not in {Win5SeasonStatus.DRAFT, Win5SeasonStatus.ACTIVE}
                or snapshot.round_status != Win5RoundStatus.SETUP
            ):
                raise Win5StaffRoundDeletionUnavailableError(
                    "Only a setup Round in a draft or active Season may be hard deleted."
                )
            if source.dependencies.has_downstream_facts:
                raise Win5StaffRoundDeletionUnavailableError(
                    "WIN5 Round has downstream Submission, Result, score, or publication facts."
                )
            return snapshot

        return self.query_runner.run(query)
