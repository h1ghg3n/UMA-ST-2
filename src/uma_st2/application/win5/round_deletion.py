"""Guarded deletion of one incorrectly created WIN5 setup Round graph."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.application.publication import (
    WIN5_ROUND_PUBLICATION_SOURCE_KIND as WIN5_ROUND_PUBLICATION_SOURCE_KIND,
)
from uma_st2.domain.win5 import Win5RoundSourceKind, Win5RoundStatus, Win5RoundType, Win5SeasonStatus
from uma_st2.shared import normalize_utc_datetime

WIN5_SETUP_ROUND_DELETION_AUDIT_SCHEMA_VERSION: Final = 1


class Win5SetupRoundDeletionAuditType(StrEnum):
    """Canonical operation type for a retained setup-Round tombstone."""

    DELETED = "setup_round_deleted"


class Win5SetupRoundDeletionError(ValueError):
    """Base error for rejected setup-Round deletion commands."""


class Win5SetupRoundDeletionUnavailableError(Win5SetupRoundDeletionError):
    """The selected Season/Round no longer permits hard deletion."""


class Win5SetupRoundDeletionInvalidSourceError(Win5SetupRoundDeletionError):
    """Stored graph or dependency facts cannot be deleted safely."""


class Win5SetupRoundDeletionVersionConflictError(Win5SetupRoundDeletionError):
    """The Round graph changed after the destructive preview was rendered."""


class Win5SetupRoundDeletionIdempotencyConflictError(Win5SetupRoundDeletionError):
    """An idempotency key is bound to another logical command."""


class Win5SetupRoundDeletionAuditError(Win5SetupRoundDeletionError):
    """Stored exact-retry audit data is absent or malformed."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _require_non_negative_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


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


def _require_sha256(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest.")
    if any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest.")


def _payload_positive_int(payload: Mapping[str, object], key: str) -> int:
    value = payload[key]
    _require_positive_int(value, field_name=key)  # type: ignore[arg-type]
    return value  # type: ignore[return-value]


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


def _optional_datetime_payload(value: datetime | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return normalize_utc_datetime(value, field_name=field_name).isoformat()


def _datetime_from_payload(value: object, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO datetime string or null.")
    return normalize_utc_datetime(datetime.fromisoformat(value), field_name=field_name)


@dataclass(frozen=True, slots=True)
class DeleteWin5SetupRound:
    """Delete one exact setup Round graph through a caller-owned command UoW."""

    season_id: int
    round_id: int
    expected_graph_fingerprint: str
    idempotency_key: str
    actor_discord_user_id: str
    reason: str
    guild_id: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_sha256(self.expected_graph_fingerprint, field_name="expected_graph_fingerprint")
        for field_name, value, max_length, optional in (
            ("idempotency_key", self.idempotency_key, 128, False),
            ("actor_discord_user_id", self.actor_discord_user_id, 32, False),
            ("reason", self.reason, 255, False),
            ("guild_id", self.guild_id, 32, True),
            ("correlation_id", self.correlation_id, 128, True),
        ):
            object.__setattr__(
                self,
                field_name,
                _normalized_bounded_string(
                    value,
                    field_name=field_name,
                    max_length=max_length,
                    optional=optional,
                ),
            )

    @property
    def request_fingerprint(self) -> str:
        canonical = {
            "schema": "win5-setup-round-deletion-command-v1",
            "season_id": self.season_id,
            "round_id": self.round_id,
            "expected_graph_fingerprint": self.expected_graph_fingerprint,
        }
        encoded = json.dumps(canonical, separators=(",", ":"), sort_keys=True)
        return sha256(encoded.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class Win5SetupRoundDeletionSeason:
    """Locked parent Season facts for one deletion command."""

    id: int
    name: str
    status: Win5SeasonStatus

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        object.__setattr__(self, "name", _normalized_bounded_string(self.name, field_name="name", max_length=100))
        object.__setattr__(self, "status", Win5SeasonStatus(self.status))


@dataclass(frozen=True, slots=True)
class Win5SetupRoundDeletionEntry:
    """One canonical Entry retained in a deletion audit snapshot."""

    id: int
    gate_number: int
    name: str

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.gate_number, field_name="gate_number")
        object.__setattr__(self, "name", _normalized_bounded_string(self.name, field_name="name", max_length=100))


@dataclass(frozen=True, slots=True)
class Win5SetupRoundDeletionRace:
    """One ordered Race and its current Entries in a deletion snapshot."""

    id: int
    name: str
    scheduled_at: datetime | None = None
    entries: tuple[Win5SetupRoundDeletionEntry, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        object.__setattr__(self, "name", _normalized_bounded_string(self.name, field_name="name", max_length=200))
        if self.scheduled_at is not None:
            object.__setattr__(
                self,
                "scheduled_at",
                normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
            )
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, Win5SetupRoundDeletionEntry) for entry in self.entries
        ):
            raise ValueError("entries must be a tuple of Win5SetupRoundDeletionEntry values.")
        if len({entry.id for entry in self.entries}) != len(self.entries):
            raise ValueError("A deletion Race snapshot must contain unique Entry IDs.")
        if len({entry.gate_number for entry in self.entries}) != len(self.entries):
            raise ValueError("A deletion Race snapshot must contain unique Entry gate numbers.")
        object.__setattr__(
            self, "entries", tuple(sorted(self.entries, key=lambda entry: (entry.gate_number, entry.id)))
        )


@dataclass(frozen=True, slots=True)
class Win5SetupRoundDeletionSnapshot:
    """Complete deterministic graph used for preview, stale checking, and audit."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    round_id: int
    round_name: str
    round_type: Win5RoundType
    round_status: Win5RoundStatus
    source_kind: Win5RoundSourceKind
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    races: tuple[Win5SetupRoundDeletionRace, ...] = field(default_factory=tuple)

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
        object.__setattr__(self, "source_kind", Win5RoundSourceKind(self.source_kind))
        for field_name in ("opens_at", "closes_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, normalize_utc_datetime(value, field_name=field_name))
        if not isinstance(self.races, tuple) or any(
            not isinstance(race, Win5SetupRoundDeletionRace) for race in self.races
        ):
            raise ValueError("races must be a tuple of Win5SetupRoundDeletionRace values.")
        if len({race.id for race in self.races}) != len(self.races):
            raise ValueError("A deletion snapshot must contain unique Race IDs.")
        entry_ids = tuple(entry.id for race in self.races for entry in race.entries)
        if len(set(entry_ids)) != len(entry_ids):
            raise ValueError("A deletion snapshot must contain unique Entry IDs.")
        object.__setattr__(self, "races", tuple(sorted(self.races, key=lambda race: race.id)))

    @property
    def race_count(self) -> int:
        return len(self.races)

    @property
    def entry_count(self) -> int:
        return sum(len(race.entries) for race in self.races)

    @property
    def graph_fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_audit_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(encoded.encode("utf-8")).hexdigest()

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": WIN5_SETUP_ROUND_DELETION_AUDIT_SCHEMA_VERSION,
            "season_id": self.season_id,
            "season_name": self.season_name,
            "season_status": self.season_status.value,
            "round_id": self.round_id,
            "round_name": self.round_name,
            "round_type": self.round_type.value,
            "round_status": self.round_status.value,
            "opens_at": _optional_datetime_payload(self.opens_at, field_name="opens_at"),
            "closes_at": _optional_datetime_payload(self.closes_at, field_name="closes_at"),
            "races": [
                {
                    "race_id": race.id,
                    "race_name": race.name,
                    "scheduled_at": _optional_datetime_payload(race.scheduled_at, field_name="scheduled_at"),
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
    def from_audit_payload(cls, payload: Mapping[str, object]) -> Win5SetupRoundDeletionSnapshot:
        if _payload_positive_int(payload, "schema_version") != WIN5_SETUP_ROUND_DELETION_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported WIN5 setup Round deletion audit schema version.")
        race_payloads = payload["races"]
        if not isinstance(race_payloads, list):
            raise ValueError("races must be a list.")
        races: list[Win5SetupRoundDeletionRace] = []
        for race_payload in race_payloads:
            if not isinstance(race_payload, Mapping):
                raise ValueError("Each Race audit value must be an object.")
            entry_payloads = race_payload["entries"]
            if not isinstance(entry_payloads, list):
                raise ValueError("entries must be a list.")
            entries: list[Win5SetupRoundDeletionEntry] = []
            for entry_payload in entry_payloads:
                if not isinstance(entry_payload, Mapping):
                    raise ValueError("Each Entry audit value must be an object.")
                entries.append(
                    Win5SetupRoundDeletionEntry(
                        id=_payload_positive_int(entry_payload, "entry_id"),
                        gate_number=_payload_positive_int(entry_payload, "gate_number"),
                        name=_payload_string(entry_payload, "name"),
                    )
                )
            races.append(
                Win5SetupRoundDeletionRace(
                    id=_payload_positive_int(race_payload, "race_id"),
                    name=_payload_string(race_payload, "race_name"),
                    scheduled_at=_datetime_from_payload(race_payload["scheduled_at"], field_name="scheduled_at"),
                    entries=tuple(entries),
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
            source_kind=Win5RoundSourceKind.NATIVE_V2,
            opens_at=_datetime_from_payload(payload["opens_at"], field_name="opens_at"),
            closes_at=_datetime_from_payload(payload["closes_at"], field_name="closes_at"),
            races=tuple(races),
        )


@dataclass(frozen=True, slots=True)
class Win5SetupRoundDependencyState:
    """Locked downstream row counts that prohibit hard deletion."""

    submission_count: int
    result_count: int
    score_event_count: int
    publication_count: int

    def __post_init__(self) -> None:
        for field_name in (
            "submission_count",
            "result_count",
            "score_event_count",
            "publication_count",
        ):
            _require_non_negative_int(getattr(self, field_name), field_name=field_name)

    @property
    def has_downstream_facts(self) -> bool:
        return any(
            (
                self.submission_count,
                self.result_count,
                self.score_event_count,
                self.publication_count,
            )
        )


@dataclass(frozen=True, slots=True)
class DeletedWin5SetupRound:
    """Closed-session deletion result returned after commit or exact retry."""

    snapshot: Win5SetupRoundDeletionSnapshot
    operation_type: Win5SetupRoundDeletionAuditType = Win5SetupRoundDeletionAuditType.DELETED

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_type", Win5SetupRoundDeletionAuditType(self.operation_type))
        if self.operation_type != Win5SetupRoundDeletionAuditType.DELETED:
            raise ValueError("Unexpected setup Round deletion operation type.")

    @property
    def graph_fingerprint(self) -> str:
        return self.snapshot.graph_fingerprint


@dataclass(frozen=True, slots=True)
class Win5SetupRoundDeletionAuditRecord:
    """Immutable graph snapshot and tombstone stored with one deletion."""

    before: Win5SetupRoundDeletionSnapshot
    type: Win5SetupRoundDeletionAuditType = Win5SetupRoundDeletionAuditType.DELETED

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", Win5SetupRoundDeletionAuditType(self.type))

    @property
    def before_data(self) -> dict[str, object]:
        return self.before.to_audit_payload()

    @property
    def after_data(self) -> dict[str, object]:
        return {
            "schema_version": WIN5_SETUP_ROUND_DELETION_AUDIT_SCHEMA_VERSION,
            "deleted": True,
            "season_id": self.before.season_id,
            "round_id": self.before.round_id,
            "graph_fingerprint": self.before.graph_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class StoredWin5SetupRoundDeletionOperation:
    """Minimal persisted operation used to resolve an exact retry."""

    request_fingerprint: str | None
    type: str | None
    season_id: int | None
    round_id: int | None
    before_data: Mapping[str, object] | None
    after_data: Mapping[str, object] | None


class Win5SetupRoundDeletionRepository(Protocol):
    """Persistence operations required by guarded setup-Round deletion."""

    def lock_season(self, *, season_id: int) -> Win5SetupRoundDeletionSeason | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SetupRoundDeletionOperation | None: ...

    def lock_round_graph(
        self,
        *,
        season: Win5SetupRoundDeletionSeason,
        round_id: int,
    ) -> Win5SetupRoundDeletionSnapshot | None: ...

    def lock_dependency_state(
        self,
        *,
        round_id: int,
        race_ids: tuple[int, ...],
    ) -> Win5SetupRoundDependencyState: ...

    def delete_round_graph(self, *, snapshot: Win5SetupRoundDeletionSnapshot) -> None: ...

    def add_deletion_audit(
        self,
        *,
        command: DeleteWin5SetupRound,
        record: Win5SetupRoundDeletionAuditRecord,
        created_at: datetime,
    ) -> None: ...


class Win5SetupRoundDeletionUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the setup-Round deletion repository."""

    @property
    def win5_setup_round_deletion(self) -> Win5SetupRoundDeletionRepository: ...


@dataclass(frozen=True, slots=True)
class Win5SetupRoundDeletionCommands:
    """Application entry point for one guarded graph deletion."""

    command_runner: CommandRunner[Win5SetupRoundDeletionUnitOfWork]
    clock: Callable[[], datetime]

    def delete_round(self, command: DeleteWin5SetupRound) -> DeletedWin5SetupRound:
        return self.command_runner.run(
            lambda unit_of_work: self._delete_round(unit_of_work.win5_setup_round_deletion, command)
        )

    def _delete_round(
        self,
        repository: Win5SetupRoundDeletionRepository,
        command: DeleteWin5SetupRound,
    ) -> DeletedWin5SetupRound:
        try:
            season = repository.lock_season(season_id=command.season_id)
        except (TypeError, ValueError) as exc:
            raise Win5SetupRoundDeletionInvalidSourceError("Stored WIN5 Season facts are malformed.") from exc
        if season is None:
            raise Win5SetupRoundDeletionUnavailableError("WIN5 Season does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if season.status not in {Win5SeasonStatus.DRAFT, Win5SeasonStatus.ACTIVE}:
            raise Win5SetupRoundDeletionUnavailableError(
                "A setup Round may be deleted only from a draft or active Season."
            )
        try:
            snapshot = repository.lock_round_graph(season=season, round_id=command.round_id)
        except (TypeError, ValueError) as exc:
            raise Win5SetupRoundDeletionInvalidSourceError(
                "Stored WIN5 setup Round graph facts are malformed."
            ) from exc
        if snapshot is None:
            raise Win5SetupRoundDeletionUnavailableError("WIN5 Round does not exist in the selected Season.")
        if snapshot.source_kind != Win5RoundSourceKind.NATIVE_V2:
            raise Win5SetupRoundDeletionUnavailableError("Imported WIN5 Rounds are read-only.")
        if (
            snapshot.season_id != season.id
            or snapshot.season_name != season.name
            or snapshot.season_status != season.status
        ):
            raise Win5SetupRoundDeletionInvalidSourceError("WIN5 Round graph does not match its locked parent Season.")
        if snapshot.round_status != Win5RoundStatus.SETUP:
            raise Win5SetupRoundDeletionUnavailableError("Only a setup WIN5 Round may be hard deleted.")
        if snapshot.graph_fingerprint != command.expected_graph_fingerprint:
            raise Win5SetupRoundDeletionVersionConflictError(
                "WIN5 Round graph changed after the deletion preview was loaded."
            )

        try:
            dependencies = repository.lock_dependency_state(
                round_id=snapshot.round_id,
                race_ids=tuple(race.id for race in snapshot.races),
            )
        except (TypeError, ValueError) as exc:
            raise Win5SetupRoundDeletionInvalidSourceError(
                "Stored WIN5 setup Round dependency facts are malformed."
            ) from exc
        if dependencies.has_downstream_facts:
            raise Win5SetupRoundDeletionUnavailableError(
                "WIN5 Round has downstream Submission, Result, score, or publication facts."
            )

        deleted_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        record = Win5SetupRoundDeletionAuditRecord(before=snapshot)
        repository.delete_round_graph(snapshot=snapshot)
        repository.add_deletion_audit(
            command=command,
            record=record,
            created_at=deleted_at,
        )
        return DeletedWin5SetupRound(snapshot=snapshot)

    @classmethod
    def _resolve_exact_retry(
        cls,
        *,
        stored: StoredWin5SetupRoundDeletionOperation,
        command: DeleteWin5SetupRound,
    ) -> DeletedWin5SetupRound:
        try:
            operation_type = Win5SetupRoundDeletionAuditType(stored.type)
        except (TypeError, ValueError) as exc:
            raise Win5SetupRoundDeletionIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from exc
        if (
            operation_type != Win5SetupRoundDeletionAuditType.DELETED
            or stored.request_fingerprint != command.request_fingerprint
            or stored.season_id != command.season_id
            or stored.round_id != command.round_id
        ):
            raise Win5SetupRoundDeletionIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.before_data is None or stored.after_data is None:
            raise Win5SetupRoundDeletionAuditError("Exact-retry deletion operation has no complete audit payload.")
        try:
            snapshot = Win5SetupRoundDeletionSnapshot.from_audit_payload(stored.before_data)
            cls._validate_tombstone(payload=stored.after_data, snapshot=snapshot)
        except (KeyError, TypeError, ValueError) as exc:
            raise Win5SetupRoundDeletionAuditError("Exact-retry deletion audit payload is malformed.") from exc
        if (
            snapshot.season_id != command.season_id
            or snapshot.round_id != command.round_id
            or snapshot.graph_fingerprint != command.expected_graph_fingerprint
        ):
            raise Win5SetupRoundDeletionAuditError(
                "Exact-retry deletion snapshot does not match its operation context."
            )
        return DeletedWin5SetupRound(snapshot=snapshot, operation_type=operation_type)

    @staticmethod
    def _validate_tombstone(
        *,
        payload: Mapping[str, object],
        snapshot: Win5SetupRoundDeletionSnapshot,
    ) -> None:
        if _payload_positive_int(payload, "schema_version") != WIN5_SETUP_ROUND_DELETION_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported deletion tombstone schema version.")
        if payload["deleted"] is not True:
            raise ValueError("Deletion tombstone must be marked deleted.")
        if (
            _payload_positive_int(payload, "season_id") != snapshot.season_id
            or _payload_positive_int(payload, "round_id") != snapshot.round_id
            or _payload_string(payload, "graph_fingerprint") != snapshot.graph_fingerprint
        ):
            raise ValueError("Deletion tombstone does not match its before snapshot.")
