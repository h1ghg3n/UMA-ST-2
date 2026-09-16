"""Pure WIN5 domain entities and immutable value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import (
    Win5RoundStatus,
    Win5RoundType,
    Win5SubmissionStatus,
    Win5SubmissionTier,
)
from .errors import Win5DomainError

EntityId = int | str


def _normalize_enum(
    value: Win5RoundType | Win5RoundStatus | Win5SubmissionTier | Win5SubmissionStatus | str,
    enum_type: Any,
    *,
    field_name: str,
) -> Any:
    if isinstance(value, enum_type):
        return value

    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as exc:
            raise Win5DomainError(f"{field_name} must be canonical: {value!r}.") from exc

    raise Win5DomainError(f"{field_name} must be a canonical enum value, got {type(value)!r}.")


@dataclass(frozen=True, slots=True)
class Win5RaceEntry:
    """Gate/name entry for a WIN5 race.

    Normal rounds use entries as the canonical pick/result identity. Special
    rounds may carry entries only as optional presentation references.
    """

    id: EntityId
    race_id: EntityId
    gate_number: int
    name: str


@dataclass(frozen=True, slots=True)
class Win5Race:
    """Canonical WIN5 race and optional entry/reference payload."""

    id: EntityId
    name: str
    entries: tuple[Win5RaceEntry, ...] = field(default_factory=tuple)

    @property
    def entries_ordered_by_gate(self) -> tuple[Win5RaceEntry, ...]:
        """Return race entries ordered by gate number without mutating storage."""

        return tuple(sorted(self.entries, key=lambda entry: entry.gate_number))


@dataclass(frozen=True, slots=True)
class Win5Round:
    """Round root object with frozen in-memory race payload."""

    id: EntityId
    season_id: EntityId
    name: str
    type: Win5RoundType
    status: Win5RoundStatus
    races: tuple[Win5Race, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 100:
            raise Win5DomainError("name must be a non-empty string of at most 100 characters.")
        object.__setattr__(self, "type", _normalize_enum(self.type, Win5RoundType, field_name="type"))
        object.__setattr__(self, "status", _normalize_enum(self.status, Win5RoundStatus, field_name="status"))


@dataclass(frozen=True, slots=True)
class Win5SubmissionPick:
    """One pick in a submission.

    Normal picks use ``race_entry_id`` and no ``gate_number``. Special picks
    use ``gate_number`` and no ``race_entry_id``. Missing slots/races are
    represented by the absence of a pick rather than placeholder objects.
    """

    id: EntityId
    submission_id: EntityId
    race_id: EntityId
    race_entry_id: EntityId | None
    position: int
    gate_number: int | None = None


@dataclass(frozen=True, slots=True)
class Win5Submission:
    """Submission aggregate captured by persona per round."""

    id: EntityId
    round_id: EntityId
    persona_id: str
    tier: Win5SubmissionTier
    status: Win5SubmissionStatus
    picks: tuple[Win5SubmissionPick, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "tier",
            _normalize_enum(self.tier, Win5SubmissionTier, field_name="tier"),
        )
        object.__setattr__(
            self,
            "status",
            _normalize_enum(self.status, Win5SubmissionStatus, field_name="status"),
        )


@dataclass(frozen=True, slots=True)
class Win5Placement:
    """One result placement.

    Normal results use ``race_entry_id``. A Special result is a single
    position-1 placement carrying the winner ``gate_number`` instead.
    """

    race_entry_id: EntityId | None
    position: int
    gate_number: int | None = None


@dataclass(frozen=True, slots=True)
class Win5RaceResult:
    """Result collection for a WIN5 race."""

    race_id: EntityId
    placements: tuple[Win5Placement, ...] = field(default_factory=tuple)
