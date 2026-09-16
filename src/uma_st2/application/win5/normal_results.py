"""Normal WIN5 authoritative result entry and correction boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.win5 import (
    Win5DomainError,
    Win5NormalResultPlacement,
    Win5Placement,
    Win5Race,
    Win5RaceResult,
    Win5Round,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    fingerprint_normal_result,
    validate_normal_round_result_structure,
)
from uma_st2.shared import normalize_utc_datetime

WIN5_NORMAL_RESULT_AUDIT_SCHEMA_VERSION: Final = 1


class Win5NormalResultAuditType(StrEnum):
    """Canonical operation types for Normal authoritative result mutations."""

    ENTERED = "normal_result_entered"
    CORRECTED = "normal_result_corrected"


class Win5NormalResultCommandError(ValueError):
    """Base error for rejected Normal result commands."""


class Win5NormalResultUnavailableError(Win5NormalResultCommandError):
    """The target is not a closed Normal Round in an active Season."""


class Win5NormalResultImmutableError(Win5NormalResultCommandError):
    """The target Round already owns immutable scoring facts."""


class Win5NormalResultInvalidSourceError(Win5NormalResultCommandError):
    """Persisted or desired result facts cannot form one canonical board."""


class Win5NormalResultVersionConflictError(Win5NormalResultCommandError):
    """The command expected a different current result fingerprint."""


class Win5NormalResultIdempotencyConflictError(Win5NormalResultCommandError):
    """An idempotency key is bound to a different logical command."""


class Win5NormalResultAuditError(Win5NormalResultCommandError):
    """Stored exact-retry audit data is absent or malformed."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _require_bounded_string(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
    optional: bool = False,
) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value or len(value) > max_length:
        qualifier = "non-empty " if not optional else "non-empty optional "
        raise ValueError(f"{field_name} must be a {qualifier}string no longer than {max_length} characters.")


def _require_sha256(value: str, *, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest.")


def _payload_positive_int(payload: Mapping[str, object], key: str) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{key} must be a positive integer.")
    return value


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


@dataclass(frozen=True, slots=True)
class Win5NormalResultPlacementInput:
    """One desired Normal finishing position and canonical RaceEntry."""

    position: int
    race_entry_id: int

    def __post_init__(self) -> None:
        _require_positive_int(self.position, field_name="position")
        _require_positive_int(self.race_entry_id, field_name="race_entry_id")


@dataclass(frozen=True, slots=True)
class SaveNormalWin5Result:
    """Enter or correct one complete Normal result board."""

    round_id: int
    placements: tuple[Win5NormalResultPlacementInput, ...]
    idempotency_key: str
    actor_discord_user_id: str
    expected_result_fingerprint: str | None = None
    guild_id: str | None = None
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.round_id, field_name="round_id")
        object.__setattr__(
            self,
            "placements",
            tuple(sorted(self.placements, key=lambda placement: placement.position)),
        )
        _require_bounded_string(self.idempotency_key, field_name="idempotency_key", max_length=128)
        _require_bounded_string(
            self.actor_discord_user_id,
            field_name="actor_discord_user_id",
            max_length=32,
        )
        if self.expected_result_fingerprint is not None:
            _require_sha256(
                self.expected_result_fingerprint,
                field_name="expected_result_fingerprint",
            )
        _require_bounded_string(self.guild_id, field_name="guild_id", max_length=32, optional=True)
        _require_bounded_string(
            self.correlation_id,
            field_name="correlation_id",
            max_length=128,
            optional=True,
        )
        _require_bounded_string(self.reason, field_name="reason", max_length=255, optional=True)

    @property
    def request_fingerprint(self) -> str:
        """Return a stable fingerprint for the complete desired mutation."""

        expected = self.expected_result_fingerprint or "none"
        canonical = "\n".join(
            (
                "win5-normal-result-command-v1",
                f"round:{self.round_id}",
                f"expected:{expected}",
                *(f"{placement.position}:{placement.race_entry_id}" for placement in self.placements),
            )
        )
        return sha256(canonical.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class Win5NormalResultRoundTarget:
    """Locked Round and parent Season state for result mutation."""

    id: int
    season_id: int
    name: str
    type: Win5RoundType
    source_kind: Win5RoundSourceKind
    status: Win5RoundStatus
    season_status: Win5SeasonStatus

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.season_id, field_name="season_id")
        _require_bounded_string(self.name, field_name="name", max_length=100)
        object.__setattr__(self, "type", Win5RoundType(self.type))
        object.__setattr__(self, "status", Win5RoundStatus(self.status))
        object.__setattr__(self, "season_status", Win5SeasonStatus(self.season_status))
        object.__setattr__(self, "source_kind", Win5RoundSourceKind(self.source_kind))


@dataclass(frozen=True, slots=True)
class SavedNormalWin5Result:
    """Closed-session canonical result returned after save, no-op, or retry."""

    season_id: int
    round_id: int
    race_id: int
    result_fingerprint: str
    placements: tuple[Win5NormalResultPlacement, ...] = field(default_factory=tuple)
    operation_type: Win5NormalResultAuditType | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_positive_int(self.race_id, field_name="race_id")
        _require_sha256(self.result_fingerprint, field_name="result_fingerprint")
        ordered = tuple(sorted(self.placements, key=lambda placement: placement.position))
        if fingerprint_normal_result(ordered) != self.result_fingerprint:
            raise ValueError("result_fingerprint does not match the canonical placements.")
        object.__setattr__(self, "placements", ordered)
        if self.operation_type is not None:
            object.__setattr__(self, "operation_type", Win5NormalResultAuditType(self.operation_type))

    def to_audit_payload(self) -> dict[str, object]:
        """Serialize the deterministic authoritative result snapshot v1."""

        return {
            "schema_version": WIN5_NORMAL_RESULT_AUDIT_SCHEMA_VERSION,
            "season_id": self.season_id,
            "round_id": self.round_id,
            "race_id": self.race_id,
            "round_status": Win5RoundStatus.CLOSED.value,
            "result_fingerprint": self.result_fingerprint,
            "placements": [
                {
                    "result_id": placement.id,
                    "position": placement.position,
                    "race_entry_id": placement.race_entry_id,
                }
                for placement in self.placements
            ],
        }

    @classmethod
    def from_audit_payload(
        cls,
        payload: Mapping[str, object],
        *,
        operation_type: Win5NormalResultAuditType,
    ) -> SavedNormalWin5Result:
        if _payload_positive_int(payload, "schema_version") != WIN5_NORMAL_RESULT_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Normal result audit schema version.")
        if _payload_string(payload, "round_status") != Win5RoundStatus.CLOSED.value:
            raise ValueError("Stored Normal result audit does not describe a closed Round.")
        raw_placements = payload["placements"]
        if not isinstance(raw_placements, list) or not all(
            isinstance(placement, Mapping) for placement in raw_placements
        ):
            raise ValueError("Stored Normal result placements must be a JSON list of objects.")
        return cls(
            season_id=_payload_positive_int(payload, "season_id"),
            round_id=_payload_positive_int(payload, "round_id"),
            race_id=_payload_positive_int(payload, "race_id"),
            result_fingerprint=_payload_string(payload, "result_fingerprint"),
            placements=tuple(
                Win5NormalResultPlacement(
                    id=_payload_positive_int(placement, "result_id"),
                    position=_payload_positive_int(placement, "position"),
                    race_entry_id=_payload_positive_int(placement, "race_entry_id"),
                )
                for placement in raw_placements
            ),
            operation_type=operation_type,
        )


@dataclass(frozen=True, slots=True)
class Win5NormalResultAuditRecord:
    """One state-changing Normal result audit prepared by Application."""

    type: Win5NormalResultAuditType
    before: SavedNormalWin5Result | None
    after: SavedNormalWin5Result

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", Win5NormalResultAuditType(self.type))
        if self.after.operation_type != self.type:
            raise ValueError("Normal result audit after DTO must carry the operation type.")
        if self.type == Win5NormalResultAuditType.ENTERED:
            if self.before is not None:
                raise ValueError("Initial Normal result entry must omit the before snapshot.")
            return
        if self.before is None:
            raise ValueError("Normal result correction requires a before snapshot.")
        if (
            self.before.season_id,
            self.before.round_id,
            self.before.race_id,
        ) != (
            self.after.season_id,
            self.after.round_id,
            self.after.race_id,
        ):
            raise ValueError("Normal result correction must preserve Season, Round, and Race identity.")
        before_ids = tuple(placement.id for placement in self.before.placements)
        after_ids = tuple(placement.id for placement in self.after.placements)
        if before_ids != after_ids:
            raise ValueError("Normal result correction must preserve Result row IDs by position.")
        if self.before.result_fingerprint == self.after.result_fingerprint:
            raise ValueError("An exact Normal result no-op must not create an audit record.")

    @property
    def before_data(self) -> dict[str, object] | None:
        return None if self.before is None else self.before.to_audit_payload()

    @property
    def after_data(self) -> dict[str, object]:
        return self.after.to_audit_payload()


@dataclass(frozen=True, slots=True)
class StoredWin5NormalResultOperation:
    """Minimal persisted operation used to resolve an exact retry."""

    request_fingerprint: str | None
    type: str | None
    round_id: int | None
    after_data: Mapping[str, object] | None


class Win5NormalResultRepository(Protocol):
    """Persistence operations required by Normal result mutation."""

    def lock_round(self, *, round_id: int) -> Win5NormalResultRoundTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredWin5NormalResultOperation | None: ...

    def lock_round_races(self, *, round_id: int) -> tuple[Win5Race, ...]: ...

    def lock_current_result(self, *, race_id: int) -> tuple[Win5NormalResultPlacement, ...]: ...

    def has_score_events(self, *, round_id: int) -> bool: ...

    def create_result(
        self,
        *,
        race_id: int,
        placements: tuple[Win5NormalResultPlacementInput, ...],
        created_at: datetime,
    ) -> tuple[Win5NormalResultPlacement, ...]: ...

    def replace_result(
        self,
        *,
        race_id: int,
        placements: tuple[Win5NormalResultPlacement, ...],
    ) -> None: ...

    def add_result_audit(
        self,
        *,
        command: SaveNormalWin5Result,
        round_: Win5NormalResultRoundTarget,
        record: Win5NormalResultAuditRecord,
        created_at: datetime,
    ) -> None: ...


class Win5NormalResultUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the Normal result repository."""

    @property
    def win5_normal_results(self) -> Win5NormalResultRepository: ...


@dataclass(frozen=True, slots=True)
class Win5NormalResultCommands:
    """Application entry point for atomic Normal result entry/correction."""

    command_runner: CommandRunner[Win5NormalResultUnitOfWork]
    clock: Callable[[], datetime]

    def save_result(self, command: SaveNormalWin5Result) -> SavedNormalWin5Result:
        return self.command_runner.run(
            lambda unit_of_work: self._save_result(unit_of_work.win5_normal_results, command)
        )

    def _save_result(
        self,
        repository: Win5NormalResultRepository,
        command: SaveNormalWin5Result,
    ) -> SavedNormalWin5Result:
        round_ = repository.lock_round(round_id=command.round_id)
        if round_ is None:
            raise Win5NormalResultUnavailableError("WIN5 Round does not exist.")
        if round_.source_kind != Win5RoundSourceKind.NATIVE_V2:
            raise Win5NormalResultUnavailableError("Imported WIN5 Rounds are read-only.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if round_.status == Win5RoundStatus.SCORED:
            raise Win5NormalResultImmutableError("A scored WIN5 Round has immutable Result facts.")
        if (
            round_.type != Win5RoundType.NORMAL
            or round_.season_status != Win5SeasonStatus.ACTIVE
            or round_.status != Win5RoundStatus.CLOSED
        ):
            raise Win5NormalResultUnavailableError(
                "Normal result entry requires a closed Normal Round in an active Season."
            )

        races = repository.lock_round_races(round_id=round_.id)
        if len(races) != 1:
            raise Win5NormalResultInvalidSourceError("Normal result entry requires exactly one Race.")
        race = races[0]
        domain_round = Win5Round(
            id=round_.id,
            season_id=round_.season_id,
            name=round_.name,
            type=round_.type,
            status=round_.status,
            races=(race,),
        )
        desired_result = Win5RaceResult(
            race_id=race.id,
            placements=tuple(
                Win5Placement(
                    race_entry_id=placement.race_entry_id,
                    position=placement.position,
                    gate_number=None,
                )
                for placement in command.placements
            ),
        )
        try:
            validate_normal_round_result_structure(domain_round, desired_result)
        except Win5DomainError as exc:
            raise Win5NormalResultInvalidSourceError(str(exc)) from exc

        try:
            current = repository.lock_current_result(race_id=race.id)
        except ValueError as exc:
            raise Win5NormalResultInvalidSourceError(str(exc)) from exc
        if repository.has_score_events(round_id=round_.id):
            raise Win5NormalResultImmutableError("A Round with score events has immutable Result facts.")

        changed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        if not current:
            if command.expected_result_fingerprint is not None:
                raise Win5NormalResultVersionConflictError(
                    "Initial Normal result entry requires no expected result fingerprint."
                )
            persisted = repository.create_result(
                race_id=race.id,
                placements=command.placements,
                created_at=changed_at,
            )
            operation_type = Win5NormalResultAuditType.ENTERED
            before = None
        else:
            try:
                current_fingerprint = fingerprint_normal_result(current)
            except Win5DomainError as exc:
                raise Win5NormalResultInvalidSourceError(str(exc)) from exc
            if command.expected_result_fingerprint != current_fingerprint:
                raise Win5NormalResultVersionConflictError(
                    "Normal result fingerprint conflict: "
                    f"expected {command.expected_result_fingerprint!r}, current {current_fingerprint}."
                )
            before = SavedNormalWin5Result(
                season_id=round_.season_id,
                round_id=round_.id,
                race_id=race.id,
                result_fingerprint=current_fingerprint,
                placements=current,
            )
            current_by_position = {placement.position: placement for placement in current}
            persisted = tuple(
                Win5NormalResultPlacement(
                    id=current_by_position[placement.position].id,
                    position=placement.position,
                    race_entry_id=placement.race_entry_id,
                )
                for placement in command.placements
            )
            desired_identity = tuple((placement.position, placement.race_entry_id) for placement in persisted)
            current_identity = tuple((placement.position, placement.race_entry_id) for placement in current)
            if desired_identity == current_identity:
                return before
            repository.replace_result(race_id=race.id, placements=persisted)
            operation_type = Win5NormalResultAuditType.CORRECTED

        result_fingerprint = fingerprint_normal_result(persisted)
        after = SavedNormalWin5Result(
            season_id=round_.season_id,
            round_id=round_.id,
            race_id=race.id,
            result_fingerprint=result_fingerprint,
            placements=persisted,
            operation_type=operation_type,
        )
        record = Win5NormalResultAuditRecord(
            type=operation_type,
            before=before,
            after=after,
        )
        repository.add_result_audit(
            command=command,
            round_=round_,
            record=record,
            created_at=changed_at,
        )
        return after

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredWin5NormalResultOperation,
        command: SaveNormalWin5Result,
    ) -> SavedNormalWin5Result:
        try:
            operation_type = Win5NormalResultAuditType(stored.type)
        except (TypeError, ValueError) as exc:
            raise Win5NormalResultIdempotencyConflictError(
                "Idempotency key is already bound to a different logical operation."
            ) from exc
        if stored.request_fingerprint != command.request_fingerprint or stored.round_id != command.round_id:
            raise Win5NormalResultIdempotencyConflictError(
                "Idempotency key is already bound to a different logical operation."
            )
        if stored.after_data is None:
            raise Win5NormalResultAuditError("Exact-retry operation has no stored result payload.")
        try:
            result = SavedNormalWin5Result.from_audit_payload(
                stored.after_data,
                operation_type=operation_type,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise Win5NormalResultAuditError("Exact-retry operation has malformed stored result payload.") from exc
        if result.round_id != command.round_id:
            raise Win5NormalResultAuditError("Exact-retry result does not belong to the requested Round.")
        return result
