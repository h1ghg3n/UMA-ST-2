"""WIN5 Normal/Special Round creation application boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.win5 import (
    MIN_NORMAL_WIN5_RACE_ENTRIES,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
)
from uma_st2.shared import normalize_utc_datetime

WIN5_ROUND_CREATION_AUDIT_SCHEMA_VERSION: Final = 2


class Win5RoundCreationAuditType(StrEnum):
    """Canonical operation types for newly created Round graphs."""

    NORMAL_CREATED = "normal_round_created"
    SPECIAL_CREATED = "special_round_created"

    @classmethod
    def for_round_type(cls, round_type: Win5RoundType) -> Win5RoundCreationAuditType:
        if round_type == Win5RoundType.NORMAL:
            return cls.NORMAL_CREATED
        return cls.SPECIAL_CREATED


class Win5RoundCreationError(ValueError):
    """Base error for rejected Round creation commands."""


class Win5RoundCreationUnavailableError(Win5RoundCreationError):
    """The selected Season no longer permits Round creation."""


class Win5RoundCreationIdempotencyConflictError(Win5RoundCreationError):
    """An idempotency key is bound to another logical command."""


class Win5RoundCreationAuditError(Win5RoundCreationError):
    """Stored exact-retry audit data is absent or malformed."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _normalized_bounded_string(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must be a non-empty string of at most {max_length} characters.")
    return normalized


def _payload_positive_int(payload: Mapping[str, object], key: str) -> int:
    value = payload[key]
    _require_positive_int(value, field_name=key)  # type: ignore[arg-type]
    return value  # type: ignore[return-value]


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


def _optional_datetime_payload(value: datetime | None) -> str | None:
    if value is None:
        return None
    return normalize_utc_datetime(value, field_name="scheduled_at").isoformat()


def _datetime_from_payload(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("scheduled_at must be an ISO datetime string or null.")
    return normalize_utc_datetime(datetime.fromisoformat(value), field_name="scheduled_at")


@dataclass(frozen=True, slots=True)
class Win5RoundCreationEntryInput:
    """One canonical gate/name Entry requested for a Normal Race."""

    gate_number: int
    name: str

    def __post_init__(self) -> None:
        _require_positive_int(self.gate_number, field_name="gate_number")
        object.__setattr__(
            self,
            "name",
            _normalized_bounded_string(self.name, field_name="entry name", max_length=100),
        )


@dataclass(frozen=True, slots=True)
class Win5RoundCreationRaceInput:
    """One ordered Race requested as part of a new Round."""

    name: str
    scheduled_at: datetime | None = None
    entries: tuple[Win5RoundCreationEntryInput, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "name",
            _normalized_bounded_string(self.name, field_name="race name", max_length=200),
        )
        if self.scheduled_at is not None:
            object.__setattr__(
                self,
                "scheduled_at",
                normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
            )
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, Win5RoundCreationEntryInput) for entry in self.entries
        ):
            raise ValueError("entries must be a tuple of Win5RoundCreationEntryInput values.")
        if len({entry.gate_number for entry in self.entries}) != len(self.entries):
            raise ValueError("Race Entry gate numbers must be unique.")
        object.__setattr__(
            self,
            "entries",
            tuple(sorted(self.entries, key=lambda entry: entry.gate_number)),
        )


@dataclass(frozen=True, slots=True)
class CreateWin5Round:
    """Create one setup Round and its ordered Race graph atomically."""

    season_id: int
    round_type: Win5RoundType
    round_name: str
    races: tuple[Win5RoundCreationRaceInput, ...]
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str | None = None
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        object.__setattr__(self, "round_type", Win5RoundType(self.round_type))
        object.__setattr__(
            self,
            "round_name",
            _normalized_bounded_string(self.round_name, field_name="round_name", max_length=100),
        )
        if not isinstance(self.races, tuple) or not self.races:
            raise ValueError("races must be a non-empty tuple.")
        if any(not isinstance(race, Win5RoundCreationRaceInput) for race in self.races):
            raise ValueError("races must contain Win5RoundCreationRaceInput values.")
        if self.round_type == Win5RoundType.NORMAL and len(self.races) != 1:
            raise ValueError("A Normal WIN5 Round must contain exactly one Race.")
        if self.round_type == Win5RoundType.NORMAL and len(self.races[0].entries) < MIN_NORMAL_WIN5_RACE_ENTRIES:
            raise ValueError(f"A Normal WIN5 Round must contain at least {MIN_NORMAL_WIN5_RACE_ENTRIES} Entries.")
        if self.round_type == Win5RoundType.SPECIAL and any(race.entries for race in self.races):
            raise ValueError("Special WIN5 Round creation does not accept reference Entries.")
        object.__setattr__(
            self,
            "idempotency_key",
            _normalized_bounded_string(
                self.idempotency_key,
                field_name="idempotency_key",
                max_length=128,
            ),
        )
        object.__setattr__(
            self,
            "actor_discord_user_id",
            _normalized_bounded_string(
                self.actor_discord_user_id,
                field_name="actor_discord_user_id",
                max_length=32,
            ),
        )
        object.__setattr__(
            self,
            "guild_id",
            _normalized_bounded_string(
                self.guild_id,
                field_name="guild_id",
                max_length=32,
                optional=True,
            ),
        )
        object.__setattr__(
            self,
            "correlation_id",
            _normalized_bounded_string(
                self.correlation_id,
                field_name="correlation_id",
                max_length=128,
                optional=True,
            ),
        )
        object.__setattr__(
            self,
            "reason",
            _normalized_bounded_string(
                self.reason,
                field_name="reason",
                max_length=255,
                optional=True,
            ),
        )

    @property
    def request_fingerprint(self) -> str:
        canonical = {
            "schema": "win5-round-creation-command-v2",
            "season_id": self.season_id,
            "round_type": self.round_type.value,
            "round_name": self.round_name,
            "races": [
                {
                    "name": race.name,
                    "scheduled_at": _optional_datetime_payload(race.scheduled_at),
                    "entries": [
                        {
                            "gate_number": entry.gate_number,
                            "name": entry.name,
                        }
                        for entry in race.entries
                    ],
                }
                for race in self.races
            ],
        }
        encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Win5RoundCreationSeason:
    """Locked parent Season facts used by Round creation."""

    id: int
    name: str
    status: Win5SeasonStatus

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        object.__setattr__(
            self,
            "name",
            _normalized_bounded_string(self.name, field_name="name", max_length=100),
        )
        object.__setattr__(self, "status", Win5SeasonStatus(self.status))


@dataclass(frozen=True, slots=True)
class Win5CreatedEntry:
    """One committed canonical RaceEntry returned by persistence."""

    id: int
    gate_number: int
    name: str

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.gate_number, field_name="gate_number")
        object.__setattr__(
            self,
            "name",
            _normalized_bounded_string(self.name, field_name="name", max_length=100),
        )


@dataclass(frozen=True, slots=True)
class Win5CreatedRace:
    """One committed Race identity returned by the persistence boundary."""

    id: int
    name: str
    scheduled_at: datetime | None = None
    entries: tuple[Win5CreatedEntry, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        object.__setattr__(
            self,
            "name",
            _normalized_bounded_string(self.name, field_name="name", max_length=200),
        )
        if self.scheduled_at is not None:
            object.__setattr__(
                self,
                "scheduled_at",
                normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
            )
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, Win5CreatedEntry) for entry in self.entries
        ):
            raise ValueError("entries must be a tuple of Win5CreatedEntry values.")
        if len({entry.id for entry in self.entries}) != len(self.entries):
            raise ValueError("A created Race must contain unique Entry IDs.")
        if len({entry.gate_number for entry in self.entries}) != len(self.entries):
            raise ValueError("A created Race must contain unique Entry gate numbers.")
        object.__setattr__(
            self,
            "entries",
            tuple(sorted(self.entries, key=lambda entry: entry.gate_number)),
        )


@dataclass(frozen=True, slots=True)
class Win5CreatedRoundGraph:
    """New persistence identities allocated for one Round graph."""

    round_id: int
    races: tuple[Win5CreatedRace, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_positive_int(self.round_id, field_name="round_id")
        if not self.races:
            raise ValueError("A created WIN5 Round graph must contain at least one Race.")
        if len({race.id for race in self.races}) != len(self.races):
            raise ValueError("A created WIN5 Round graph must contain unique Race IDs.")
        entry_ids = tuple(entry.id for race in self.races for entry in race.entries)
        if len(set(entry_ids)) != len(entry_ids):
            raise ValueError("A created WIN5 Round graph must contain unique Entry IDs.")


@dataclass(frozen=True, slots=True)
class Win5RoundCreationSnapshot:
    """Deterministic created graph stored in the operation audit."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    round_id: int
    round_name: str
    round_type: Win5RoundType
    round_status: Win5RoundStatus
    races: tuple[Win5CreatedRace, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        object.__setattr__(
            self,
            "season_name",
            _normalized_bounded_string(self.season_name, field_name="season_name", max_length=100),
        )
        object.__setattr__(
            self,
            "round_name",
            _normalized_bounded_string(self.round_name, field_name="round_name", max_length=100),
        )
        object.__setattr__(self, "season_status", Win5SeasonStatus(self.season_status))
        object.__setattr__(self, "round_type", Win5RoundType(self.round_type))
        object.__setattr__(self, "round_status", Win5RoundStatus(self.round_status))
        if self.round_status != Win5RoundStatus.SETUP:
            raise ValueError("A newly created WIN5 Round must be in setup status.")
        if not self.races:
            raise ValueError("A created WIN5 Round snapshot must contain at least one Race.")
        if len({race.id for race in self.races}) != len(self.races):
            raise ValueError("A created WIN5 Round snapshot must contain unique Race IDs.")
        if self.round_type == Win5RoundType.NORMAL and len(self.races) != 1:
            raise ValueError("A created Normal WIN5 Round must contain exactly one Race.")
        if self.round_type == Win5RoundType.NORMAL and len(self.races[0].entries) < MIN_NORMAL_WIN5_RACE_ENTRIES:
            raise ValueError(
                f"A created Normal WIN5 Round must contain at least {MIN_NORMAL_WIN5_RACE_ENTRIES} Entries."
            )
        if self.round_type == Win5RoundType.SPECIAL and any(race.entries for race in self.races):
            raise ValueError("A created Special WIN5 Round must not contain reference Entries.")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": WIN5_ROUND_CREATION_AUDIT_SCHEMA_VERSION,
            "season_id": self.season_id,
            "season_name": self.season_name,
            "season_status": self.season_status.value,
            "round_id": self.round_id,
            "round_name": self.round_name,
            "round_type": self.round_type.value,
            "round_status": self.round_status.value,
            "races": [
                {
                    "race_id": race.id,
                    "race_name": race.name,
                    "scheduled_at": _optional_datetime_payload(race.scheduled_at),
                    "entries": [
                        {
                            "entry_id": entry.id,
                            "gate_number": entry.gate_number,
                            "name": entry.name,
                        }
                        for entry in race.entries
                    ],
                }
                for race in self.races
            ],
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> Win5RoundCreationSnapshot:
        if _payload_positive_int(payload, "schema_version") != WIN5_ROUND_CREATION_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported WIN5 Round creation audit schema version.")
        race_payloads = payload["races"]
        if not isinstance(race_payloads, list) or not race_payloads:
            raise ValueError("races must be a non-empty list.")
        races: list[Win5CreatedRace] = []
        for race_payload in race_payloads:
            if not isinstance(race_payload, Mapping):
                raise ValueError("Each Race audit value must be an object.")
            races.append(
                Win5CreatedRace(
                    id=_payload_positive_int(race_payload, "race_id"),
                    name=_payload_string(race_payload, "race_name"),
                    scheduled_at=_datetime_from_payload(race_payload["scheduled_at"]),
                    entries=cls._entries_from_audit_payload(race_payload["entries"]),
                )
            )
        return cls(
            season_id=_payload_positive_int(payload, "season_id"),
            season_name=_payload_string(payload, "season_name"),
            season_status=Win5SeasonStatus(_payload_string(payload, "season_status")),
            round_id=_payload_positive_int(payload, "round_id"),
            round_name=_payload_string(payload, "round_name"),
            round_type=Win5RoundType(_payload_string(payload, "round_type")),
            round_status=Win5RoundStatus(_payload_string(payload, "round_status")),
            races=tuple(races),
        )

    @staticmethod
    def _entries_from_audit_payload(value: object) -> tuple[Win5CreatedEntry, ...]:
        if not isinstance(value, list):
            raise ValueError("entries must be a list.")
        entries: list[Win5CreatedEntry] = []
        for entry_payload in value:
            if not isinstance(entry_payload, Mapping):
                raise ValueError("Each Entry audit value must be an object.")
            entries.append(
                Win5CreatedEntry(
                    id=_payload_positive_int(entry_payload, "entry_id"),
                    gate_number=_payload_positive_int(entry_payload, "gate_number"),
                    name=_payload_string(entry_payload, "name"),
                )
            )
        return tuple(entries)


@dataclass(frozen=True, slots=True)
class CreatedWin5Round:
    """Closed-session Round creation result returned after commit or exact retry."""

    snapshot: Win5RoundCreationSnapshot
    operation_type: Win5RoundCreationAuditType

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_type", Win5RoundCreationAuditType(self.operation_type))
        expected = Win5RoundCreationAuditType.for_round_type(self.snapshot.round_type)
        if self.operation_type != expected:
            raise ValueError("Round creation operation type does not match the created Round type.")


@dataclass(frozen=True, slots=True)
class Win5RoundCreationAuditRecord:
    """One atomic Round graph creation audit prepared by Application."""

    type: Win5RoundCreationAuditType
    after: Win5RoundCreationSnapshot

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", Win5RoundCreationAuditType(self.type))
        expected = Win5RoundCreationAuditType.for_round_type(self.after.round_type)
        if self.type != expected:
            raise ValueError("Round creation audit type does not match the created Round type.")

    @property
    def after_data(self) -> dict[str, object]:
        return self.after.to_audit_payload()


@dataclass(frozen=True, slots=True)
class StoredWin5RoundCreationOperation:
    """Minimal persisted operation used to resolve an exact retry."""

    request_fingerprint: str | None
    type: str | None
    season_id: int | None
    round_id: int | None
    after_data: Mapping[str, object] | None


class Win5RoundCreationRepository(Protocol):
    """Persistence operations required by Round creation commands."""

    def lock_season(self, *, season_id: int) -> Win5RoundCreationSeason | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredWin5RoundCreationOperation | None: ...

    def create_round_graph(
        self,
        *,
        command: CreateWin5Round,
        created_at: datetime,
    ) -> Win5CreatedRoundGraph: ...

    def add_creation_audit(
        self,
        *,
        command: CreateWin5Round,
        record: Win5RoundCreationAuditRecord,
        created_at: datetime,
    ) -> None: ...


class Win5RoundCreationUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the Round creation repository."""

    @property
    def win5_round_creation(self) -> Win5RoundCreationRepository: ...


@dataclass(frozen=True, slots=True)
class Win5RoundCreationCommands:
    """Application entry point for atomic Normal/Special Round creation."""

    command_runner: CommandRunner[Win5RoundCreationUnitOfWork]
    clock: Callable[[], datetime]

    def create_round(self, command: CreateWin5Round) -> CreatedWin5Round:
        return self.command_runner.run(
            lambda unit_of_work: self._create_round(unit_of_work.win5_round_creation, command)
        )

    def _create_round(
        self,
        repository: Win5RoundCreationRepository,
        command: CreateWin5Round,
    ) -> CreatedWin5Round:
        season = repository.lock_season(season_id=command.season_id)
        if season is None:
            raise Win5RoundCreationUnavailableError("WIN5 Season does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if season.status not in {Win5SeasonStatus.DRAFT, Win5SeasonStatus.ACTIVE}:
            raise Win5RoundCreationUnavailableError("WIN5 Rounds may be created only in a draft or active Season.")

        created_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        graph = repository.create_round_graph(command=command, created_at=created_at)
        requested_races = tuple(
            (
                race.name,
                race.scheduled_at,
                tuple((entry.gate_number, entry.name) for entry in race.entries),
            )
            for race in command.races
        )
        created_races = tuple(
            (
                race.name,
                race.scheduled_at,
                tuple((entry.gate_number, entry.name) for entry in race.entries),
            )
            for race in graph.races
        )
        if created_races != requested_races:
            raise Win5RoundCreationAuditError("Created Round graph does not match the requested ordered Race facts.")
        snapshot = Win5RoundCreationSnapshot(
            season_id=season.id,
            season_name=season.name,
            season_status=season.status,
            round_id=graph.round_id,
            round_name=command.round_name,
            round_type=command.round_type,
            round_status=Win5RoundStatus.SETUP,
            races=graph.races,
        )
        operation_type = Win5RoundCreationAuditType.for_round_type(command.round_type)
        repository.add_creation_audit(
            command=command,
            record=Win5RoundCreationAuditRecord(type=operation_type, after=snapshot),
            created_at=created_at,
        )
        return CreatedWin5Round(snapshot=snapshot, operation_type=operation_type)

    @classmethod
    def _resolve_exact_retry(
        cls,
        *,
        stored: StoredWin5RoundCreationOperation,
        command: CreateWin5Round,
    ) -> CreatedWin5Round:
        expected_type = Win5RoundCreationAuditType.for_round_type(command.round_type)
        try:
            operation_type = Win5RoundCreationAuditType(stored.type)
        except (TypeError, ValueError) as exc:
            raise Win5RoundCreationIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from exc
        if (
            operation_type != expected_type
            or stored.request_fingerprint != command.request_fingerprint
            or stored.season_id != command.season_id
        ):
            raise Win5RoundCreationIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise Win5RoundCreationAuditError("Exact-retry operation has no stored Round creation payload.")
        try:
            snapshot = Win5RoundCreationSnapshot.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise Win5RoundCreationAuditError("Exact-retry Round creation payload is malformed.") from exc
        requested_races = tuple(
            (
                race.name,
                race.scheduled_at,
                tuple((entry.gate_number, entry.name) for entry in race.entries),
            )
            for race in command.races
        )
        stored_races = tuple(
            (
                race.name,
                race.scheduled_at,
                tuple((entry.gate_number, entry.name) for entry in race.entries),
            )
            for race in snapshot.races
        )
        if (
            snapshot.season_id != command.season_id
            or snapshot.round_id != stored.round_id
            or snapshot.round_name != command.round_name
            or snapshot.round_type != command.round_type
            or snapshot.round_status != Win5RoundStatus.SETUP
            or requested_races != stored_races
        ):
            raise Win5RoundCreationAuditError(
                "Exact-retry Round creation payload does not match its operation context."
            )
        return CreatedWin5Round(snapshot=snapshot, operation_type=operation_type)
