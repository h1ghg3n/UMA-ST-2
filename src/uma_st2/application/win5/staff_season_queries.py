"""Read-only staff projections for WIN5 Season management interactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.win5 import Win5SeasonStatus

from .season_lifecycle import (
    Win5SeasonAction,
    Win5SeasonRoundState,
    Win5SeasonSnapshot,
)


class Win5StaffSeasonQueryError(ValueError):
    """Base error for expected staff Season-query rejection."""


class Win5StaffSeasonUnavailableError(Win5StaffSeasonQueryError):
    """The requested Season no longer permits the selected action."""


class Win5StaffSeasonInvalidSourceError(Win5StaffSeasonQueryError):
    """Stored Season facts cannot form a safe staff interaction."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _require_non_negative_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


@dataclass(frozen=True, slots=True)
class Win5SeasonTargetPage:
    """Closed-session paged Season choices for one operator action."""

    action: Win5SeasonAction
    offset: int
    limit: int
    choices: tuple[Win5SeasonSnapshot, ...] = field(default_factory=tuple)
    has_previous: bool = False
    has_next: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", Win5SeasonAction(self.action))
        if self.action == Win5SeasonAction.CREATE:
            raise ValueError("Season creation has no target page.")
        _require_non_negative_int(self.offset, field_name="offset")
        _require_positive_int(self.limit, field_name="limit")


class Win5StaffSeasonQueryRepository(Protocol):
    """Read-only persistence operations for Season selectors and previews."""

    def list_season_targets(
        self,
        *,
        action: Win5SeasonAction,
        offset: int,
        limit: int,
    ) -> tuple[Win5SeasonSnapshot, ...]: ...

    def get_season_target(self, *, season_id: int) -> Win5SeasonSnapshot | None: ...


class Win5StaffSeasonQueryUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only staff Season projections."""

    @property
    def win5_staff_season_queries(self) -> Win5StaffSeasonQueryRepository: ...


@dataclass(frozen=True, slots=True)
class Win5StaffSeasonQueries:
    """Application entry point for paged Season targets and fresh previews."""

    query_runner: QueryRunner[Win5StaffSeasonQueryUnitOfWork]

    def list_season_targets(
        self,
        *,
        action: Win5SeasonAction,
        offset: int = 0,
        limit: int = 25,
    ) -> Win5SeasonTargetPage:
        canonical_action = Win5SeasonAction(action)
        if canonical_action == Win5SeasonAction.CREATE:
            raise ValueError("Season creation has no target selector.")
        _require_non_negative_int(offset, field_name="offset")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")
        try:
            choices = self.query_runner.run(
                lambda uow: uow.win5_staff_season_queries.list_season_targets(
                    action=canonical_action,
                    offset=offset,
                    limit=limit + 1,
                )
            )
            for choice in choices:
                _validate_action_target(action=canonical_action, target=choice)
        except Win5StaffSeasonQueryError:
            raise
        except (TypeError, ValueError) as exc:
            raise Win5StaffSeasonInvalidSourceError("Stored WIN5 Season facts are malformed.") from exc
        return Win5SeasonTargetPage(
            action=canonical_action,
            offset=offset,
            limit=limit,
            choices=choices[:limit],
            has_previous=offset > 0,
            has_next=len(choices) > limit,
        )

    def get_season_target(
        self,
        *,
        season_id: int,
        expected_action: Win5SeasonAction,
    ) -> Win5SeasonSnapshot:
        _require_positive_int(season_id, field_name="season_id")
        action = Win5SeasonAction(expected_action)
        if action == Win5SeasonAction.CREATE:
            raise ValueError("Season creation has no existing target.")

        def query(unit_of_work: Win5StaffSeasonQueryUnitOfWork) -> Win5SeasonSnapshot:
            try:
                target = unit_of_work.win5_staff_season_queries.get_season_target(season_id=season_id)
                if target is None:
                    raise Win5StaffSeasonUnavailableError("WIN5 Season does not exist.")
                _validate_action_target(action=action, target=target)
                return target
            except Win5StaffSeasonQueryError:
                raise
            except (TypeError, ValueError) as exc:
                raise Win5StaffSeasonInvalidSourceError("Stored WIN5 Season facts are malformed.") from exc

        return self.query_runner.run(query)


def _validate_action_target(*, action: Win5SeasonAction, target: Win5SeasonSnapshot) -> None:
    if not isinstance(target, Win5SeasonSnapshot) or not isinstance(target.rounds, Win5SeasonRoundState):
        raise Win5StaffSeasonInvalidSourceError("Season projection is malformed.")
    if action == Win5SeasonAction.EDIT:
        return
    if action == Win5SeasonAction.ACTIVATE:
        eligible = target.status == Win5SeasonStatus.DRAFT and target.rounds.total == target.rounds.setup
    elif action == Win5SeasonAction.CLOSE:
        eligible = target.status == Win5SeasonStatus.ACTIVE and (
            target.rounds.total == target.rounds.scored + target.rounds.cancelled
        )
    elif action == Win5SeasonAction.CANCEL:
        eligible = target.status == Win5SeasonStatus.DRAFT and target.rounds.total == 0
    else:
        eligible = False
    if not eligible:
        raise Win5StaffSeasonUnavailableError("WIN5 Season is no longer eligible for the selected action.")
