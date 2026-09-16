"""Read-only staff projections for authoritative WIN5 result interactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.win5 import (
    Win5DomainError,
    Win5NormalResultPlacement,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SpecialResultWinner,
    fingerprint_normal_result,
    fingerprint_special_result,
)
from uma_st2.shared import normalize_utc_datetime


class Win5NormalResultTargetMode(StrEnum):
    """Staff interaction lanes for an empty or complete Normal result."""

    ENTRY = "entry"
    CORRECTION = "correction"


class Win5SpecialResultTargetMode(StrEnum):
    """Staff interaction lanes for an empty or complete Special result."""

    ENTRY = "entry"
    CORRECTION = "correction"


class Win5StaffResultQueryError(ValueError):
    """Base error for expected staff result-query rejection."""


class Win5StaffResultTargetUnavailableError(Win5StaffResultQueryError):
    """The requested Round is no longer eligible for the requested lane."""


class Win5StaffResultInvalidSourceError(Win5StaffResultQueryError):
    """Stored target facts cannot form a safe Normal result editor."""


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
class Win5NormalResultTargetChoice:
    """One bounded Round/Race choice for a staff result interaction."""

    season_id: int
    season_name: str
    round_id: int
    round_name: str
    race_id: int
    race_name: str
    mode: Win5NormalResultTargetMode

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_positive_int(self.race_id, field_name="race_id")
        _require_bounded_string(self.season_name, field_name="season_name", max_length=100)
        _require_bounded_string(self.round_name, field_name="round_name", max_length=100)
        _require_bounded_string(self.race_name, field_name="race_name", max_length=200)
        object.__setattr__(self, "mode", Win5NormalResultTargetMode(self.mode))


@dataclass(frozen=True, slots=True)
class Win5NormalResultEntryOption:
    """One canonical RaceEntry available to the staff result editor."""

    id: int
    gate_number: int
    name: str

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.gate_number, field_name="gate_number")
        _require_bounded_string(self.name, field_name="name", max_length=100)


@dataclass(frozen=True, slots=True)
class Win5NormalResultTargetSource:
    """Persistence projection revalidated by the staff query application."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    round_id: int
    round_name: str
    round_type: Win5RoundType
    round_status: Win5RoundStatus
    race_id: int
    race_name: str
    entries: tuple[Win5NormalResultEntryOption, ...] = field(default_factory=tuple)
    placements: tuple[Win5NormalResultPlacement, ...] = field(default_factory=tuple)
    has_score_events: bool = False


@dataclass(frozen=True, slots=True)
class Win5NormalResultTarget:
    """Complete closed-session editor state for one Normal result target."""

    season_id: int
    season_name: str
    round_id: int
    round_name: str
    race_id: int
    race_name: str
    mode: Win5NormalResultTargetMode
    entries: tuple[Win5NormalResultEntryOption, ...] = field(default_factory=tuple)
    current_placements: tuple[Win5NormalResultPlacement, ...] = field(default_factory=tuple)
    result_fingerprint: str | None = None

    @property
    def choice(self) -> Win5NormalResultTargetChoice:
        return Win5NormalResultTargetChoice(
            season_id=self.season_id,
            season_name=self.season_name,
            round_id=self.round_id,
            round_name=self.round_name,
            race_id=self.race_id,
            race_name=self.race_name,
            mode=self.mode,
        )


@dataclass(frozen=True, slots=True)
class Win5SpecialResultTargetChoice:
    """One bounded Special Round choice for a staff result interaction."""

    season_id: int
    season_name: str
    round_id: int
    round_name: str
    race_count: int
    mode: Win5SpecialResultTargetMode
    void_count: int = 0

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_positive_int(self.race_count, field_name="race_count")
        _require_non_negative_int(self.void_count, field_name="void_count")
        if self.void_count >= self.race_count:
            raise ValueError("A Special result target must have at least one non-void Race.")
        _require_bounded_string(self.season_name, field_name="season_name", max_length=100)
        _require_bounded_string(self.round_name, field_name="round_name", max_length=100)
        object.__setattr__(self, "mode", Win5SpecialResultTargetMode(self.mode))


@dataclass(frozen=True, slots=True)
class Win5SpecialResultReferenceEntry:
    """Optional non-authoritative name reference for one Special gate."""

    id: int
    gate_number: int
    name: str

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.gate_number, field_name="gate_number")
        _require_bounded_string(self.name, field_name="name", max_length=100)


@dataclass(frozen=True, slots=True)
class Win5SpecialResultRace:
    """One ordered Special Race, optional references, and explicit void fact."""

    id: int
    name: str
    entries: tuple[Win5SpecialResultReferenceEntry, ...] = field(default_factory=tuple)
    void_reason: str | None = None
    voided_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_bounded_string(self.name, field_name="name", max_length=200)
        object.__setattr__(self, "entries", tuple(self.entries))
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
class Win5SpecialResultTargetSource:
    """Persistence projection revalidated by the Special result application."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    round_id: int
    round_name: str
    round_type: Win5RoundType
    round_status: Win5RoundStatus
    races: tuple[Win5SpecialResultRace, ...] = field(default_factory=tuple)
    winners: tuple[Win5SpecialResultWinner, ...] = field(default_factory=tuple)
    has_score_events: bool = False


@dataclass(frozen=True, slots=True)
class Win5SpecialResultTarget:
    """Complete closed-session editor state for one Special result bundle."""

    season_id: int
    season_name: str
    round_id: int
    round_name: str
    mode: Win5SpecialResultTargetMode
    races: tuple[Win5SpecialResultRace, ...] = field(default_factory=tuple)
    current_winners: tuple[Win5SpecialResultWinner, ...] = field(default_factory=tuple)
    result_fingerprint: str | None = None

    @property
    def non_void_races(self) -> tuple[Win5SpecialResultRace, ...]:
        return tuple(race for race in self.races if not race.is_void)

    @property
    def void_count(self) -> int:
        return sum(race.is_void for race in self.races)

    @property
    def choice(self) -> Win5SpecialResultTargetChoice:
        return Win5SpecialResultTargetChoice(
            season_id=self.season_id,
            season_name=self.season_name,
            round_id=self.round_id,
            round_name=self.round_name,
            race_count=len(self.races),
            mode=self.mode,
            void_count=self.void_count,
        )


class Win5StaffResultQueryRepository(Protocol):
    """Read-only persistence operations for staff result interactions."""

    def list_normal_result_target_choices(
        self,
        *,
        mode: Win5NormalResultTargetMode,
        limit: int,
    ) -> tuple[Win5NormalResultTargetChoice, ...]: ...

    def get_normal_result_target_source(
        self,
        *,
        round_id: int,
    ) -> Win5NormalResultTargetSource | None: ...

    def list_special_result_target_choices(
        self,
        *,
        mode: Win5SpecialResultTargetMode,
        limit: int,
    ) -> tuple[Win5SpecialResultTargetChoice, ...]: ...

    def get_special_result_target_source(
        self,
        *,
        round_id: int,
    ) -> Win5SpecialResultTargetSource | None: ...


class Win5StaffResultQueryUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only its read-only staff result repository."""

    @property
    def win5_staff_result_queries(self) -> Win5StaffResultQueryRepository: ...


@dataclass(frozen=True, slots=True)
class Win5StaffResultQueries:
    """Application entry point for bounded staff result queries."""

    query_runner: QueryRunner[Win5StaffResultQueryUnitOfWork]

    def list_normal_result_targets(
        self,
        *,
        mode: Win5NormalResultTargetMode,
        limit: int = 25,
    ) -> tuple[Win5NormalResultTargetChoice, ...]:
        try:
            canonical_mode = Win5NormalResultTargetMode(mode)
        except ValueError as exc:
            raise ValueError("mode must be 'entry' or 'correction'.") from exc
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")
        return self.query_runner.run(
            lambda unit_of_work: unit_of_work.win5_staff_result_queries.list_normal_result_target_choices(
                mode=canonical_mode,
                limit=limit,
            )
        )

    def get_normal_result_target(
        self,
        *,
        round_id: int,
        expected_mode: Win5NormalResultTargetMode,
    ) -> Win5NormalResultTarget:
        _require_positive_int(round_id, field_name="round_id")
        try:
            canonical_mode = Win5NormalResultTargetMode(expected_mode)
        except ValueError as exc:
            raise ValueError("expected_mode must be 'entry' or 'correction'.") from exc

        def query(unit_of_work: Win5StaffResultQueryUnitOfWork) -> Win5NormalResultTarget:
            try:
                source = unit_of_work.win5_staff_result_queries.get_normal_result_target_source(round_id=round_id)
            except (TypeError, ValueError) as exc:
                raise Win5StaffResultInvalidSourceError("Stored Normal result target facts are malformed.") from exc
            if source is None:
                raise Win5StaffResultTargetUnavailableError("Normal result target does not exist.")
            return self._build_target(source=source, expected_mode=canonical_mode)

        return self.query_runner.run(query)

    def list_special_result_targets(
        self,
        *,
        mode: Win5SpecialResultTargetMode,
        limit: int = 25,
    ) -> tuple[Win5SpecialResultTargetChoice, ...]:
        try:
            canonical_mode = Win5SpecialResultTargetMode(mode)
        except ValueError as exc:
            raise ValueError("mode must be 'entry' or 'correction'.") from exc
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")
        return self.query_runner.run(
            lambda unit_of_work: unit_of_work.win5_staff_result_queries.list_special_result_target_choices(
                mode=canonical_mode,
                limit=limit,
            )
        )

    def get_special_result_target(
        self,
        *,
        round_id: int,
        expected_mode: Win5SpecialResultTargetMode,
    ) -> Win5SpecialResultTarget:
        _require_positive_int(round_id, field_name="round_id")
        try:
            canonical_mode = Win5SpecialResultTargetMode(expected_mode)
        except ValueError as exc:
            raise ValueError("expected_mode must be 'entry' or 'correction'.") from exc

        def query(unit_of_work: Win5StaffResultQueryUnitOfWork) -> Win5SpecialResultTarget:
            try:
                source = unit_of_work.win5_staff_result_queries.get_special_result_target_source(round_id=round_id)
            except (TypeError, ValueError) as exc:
                raise Win5StaffResultInvalidSourceError("Stored Special result target facts are malformed.") from exc
            if source is None:
                raise Win5StaffResultTargetUnavailableError("Special result target does not exist.")
            return self._build_special_target(source=source, expected_mode=canonical_mode)

        return self.query_runner.run(query)

    @staticmethod
    def _build_target(
        *,
        source: Win5NormalResultTargetSource,
        expected_mode: Win5NormalResultTargetMode,
    ) -> Win5NormalResultTarget:
        if (
            source.season_status != Win5SeasonStatus.ACTIVE
            or source.round_type != Win5RoundType.NORMAL
            or source.round_status != Win5RoundStatus.CLOSED
        ):
            raise Win5StaffResultTargetUnavailableError(
                "Normal result target must be a closed Round in an active Season."
            )
        if source.has_score_events:
            raise Win5StaffResultTargetUnavailableError("A score-backed Round has immutable Results.")

        entries = tuple(sorted(source.entries, key=lambda entry: (entry.gate_number, entry.id)))
        if (
            len(entries) < 5
            or len({entry.id for entry in entries}) != len(entries)
            or len({entry.gate_number for entry in entries}) != len(entries)
        ):
            raise Win5StaffResultInvalidSourceError(
                "Normal result target requires at least five unique canonical RaceEntries."
            )

        placements = tuple(sorted(source.placements, key=lambda placement: placement.position))
        if not placements:
            mode = Win5NormalResultTargetMode.ENTRY
            result_fingerprint = None
        else:
            try:
                result_fingerprint = fingerprint_normal_result(placements)
            except Win5DomainError as exc:
                raise Win5StaffResultInvalidSourceError(
                    "Stored Normal result must be a complete canonical first-through-fifth board."
                ) from exc
            if any(placement.race_entry_id not in {entry.id for entry in entries} for placement in placements):
                raise Win5StaffResultInvalidSourceError(
                    "Stored Normal result references an entry outside the target Race."
                )
            mode = Win5NormalResultTargetMode.CORRECTION

        if mode != expected_mode:
            raise Win5StaffResultTargetUnavailableError(
                "Normal result target changed state; reopen the staff interaction."
            )
        return Win5NormalResultTarget(
            season_id=source.season_id,
            season_name=source.season_name,
            round_id=source.round_id,
            round_name=source.round_name,
            race_id=source.race_id,
            race_name=source.race_name,
            mode=mode,
            entries=entries,
            current_placements=placements,
            result_fingerprint=result_fingerprint,
        )

    @staticmethod
    def _build_special_target(
        *,
        source: Win5SpecialResultTargetSource,
        expected_mode: Win5SpecialResultTargetMode,
    ) -> Win5SpecialResultTarget:
        if (
            source.season_status != Win5SeasonStatus.ACTIVE
            or source.round_type != Win5RoundType.SPECIAL
            or source.round_status != Win5RoundStatus.CLOSED
        ):
            raise Win5StaffResultTargetUnavailableError(
                "Special result target must be a closed Round in an active Season."
            )
        if source.has_score_events:
            raise Win5StaffResultTargetUnavailableError("A score-backed Round has immutable Results.")
        races = tuple(sorted(source.races, key=lambda race: race.id))
        if not races or len({race.id for race in races}) != len(races):
            raise Win5StaffResultInvalidSourceError("Special result target requires one or more unique Races.")
        canonical_races: list[Win5SpecialResultRace] = []
        for race in races:
            entries = tuple(sorted(race.entries, key=lambda entry: (entry.gate_number, entry.id)))
            if len({entry.id for entry in entries}) != len(entries) or len(
                {entry.gate_number for entry in entries}
            ) != len(entries):
                raise Win5StaffResultInvalidSourceError(
                    "Special reference entries must have unique IDs and gate numbers within each Race."
                )
            canonical_races.append(
                Win5SpecialResultRace(
                    id=race.id,
                    name=race.name,
                    entries=entries,
                    void_reason=race.void_reason,
                    voided_at=race.voided_at,
                )
            )

        non_void_race_ids = {race.id for race in canonical_races if not race.is_void}
        if not non_void_race_ids:
            raise Win5StaffResultInvalidSourceError("An all-void Special Round must already be cancelled.")

        winners = tuple(sorted(source.winners, key=lambda winner: winner.race_id))
        if not winners:
            mode = Win5SpecialResultTargetMode.ENTRY
            result_fingerprint = None
        else:
            if len(winners) != len(non_void_race_ids) or {winner.race_id for winner in winners} != non_void_race_ids:
                raise Win5StaffResultInvalidSourceError(
                    "Stored Special result must contain exactly one winner for every non-void Race."
                )
            try:
                result_fingerprint = fingerprint_special_result(winners)
            except Win5DomainError as exc:
                raise Win5StaffResultInvalidSourceError(
                    "Stored Special result must be one complete canonical winner bundle."
                ) from exc
            mode = Win5SpecialResultTargetMode.CORRECTION

        if mode != expected_mode:
            raise Win5StaffResultTargetUnavailableError(
                "Special result target changed state; reopen the staff interaction."
            )
        return Win5SpecialResultTarget(
            season_id=source.season_id,
            season_name=source.season_name,
            round_id=source.round_id,
            round_name=source.round_name,
            mode=mode,
            races=tuple(canonical_races),
            current_winners=winners,
            result_fingerprint=result_fingerprint,
        )
