"""Read-only staff projections for Special WIN5 Race-void interactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.win5 import (
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    fingerprint_special_void_state,
)
from uma_st2.shared import normalize_utc_datetime


class Win5StaffSpecialVoidQueryError(ValueError):
    """Base error for rejected staff Special void queries."""


class Win5StaffSpecialVoidUnavailableError(Win5StaffSpecialVoidQueryError):
    """The selected Round no longer permits void changes."""


class Win5StaffSpecialVoidInvalidSourceError(Win5StaffSpecialVoidQueryError):
    """Stored Race, Result, or void facts are malformed."""


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
class Win5StaffSpecialVoidTargetChoice:
    """One bounded closed Special Round choice."""

    season_id: int
    season_name: str
    round_id: int
    round_name: str
    race_count: int
    void_count: int

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_positive_int(self.race_count, field_name="race_count")
        _require_non_negative_int(self.void_count, field_name="void_count")
        if self.void_count >= self.race_count:
            raise ValueError("A mutable Special void target cannot already be all-void.")
        _require_bounded_string(self.season_name, field_name="season_name", max_length=100)
        _require_bounded_string(self.round_name, field_name="round_name", max_length=100)


@dataclass(frozen=True, slots=True)
class Win5StaffSpecialVoidRace:
    """One ordered Race and its optional explicit current void fact."""

    id: int
    name: str
    void_reason: str | None = None
    voided_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_bounded_string(self.name, field_name="name", max_length=200)
        if (self.void_reason is None) != (self.voided_at is None):
            raise ValueError("Special Race void reason/time must both be present or absent.")
        if self.void_reason is not None:
            _require_bounded_string(self.void_reason, field_name="void_reason", max_length=255)
            object.__setattr__(self, "void_reason", self.void_reason.strip())
        if self.voided_at is not None:
            object.__setattr__(
                self,
                "voided_at",
                normalize_utc_datetime(self.voided_at, field_name="voided_at"),
            )

    @property
    def is_void(self) -> bool:
        return self.void_reason is not None


@dataclass(frozen=True, slots=True)
class Win5StaffSpecialVoidTargetSource:
    """Persistence source revalidated by the Application query."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    round_id: int
    round_name: str
    round_type: Win5RoundType
    round_status: Win5RoundStatus
    result_count: int
    has_score_events: bool
    races: tuple[Win5StaffSpecialVoidRace, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5StaffSpecialVoidTarget:
    """Complete closed-session Special void editor state."""

    season_id: int
    season_name: str
    round_id: int
    round_name: str
    races: tuple[Win5StaffSpecialVoidRace, ...]
    result_count: int
    void_fingerprint: str

    @property
    def choice(self) -> Win5StaffSpecialVoidTargetChoice:
        return Win5StaffSpecialVoidTargetChoice(
            season_id=self.season_id,
            season_name=self.season_name,
            round_id=self.round_id,
            round_name=self.round_name,
            race_count=len(self.races),
            void_count=sum(race.is_void for race in self.races),
        )


class Win5StaffSpecialVoidQueryRepository(Protocol):
    """Read-only persistence operations for Special void interactions."""

    def list_target_choices(self, *, limit: int) -> tuple[Win5StaffSpecialVoidTargetChoice, ...]: ...

    def get_target_source(self, *, round_id: int) -> Win5StaffSpecialVoidTargetSource | None: ...


class Win5StaffSpecialVoidQueryUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only Special void query persistence."""

    @property
    def win5_staff_special_void_queries(self) -> Win5StaffSpecialVoidQueryRepository: ...


@dataclass(frozen=True, slots=True)
class Win5StaffSpecialVoidQueries:
    """Application entry point for bounded Special void targets."""

    query_runner: QueryRunner[Win5StaffSpecialVoidQueryUnitOfWork]

    def list_targets(self, *, limit: int = 25) -> tuple[Win5StaffSpecialVoidTargetChoice, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")
        return self.query_runner.run(
            lambda unit_of_work: unit_of_work.win5_staff_special_void_queries.list_target_choices(limit=limit)
        )

    def get_target(self, *, round_id: int) -> Win5StaffSpecialVoidTarget:
        _require_positive_int(round_id, field_name="round_id")

        def query(unit_of_work: Win5StaffSpecialVoidQueryUnitOfWork) -> Win5StaffSpecialVoidTarget:
            try:
                source = unit_of_work.win5_staff_special_void_queries.get_target_source(round_id=round_id)
            except (TypeError, ValueError) as exc:
                raise Win5StaffSpecialVoidInvalidSourceError("Stored Special void target facts are malformed.") from exc
            if source is None:
                raise Win5StaffSpecialVoidUnavailableError("Special void target does not exist.")
            return self._build_target(source=source)

        return self.query_runner.run(query)

    @staticmethod
    def _build_target(*, source: Win5StaffSpecialVoidTargetSource) -> Win5StaffSpecialVoidTarget:
        if (
            source.season_status != Win5SeasonStatus.ACTIVE
            or source.round_type != Win5RoundType.SPECIAL
            or source.round_status != Win5RoundStatus.CLOSED
        ):
            raise Win5StaffSpecialVoidUnavailableError(
                "Special void target must be a closed Special Round in an active Season."
            )
        if source.has_score_events:
            raise Win5StaffSpecialVoidUnavailableError("A score-backed Round has immutable void facts.")

        races = tuple(sorted(source.races, key=lambda race: race.id))
        if not races or len({race.id for race in races}) != len(races):
            raise Win5StaffSpecialVoidInvalidSourceError(
                "Special void target requires one or more unique canonical Races."
            )
        void_ids = tuple(race.id for race in races if race.is_void)
        if len(void_ids) == len(races):
            raise Win5StaffSpecialVoidInvalidSourceError("An all-void Special Round must already be cancelled.")
        _require_non_negative_int(source.result_count, field_name="result_count")
        non_void_count = len(races) - len(void_ids)
        if source.result_count not in {0, non_void_count}:
            raise Win5StaffSpecialVoidInvalidSourceError(
                "Special void target Result must be empty or complete for every non-void Race."
            )
        return Win5StaffSpecialVoidTarget(
            season_id=source.season_id,
            season_name=source.season_name,
            round_id=source.round_id,
            round_name=source.round_name,
            races=races,
            result_count=source.result_count,
            void_fingerprint=fingerprint_special_void_state(void_ids),
        )
