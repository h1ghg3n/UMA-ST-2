"""Read-only staff projections for WIN5 Round open/close interactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.win5 import (
    Win5DomainError,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    validate_win5_round_open_readiness,
)

from .round_lifecycle import MAX_OPEN_WIN5_ROUNDS_PER_ACTIVE_SEASON, Win5RoundLifecycleAction


class Win5StaffRoundLifecycleQueryError(ValueError):
    """Base error for expected staff lifecycle-query rejection."""


class Win5StaffRoundLifecycleUnavailableError(Win5StaffRoundLifecycleQueryError):
    """The requested Round is no longer eligible for the selected action."""


class Win5StaffRoundLifecycleInvalidSourceError(Win5StaffRoundLifecycleQueryError):
    """Stored lifecycle facts cannot form a safe staff interaction."""


class Win5StaffRoundOpenLimitError(Win5StaffRoundLifecycleQueryError):
    """The selected active Season currently has no remaining open slot."""


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
class Win5RoundLifecycleTargetChoice:
    """One bounded staff lifecycle selector option."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    round_id: int
    round_name: str
    round_type: Win5RoundType
    round_status: Win5RoundStatus
    race_count: int
    race_entry_count: int
    result_count: int
    season_open_round_count: int

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_bounded_string(self.season_name, field_name="season_name", max_length=100)
        _require_bounded_string(self.round_name, field_name="round_name", max_length=100)
        object.__setattr__(self, "season_status", Win5SeasonStatus(self.season_status))
        object.__setattr__(self, "round_type", Win5RoundType(self.round_type))
        object.__setattr__(self, "round_status", Win5RoundStatus(self.round_status))
        _require_non_negative_int(self.race_count, field_name="race_count")
        _require_non_negative_int(self.race_entry_count, field_name="race_entry_count")
        _require_non_negative_int(self.result_count, field_name="result_count")
        _require_non_negative_int(self.season_open_round_count, field_name="season_open_round_count")


@dataclass(frozen=True, slots=True)
class Win5RoundLifecycleTargetPageSource:
    """Repository page plus active-Season overflow evidence."""

    choices: tuple[Win5RoundLifecycleTargetChoice, ...] = field(default_factory=tuple)
    has_active_open_overflow: bool = False


@dataclass(frozen=True, slots=True)
class Win5RoundLifecycleTargetPage:
    """Closed-session selector page with stable navigation flags."""

    action: Win5RoundLifecycleAction
    offset: int
    limit: int
    choices: tuple[Win5RoundLifecycleTargetChoice, ...] = field(default_factory=tuple)
    has_previous: bool = False
    has_next: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", Win5RoundLifecycleAction(self.action))
        _require_non_negative_int(self.offset, field_name="offset")
        _require_positive_int(self.limit, field_name="limit")


@dataclass(frozen=True, slots=True)
class Win5RoundLifecycleTargetSource:
    """Current source facts revalidated before rendering a transition preview."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    round_id: int
    round_name: str
    round_type: Win5RoundType
    round_status: Win5RoundStatus
    race_count: int
    race_entry_count: int
    result_count: int
    accepted_submission_count: int
    season_open_round_count: int
    has_active_open_overflow: bool = False

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_bounded_string(self.season_name, field_name="season_name", max_length=100)
        _require_bounded_string(self.round_name, field_name="round_name", max_length=100)
        object.__setattr__(self, "season_status", Win5SeasonStatus(self.season_status))
        object.__setattr__(self, "round_type", Win5RoundType(self.round_type))
        object.__setattr__(self, "round_status", Win5RoundStatus(self.round_status))
        _require_non_negative_int(self.race_count, field_name="race_count")
        _require_non_negative_int(self.race_entry_count, field_name="race_entry_count")
        _require_non_negative_int(self.result_count, field_name="result_count")
        _require_non_negative_int(self.accepted_submission_count, field_name="accepted_submission_count")
        _require_non_negative_int(self.season_open_round_count, field_name="season_open_round_count")
        if not isinstance(self.has_active_open_overflow, bool):
            raise ValueError("has_active_open_overflow must be a boolean.")


@dataclass(frozen=True, slots=True)
class Win5RoundLifecycleTarget:
    """Validated current Round detail carried into a confirmation View."""

    action: Win5RoundLifecycleAction
    season_id: int
    season_name: str
    round_id: int
    round_name: str
    round_type: Win5RoundType
    current_status: Win5RoundStatus
    race_count: int
    race_entry_count: int
    accepted_submission_count: int
    season_open_round_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", Win5RoundLifecycleAction(self.action))
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_bounded_string(self.season_name, field_name="season_name", max_length=100)
        _require_bounded_string(self.round_name, field_name="round_name", max_length=100)
        object.__setattr__(self, "round_type", Win5RoundType(self.round_type))
        object.__setattr__(self, "current_status", Win5RoundStatus(self.current_status))
        _require_non_negative_int(self.race_count, field_name="race_count")
        _require_non_negative_int(self.race_entry_count, field_name="race_entry_count")
        _require_non_negative_int(self.accepted_submission_count, field_name="accepted_submission_count")
        _require_non_negative_int(self.season_open_round_count, field_name="season_open_round_count")

    @property
    def target_status(self) -> Win5RoundStatus:
        if self.action == Win5RoundLifecycleAction.OPEN:
            return Win5RoundStatus.OPEN
        return Win5RoundStatus.CLOSED


class Win5StaffRoundLifecycleQueryRepository(Protocol):
    """Read-only persistence operations for staff Round lifecycle interactions."""

    def list_round_lifecycle_target_page_source(
        self,
        *,
        action: Win5RoundLifecycleAction,
        offset: int,
        limit: int,
    ) -> Win5RoundLifecycleTargetPageSource: ...

    def get_round_lifecycle_target_source(
        self,
        *,
        round_id: int,
    ) -> Win5RoundLifecycleTargetSource | None: ...


class Win5StaffRoundLifecycleQueryUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only staff lifecycle projections."""

    @property
    def win5_staff_round_lifecycle_queries(self) -> Win5StaffRoundLifecycleQueryRepository: ...


@dataclass(frozen=True, slots=True)
class Win5StaffRoundLifecycleQueries:
    """Application entry point for paged lifecycle targets and fresh previews."""

    query_runner: QueryRunner[Win5StaffRoundLifecycleQueryUnitOfWork]

    def list_round_lifecycle_targets(
        self,
        *,
        action: Win5RoundLifecycleAction,
        offset: int = 0,
        limit: int = 25,
    ) -> Win5RoundLifecycleTargetPage:
        canonical_action = Win5RoundLifecycleAction(action)
        _require_non_negative_int(offset, field_name="offset")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")
        source = self.query_runner.run(
            lambda unit_of_work: (
                unit_of_work.win5_staff_round_lifecycle_queries.list_round_lifecycle_target_page_source(
                    action=canonical_action,
                    offset=offset,
                    limit=limit + 1,
                )
            )
        )
        if canonical_action == Win5RoundLifecycleAction.OPEN and source.has_active_open_overflow:
            raise Win5StaffRoundLifecycleInvalidSourceError("An active Season contains more than 25 open Rounds.")
        expected_status = (
            Win5RoundStatus.SETUP if canonical_action == Win5RoundLifecycleAction.OPEN else Win5RoundStatus.OPEN
        )
        for choice in source.choices:
            if choice.round_status != expected_status:
                raise Win5StaffRoundLifecycleInvalidSourceError("Lifecycle selector returned an ineligible Round.")
            if canonical_action == Win5RoundLifecycleAction.OPEN and choice.season_status != Win5SeasonStatus.ACTIVE:
                raise Win5StaffRoundLifecycleInvalidSourceError("Round-open selector returned a non-active Season.")
            if canonical_action == Win5RoundLifecycleAction.OPEN:
                try:
                    validate_win5_round_open_readiness(
                        round_type=choice.round_type,
                        race_count=choice.race_count,
                        race_entry_count=choice.race_entry_count,
                        result_count=choice.result_count,
                    )
                except Win5DomainError as exc:
                    raise Win5StaffRoundLifecycleInvalidSourceError(
                        "Round-open selector returned an unready Round."
                    ) from exc
                if choice.season_open_round_count >= MAX_OPEN_WIN5_ROUNDS_PER_ACTIVE_SEASON:
                    raise Win5StaffRoundLifecycleInvalidSourceError(
                        "Round-open selector returned a Season without an open slot."
                    )
        choices = source.choices[:limit]
        return Win5RoundLifecycleTargetPage(
            action=canonical_action,
            offset=offset,
            limit=limit,
            choices=choices,
            has_previous=offset > 0,
            has_next=len(source.choices) > limit,
        )

    def get_round_lifecycle_target(
        self,
        *,
        round_id: int,
        expected_action: Win5RoundLifecycleAction,
    ) -> Win5RoundLifecycleTarget:
        _require_positive_int(round_id, field_name="round_id")
        canonical_action = Win5RoundLifecycleAction(expected_action)

        def query(unit_of_work: Win5StaffRoundLifecycleQueryUnitOfWork) -> Win5RoundLifecycleTarget:
            try:
                source = unit_of_work.win5_staff_round_lifecycle_queries.get_round_lifecycle_target_source(
                    round_id=round_id
                )
            except (TypeError, ValueError) as exc:
                raise Win5StaffRoundLifecycleInvalidSourceError("Stored Round lifecycle facts are malformed.") from exc
            if source is None:
                raise Win5StaffRoundLifecycleUnavailableError("WIN5 Round does not exist.")
            return self._build_target(source=source, action=canonical_action)

        return self.query_runner.run(query)

    @staticmethod
    def _build_target(
        *,
        source: Win5RoundLifecycleTargetSource,
        action: Win5RoundLifecycleAction,
    ) -> Win5RoundLifecycleTarget:
        if action == Win5RoundLifecycleAction.OPEN and source.has_active_open_overflow:
            raise Win5StaffRoundLifecycleInvalidSourceError("An active Season contains more than 25 open Rounds.")
        if action == Win5RoundLifecycleAction.OPEN:
            if source.season_status != Win5SeasonStatus.ACTIVE or source.round_status != Win5RoundStatus.SETUP:
                raise Win5StaffRoundLifecycleUnavailableError(
                    "Round opening requires a setup Round in an active Season."
                )
            try:
                validate_win5_round_open_readiness(
                    round_type=source.round_type,
                    race_count=source.race_count,
                    race_entry_count=source.race_entry_count,
                    result_count=source.result_count,
                )
            except Win5DomainError as exc:
                raise Win5StaffRoundLifecycleInvalidSourceError(str(exc)) from exc
            if source.season_open_round_count >= MAX_OPEN_WIN5_ROUNDS_PER_ACTIVE_SEASON:
                raise Win5StaffRoundOpenLimitError("The active Season already has 25 open Rounds.")
        elif source.round_status != Win5RoundStatus.OPEN:
            raise Win5StaffRoundLifecycleUnavailableError("Round closing requires an open Round.")

        return Win5RoundLifecycleTarget(
            action=action,
            season_id=source.season_id,
            season_name=source.season_name,
            round_id=source.round_id,
            round_name=source.round_name,
            round_type=source.round_type,
            current_status=source.round_status,
            race_count=source.race_count,
            race_entry_count=source.race_entry_count,
            accepted_submission_count=source.accepted_submission_count,
            season_open_round_count=source.season_open_round_count,
        )
