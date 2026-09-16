"""Special WIN5 authoritative result entry and correction boundary."""

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
    Win5Placement,
    Win5Race,
    Win5RaceResult,
    Win5Round,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SpecialResultWinner,
    fingerprint_special_result,
    validate_special_round_result_structure,
)
from uma_st2.shared import normalize_utc_datetime

WIN5_SPECIAL_RESULT_AUDIT_SCHEMA_VERSION: Final = 1


class Win5SpecialResultAuditType(StrEnum):
    """Canonical operation types for Special authoritative result mutations."""

    ENTERED = "special_result_entered"
    CORRECTED = "special_result_corrected"


class Win5SpecialResultCommandError(ValueError):
    """Base error for rejected Special result commands."""


class Win5SpecialResultUnavailableError(Win5SpecialResultCommandError):
    """The target is not a closed Special Round in an active Season."""


class Win5SpecialResultImmutableError(Win5SpecialResultCommandError):
    """The target Round already owns immutable scoring facts."""


class Win5SpecialResultInvalidSourceError(Win5SpecialResultCommandError):
    """Persisted or desired result facts cannot form one complete winner bundle."""


class Win5SpecialResultVersionConflictError(Win5SpecialResultCommandError):
    """The command expected a different current result fingerprint."""


class Win5SpecialResultIdempotencyConflictError(Win5SpecialResultCommandError):
    """An idempotency key is bound to a different logical command."""


class Win5SpecialResultAuditError(Win5SpecialResultCommandError):
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
class Win5SpecialResultWinnerInput:
    """One desired Special Race winner identified by positive gate number."""

    race_id: int
    gate_number: int

    def __post_init__(self) -> None:
        _require_positive_int(self.race_id, field_name="race_id")
        _require_positive_int(self.gate_number, field_name="gate_number")


@dataclass(frozen=True, slots=True)
class SaveSpecialWin5Result:
    """Enter or correct one complete Special winner bundle."""

    round_id: int
    winners: tuple[Win5SpecialResultWinnerInput, ...]
    idempotency_key: str
    actor_discord_user_id: str
    expected_result_fingerprint: str | None = None
    guild_id: str | None = None
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.round_id, field_name="round_id")
        object.__setattr__(self, "winners", tuple(sorted(self.winners, key=lambda winner: winner.race_id)))
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
                "win5-special-result-command-v1",
                f"round:{self.round_id}",
                f"expected:{expected}",
                *(f"{winner.race_id}:{winner.gate_number}" for winner in self.winners),
            )
        )
        return sha256(canonical.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class Win5SpecialResultRoundTarget:
    """Locked Round and parent Season state for Special result mutation."""

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
class Win5SpecialResultRaceTarget:
    """One locked Special Race and its complete explicit current void fact."""

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
class SavedSpecialWin5Result:
    """Closed-session canonical Special result returned after save/no-op/retry."""

    season_id: int
    round_id: int
    result_fingerprint: str
    winners: tuple[Win5SpecialResultWinner, ...] = field(default_factory=tuple)
    operation_type: Win5SpecialResultAuditType | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_sha256(self.result_fingerprint, field_name="result_fingerprint")
        ordered = tuple(sorted(self.winners, key=lambda winner: winner.race_id))
        if fingerprint_special_result(ordered) != self.result_fingerprint:
            raise ValueError("result_fingerprint does not match the canonical winners.")
        object.__setattr__(self, "winners", ordered)
        if self.operation_type is not None:
            object.__setattr__(self, "operation_type", Win5SpecialResultAuditType(self.operation_type))

    def to_audit_payload(self) -> dict[str, object]:
        """Serialize the deterministic authoritative Special result snapshot v1."""

        return {
            "schema_version": WIN5_SPECIAL_RESULT_AUDIT_SCHEMA_VERSION,
            "season_id": self.season_id,
            "round_id": self.round_id,
            "round_status": Win5RoundStatus.CLOSED.value,
            "result_fingerprint": self.result_fingerprint,
            "winners": [
                {
                    "result_id": winner.id,
                    "race_id": winner.race_id,
                    "position": 1,
                    "gate_number": winner.gate_number,
                }
                for winner in self.winners
            ],
        }

    @classmethod
    def from_audit_payload(
        cls,
        payload: Mapping[str, object],
        *,
        operation_type: Win5SpecialResultAuditType,
    ) -> SavedSpecialWin5Result:
        if _payload_positive_int(payload, "schema_version") != WIN5_SPECIAL_RESULT_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Special result audit schema version.")
        if _payload_string(payload, "round_status") != Win5RoundStatus.CLOSED.value:
            raise ValueError("Stored Special result audit does not describe a closed Round.")
        raw_winners = payload["winners"]
        if not isinstance(raw_winners, list) or not all(isinstance(winner, Mapping) for winner in raw_winners):
            raise ValueError("Stored Special result winners must be a JSON list of objects.")
        if any(_payload_positive_int(winner, "position") != 1 for winner in raw_winners):
            raise ValueError("Stored Special result winners must use position 1.")
        return cls(
            season_id=_payload_positive_int(payload, "season_id"),
            round_id=_payload_positive_int(payload, "round_id"),
            result_fingerprint=_payload_string(payload, "result_fingerprint"),
            winners=tuple(
                Win5SpecialResultWinner(
                    id=_payload_positive_int(winner, "result_id"),
                    race_id=_payload_positive_int(winner, "race_id"),
                    gate_number=_payload_positive_int(winner, "gate_number"),
                )
                for winner in raw_winners
            ),
            operation_type=operation_type,
        )


@dataclass(frozen=True, slots=True)
class Win5SpecialResultAuditRecord:
    """One state-changing Special result audit prepared by Application."""

    type: Win5SpecialResultAuditType
    before: SavedSpecialWin5Result | None
    after: SavedSpecialWin5Result

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", Win5SpecialResultAuditType(self.type))
        if self.after.operation_type != self.type:
            raise ValueError("Special result audit after DTO must carry the operation type.")
        if self.type == Win5SpecialResultAuditType.ENTERED:
            if self.before is not None:
                raise ValueError("Initial Special result entry must omit the before snapshot.")
            return
        if self.before is None:
            raise ValueError("Special result correction requires a before snapshot.")
        if (self.before.season_id, self.before.round_id) != (self.after.season_id, self.after.round_id):
            raise ValueError("Special result correction must preserve Season and Round identity.")
        before_ids = tuple((winner.race_id, winner.id) for winner in self.before.winners)
        after_ids = tuple((winner.race_id, winner.id) for winner in self.after.winners)
        if before_ids != after_ids:
            raise ValueError("Special result correction must preserve Result row IDs by Race.")
        if self.before.result_fingerprint == self.after.result_fingerprint:
            raise ValueError("An exact Special result no-op must not create an audit record.")

    @property
    def before_data(self) -> dict[str, object] | None:
        return None if self.before is None else self.before.to_audit_payload()

    @property
    def after_data(self) -> dict[str, object]:
        return self.after.to_audit_payload()


@dataclass(frozen=True, slots=True)
class StoredWin5SpecialResultOperation:
    """Minimal persisted operation used to resolve an exact retry."""

    request_fingerprint: str | None
    type: str | None
    round_id: int | None
    after_data: Mapping[str, object] | None


class Win5SpecialResultRepository(Protocol):
    """Persistence operations required by Special result mutation."""

    def lock_round(self, *, round_id: int) -> Win5SpecialResultRoundTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SpecialResultOperation | None: ...

    def lock_round_races(self, *, round_id: int) -> tuple[Win5SpecialResultRaceTarget, ...]: ...

    def lock_current_results(self, *, round_id: int) -> tuple[Win5SpecialResultWinner, ...]: ...

    def has_score_events(self, *, round_id: int) -> bool: ...

    def create_results(
        self,
        *,
        winners: tuple[Win5SpecialResultWinnerInput, ...],
        created_at: datetime,
    ) -> tuple[Win5SpecialResultWinner, ...]: ...

    def replace_results(self, *, winners: tuple[Win5SpecialResultWinner, ...]) -> None: ...

    def add_result_audit(
        self,
        *,
        command: SaveSpecialWin5Result,
        round_: Win5SpecialResultRoundTarget,
        record: Win5SpecialResultAuditRecord,
        created_at: datetime,
    ) -> None: ...


class Win5SpecialResultUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the Special result repository."""

    @property
    def win5_special_results(self) -> Win5SpecialResultRepository: ...


@dataclass(frozen=True, slots=True)
class Win5SpecialResultCommands:
    """Application entry point for atomic Special result entry/correction."""

    command_runner: CommandRunner[Win5SpecialResultUnitOfWork]
    clock: Callable[[], datetime]

    def save_result(self, command: SaveSpecialWin5Result) -> SavedSpecialWin5Result:
        return self.command_runner.run(
            lambda unit_of_work: self._save_result(unit_of_work.win5_special_results, command)
        )

    def _save_result(
        self,
        repository: Win5SpecialResultRepository,
        command: SaveSpecialWin5Result,
    ) -> SavedSpecialWin5Result:
        round_ = repository.lock_round(round_id=command.round_id)
        if round_ is None:
            raise Win5SpecialResultUnavailableError("WIN5 Round does not exist.")
        if round_.source_kind != Win5RoundSourceKind.NATIVE_V2:
            raise Win5SpecialResultUnavailableError("Imported WIN5 Rounds are read-only.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if round_.status == Win5RoundStatus.SCORED:
            raise Win5SpecialResultImmutableError("A scored WIN5 Round has immutable Result facts.")
        if (
            round_.type != Win5RoundType.SPECIAL
            or round_.season_status != Win5SeasonStatus.ACTIVE
            or round_.status != Win5RoundStatus.CLOSED
        ):
            raise Win5SpecialResultUnavailableError(
                "Special result entry requires a closed Special Round in an active Season."
            )

        race_targets = tuple(sorted(repository.lock_round_races(round_id=round_.id), key=lambda race: race.id))
        if not race_targets or len({race.id for race in race_targets}) != len(race_targets):
            raise Win5SpecialResultInvalidSourceError("Special result entry requires one or more unique Races.")
        non_void_race_ids = {race.id for race in race_targets if not race.is_void}
        if not non_void_race_ids:
            raise Win5SpecialResultInvalidSourceError("An all-void Special Round must already be cancelled.")
        desired_by_race = {winner.race_id: winner for winner in command.winners}
        if len(desired_by_race) != len(command.winners) or set(desired_by_race) != non_void_race_ids:
            raise Win5SpecialResultInvalidSourceError(
                "Special result entry must provide exactly one winner for every non-void Race in the Round."
            )

        domain_round = Win5Round(
            id=round_.id,
            season_id=round_.season_id,
            name=round_.name,
            type=round_.type,
            status=round_.status,
            races=tuple(Win5Race(id=race.id, name=race.name) for race in race_targets),
        )
        try:
            for winner in command.winners:
                validate_special_round_result_structure(
                    domain_round,
                    Win5RaceResult(
                        race_id=winner.race_id,
                        placements=(
                            Win5Placement(
                                race_entry_id=None,
                                position=1,
                                gate_number=winner.gate_number,
                            ),
                        ),
                    ),
                )
        except Win5DomainError as exc:
            raise Win5SpecialResultInvalidSourceError(str(exc)) from exc

        try:
            current = tuple(
                sorted(repository.lock_current_results(round_id=round_.id), key=lambda winner: winner.race_id)
            )
        except ValueError as exc:
            raise Win5SpecialResultInvalidSourceError(str(exc)) from exc
        if repository.has_score_events(round_id=round_.id):
            raise Win5SpecialResultImmutableError("A Round with score events has immutable Result facts.")

        changed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        if not current:
            if command.expected_result_fingerprint is not None:
                raise Win5SpecialResultVersionConflictError(
                    "Initial Special result entry requires no expected result fingerprint."
                )
            persisted = repository.create_results(
                winners=command.winners,
                created_at=changed_at,
            )
            operation_type = Win5SpecialResultAuditType.ENTERED
            before = None
        else:
            if len(current) != len(non_void_race_ids) or {winner.race_id for winner in current} != non_void_race_ids:
                raise Win5SpecialResultInvalidSourceError(
                    "Stored Special result must contain exactly one winner for every non-void Race."
                )
            try:
                current_fingerprint = fingerprint_special_result(current)
            except Win5DomainError as exc:
                raise Win5SpecialResultInvalidSourceError(str(exc)) from exc
            if command.expected_result_fingerprint != current_fingerprint:
                raise Win5SpecialResultVersionConflictError(
                    "Special result fingerprint conflict: "
                    f"expected {command.expected_result_fingerprint!r}, current {current_fingerprint}."
                )
            before = SavedSpecialWin5Result(
                season_id=round_.season_id,
                round_id=round_.id,
                result_fingerprint=current_fingerprint,
                winners=current,
            )
            current_by_race = {winner.race_id: winner for winner in current}
            persisted = tuple(
                Win5SpecialResultWinner(
                    id=current_by_race[winner.race_id].id,
                    race_id=winner.race_id,
                    gate_number=winner.gate_number,
                )
                for winner in command.winners
            )
            desired_identity = tuple((winner.race_id, winner.gate_number) for winner in persisted)
            current_identity = tuple((winner.race_id, winner.gate_number) for winner in current)
            if desired_identity == current_identity:
                return before
            repository.replace_results(winners=persisted)
            operation_type = Win5SpecialResultAuditType.CORRECTED

        persisted = tuple(sorted(persisted, key=lambda winner: winner.race_id))
        if len(persisted) != len(non_void_race_ids) or {winner.race_id for winner in persisted} != non_void_race_ids:
            raise Win5SpecialResultInvalidSourceError("Persisted Special result does not cover every non-void Race.")
        result_fingerprint = fingerprint_special_result(persisted)
        after = SavedSpecialWin5Result(
            season_id=round_.season_id,
            round_id=round_.id,
            result_fingerprint=result_fingerprint,
            winners=persisted,
            operation_type=operation_type,
        )
        record = Win5SpecialResultAuditRecord(
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
        stored: StoredWin5SpecialResultOperation,
        command: SaveSpecialWin5Result,
    ) -> SavedSpecialWin5Result:
        try:
            operation_type = Win5SpecialResultAuditType(stored.type)
        except (TypeError, ValueError) as exc:
            raise Win5SpecialResultIdempotencyConflictError(
                "Idempotency key is already bound to a different logical operation."
            ) from exc
        if stored.request_fingerprint != command.request_fingerprint or stored.round_id != command.round_id:
            raise Win5SpecialResultIdempotencyConflictError(
                "Idempotency key is already bound to a different logical operation."
            )
        if stored.after_data is None:
            raise Win5SpecialResultAuditError("Exact-retry operation has no stored result payload.")
        try:
            result = SavedSpecialWin5Result.from_audit_payload(
                stored.after_data,
                operation_type=operation_type,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise Win5SpecialResultAuditError("Exact-retry operation has malformed stored result payload.") from exc
        if result.round_id != command.round_id:
            raise Win5SpecialResultAuditError("Exact-retry result does not belong to the requested Round.")
        return result
