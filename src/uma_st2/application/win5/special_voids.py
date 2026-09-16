"""Special WIN5 Race-void mutation boundary and audit contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.win5 import (
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    fingerprint_special_void_state,
)
from uma_st2.shared import normalize_utc_datetime

WIN5_SPECIAL_VOID_AUDIT_SCHEMA_VERSION: Final = 1


class Win5SpecialVoidAuditType(StrEnum):
    """Canonical operation types for current Special Race-void changes."""

    VOIDED = "special_race_voided"
    RESTORED = "special_race_void_restored"
    ALL_VOID_CANCELLED = "special_round_all_void_cancelled"


class Win5SpecialVoidError(ValueError):
    """Base error for rejected Special Race-void commands."""


class Win5SpecialVoidUnavailableError(Win5SpecialVoidError):
    """The target Round cannot change its current void facts."""


class Win5SpecialVoidImmutableError(Win5SpecialVoidError):
    """The target already owns terminal scoring or cancellation facts."""


class Win5SpecialVoidInvalidSourceError(Win5SpecialVoidError):
    """Stored Round, Race, Result, or void facts are malformed."""


class Win5SpecialVoidVersionConflictError(Win5SpecialVoidError):
    """The command expected another complete current void set."""


class Win5SpecialVoidIdempotencyConflictError(Win5SpecialVoidError):
    """An idempotency key is already bound to another logical command."""


class Win5SpecialVoidAuditError(Win5SpecialVoidError):
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
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"{field_name} must be a non-empty string of at most {max_length} characters.")


def _require_sha256(value: str, *, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
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


def _payload_bool(payload: Mapping[str, object], key: str) -> bool:
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean.")
    return value


def _payload_positive_int_tuple(payload: Mapping[str, object], key: str) -> tuple[int, ...]:
    raw_values = payload[key]
    if not isinstance(raw_values, list):
        raise ValueError(f"{key} must be a JSON list.")
    values = tuple(raw_values)
    for value in values:
        _require_positive_int(value, field_name=key)  # type: ignore[arg-type]
    if len(set(values)) != len(values):
        raise ValueError(f"{key} must contain unique positive integers.")
    return tuple(sorted(values))  # type: ignore[arg-type,return-value]


def _datetime_to_payload(value: datetime, *, field_name: str) -> str:
    return normalize_utc_datetime(value, field_name=field_name).isoformat()


def _datetime_from_payload(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be an ISO datetime string.")
    return normalize_utc_datetime(datetime.fromisoformat(value), field_name=field_name)


@dataclass(frozen=True, slots=True)
class SetSpecialWin5RaceVoid:
    """Mark or restore one Race against an exact complete-void-set token."""

    round_id: int
    race_id: int
    voided: bool
    expected_void_fingerprint: str
    idempotency_key: str
    actor_discord_user_id: str
    reason: str
    guild_id: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.round_id, field_name="round_id")
        _require_positive_int(self.race_id, field_name="race_id")
        if not isinstance(self.voided, bool):
            raise ValueError("voided must be a boolean.")
        _require_sha256(self.expected_void_fingerprint, field_name="expected_void_fingerprint")
        _require_bounded_string(self.idempotency_key, field_name="idempotency_key", max_length=128)
        _require_bounded_string(
            self.actor_discord_user_id,
            field_name="actor_discord_user_id",
            max_length=32,
        )
        _require_bounded_string(self.reason, field_name="reason", max_length=255)
        object.__setattr__(self, "reason", self.reason.strip())
        _require_bounded_string(self.guild_id, field_name="guild_id", max_length=32, optional=True)
        _require_bounded_string(
            self.correlation_id,
            field_name="correlation_id",
            max_length=128,
            optional=True,
        )

    @property
    def request_fingerprint(self) -> str:
        canonical = "\n".join(
            (
                "win5-special-race-void-command-v1",
                f"round:{self.round_id}",
                f"race:{self.race_id}",
                f"voided:{str(self.voided).lower()}",
                f"expected:{self.expected_void_fingerprint}",
            )
        )
        return sha256(canonical.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class CancelSpecialWin5Round:
    """Atomically void every remaining Race and terminally cancel one Round."""

    round_id: int
    expected_void_fingerprint: str
    idempotency_key: str
    actor_discord_user_id: str
    reason: str
    guild_id: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.round_id, field_name="round_id")
        _require_sha256(self.expected_void_fingerprint, field_name="expected_void_fingerprint")
        _require_bounded_string(self.idempotency_key, field_name="idempotency_key", max_length=128)
        _require_bounded_string(
            self.actor_discord_user_id,
            field_name="actor_discord_user_id",
            max_length=32,
        )
        _require_bounded_string(self.reason, field_name="reason", max_length=255)
        object.__setattr__(self, "reason", self.reason.strip())
        _require_bounded_string(self.guild_id, field_name="guild_id", max_length=32, optional=True)
        _require_bounded_string(
            self.correlation_id,
            field_name="correlation_id",
            max_length=128,
            optional=True,
        )

    @property
    def request_fingerprint(self) -> str:
        canonical = "\n".join(
            (
                "win5-special-round-cancellation-command-v1",
                f"round:{self.round_id}",
                f"expected:{self.expected_void_fingerprint}",
            )
        )
        return sha256(canonical.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class Win5SpecialVoidRoundTarget:
    """Locked Special Round and parent Season state."""

    id: int
    season_id: int
    type: Win5RoundType
    source_kind: Win5RoundSourceKind
    status: Win5RoundStatus
    season_status: Win5SeasonStatus

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.season_id, field_name="season_id")
        object.__setattr__(self, "type", Win5RoundType(self.type))
        object.__setattr__(self, "status", Win5RoundStatus(self.status))
        object.__setattr__(self, "season_status", Win5SeasonStatus(self.season_status))
        object.__setattr__(self, "source_kind", Win5RoundSourceKind(self.source_kind))


@dataclass(frozen=True, slots=True)
class Win5SpecialRaceVoidFact:
    """One explicit current Race-void fact."""

    race_id: int
    reason: str
    voided_at: datetime

    def __post_init__(self) -> None:
        _require_positive_int(self.race_id, field_name="race_id")
        _require_bounded_string(self.reason, field_name="reason", max_length=255)
        object.__setattr__(self, "reason", self.reason.strip())
        object.__setattr__(
            self,
            "voided_at",
            normalize_utc_datetime(self.voided_at, field_name="voided_at"),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "race_id": self.race_id,
            "reason": self.reason,
            "voided_at": _datetime_to_payload(self.voided_at, field_name="voided_at"),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> Win5SpecialRaceVoidFact:
        return cls(
            race_id=_payload_positive_int(payload, "race_id"),
            reason=_payload_string(payload, "reason"),
            voided_at=_datetime_from_payload(payload["voided_at"], field_name="voided_at"),
        )


@dataclass(frozen=True, slots=True)
class Win5SpecialVoidResultSnapshot:
    """One unscored Special winner retained when a void change resets Results."""

    id: int
    race_id: int
    gate_number: int

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.race_id, field_name="race_id")
        _require_positive_int(self.gate_number, field_name="gate_number")

    def to_payload(self) -> dict[str, object]:
        return {
            "result_id": self.id,
            "race_id": self.race_id,
            "position": 1,
            "gate_number": self.gate_number,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> Win5SpecialVoidResultSnapshot:
        if _payload_positive_int(payload, "position") != 1:
            raise ValueError("Special void Result snapshot must use position 1.")
        return cls(
            id=_payload_positive_int(payload, "result_id"),
            race_id=_payload_positive_int(payload, "race_id"),
            gate_number=_payload_positive_int(payload, "gate_number"),
        )


@dataclass(frozen=True, slots=True)
class Win5SpecialVoidStateSnapshot:
    """Complete current void set and unscored Result bundle under one Round lock."""

    season_id: int
    round_id: int
    round_status: Win5RoundStatus
    race_ids: tuple[int, ...]
    voids: tuple[Win5SpecialRaceVoidFact, ...] = field(default_factory=tuple)
    results: tuple[Win5SpecialVoidResultSnapshot, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        object.__setattr__(self, "round_status", Win5RoundStatus(self.round_status))
        race_ids = tuple(sorted(self.race_ids))
        if not race_ids or len(set(race_ids)) != len(race_ids):
            raise ValueError("Special void state requires non-empty unique Race IDs.")
        for race_id in race_ids:
            _require_positive_int(race_id, field_name="race_id")
        object.__setattr__(self, "race_ids", race_ids)

        voids = tuple(sorted(self.voids, key=lambda item: item.race_id))
        void_ids = tuple(item.race_id for item in voids)
        if len(set(void_ids)) != len(void_ids) or not set(void_ids) <= set(race_ids):
            raise ValueError("Special void facts must be unique members of the Round Race graph.")
        object.__setattr__(self, "voids", voids)

        results = tuple(sorted(self.results, key=lambda item: item.race_id))
        result_ids = tuple(item.id for item in results)
        result_race_ids = tuple(item.race_id for item in results)
        if len(set(result_ids)) != len(result_ids) or len(set(result_race_ids)) != len(result_race_ids):
            raise ValueError("Special void Result snapshot contains duplicate authority rows.")
        non_void_ids = tuple(race_id for race_id in race_ids if race_id not in set(void_ids))
        if results and result_race_ids != non_void_ids:
            raise ValueError("Special void state Results must be empty or complete for every non-void Race.")
        object.__setattr__(self, "results", results)

        if self.round_status == Win5RoundStatus.CANCELLED:
            if void_ids != race_ids or results:
                raise ValueError("A cancelled Special void state must be all-void and contain no Result.")
        elif self.round_status == Win5RoundStatus.CLOSED:
            if void_ids == race_ids:
                raise ValueError("An all-void Special Round must be terminal cancelled.")
        else:
            raise ValueError("Special void state supports only closed or cancelled Rounds.")

    @property
    def void_fingerprint(self) -> str:
        return fingerprint_special_void_state(tuple(item.race_id for item in self.voids))

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": WIN5_SPECIAL_VOID_AUDIT_SCHEMA_VERSION,
            "season_id": self.season_id,
            "round_id": self.round_id,
            "round_status": self.round_status.value,
            "race_ids": list(self.race_ids),
            "void_fingerprint": self.void_fingerprint,
            "voids": [void.to_payload() for void in self.voids],
            "results": [result.to_payload() for result in self.results],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> Win5SpecialVoidStateSnapshot:
        if _payload_positive_int(payload, "schema_version") != WIN5_SPECIAL_VOID_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Special void audit schema version.")
        raw_race_ids = payload["race_ids"]
        raw_voids = payload["voids"]
        raw_results = payload["results"]
        if not isinstance(raw_race_ids, list):
            raise ValueError("race_ids must be a JSON list.")
        if not isinstance(raw_voids, list) or not all(isinstance(item, Mapping) for item in raw_voids):
            raise ValueError("voids must be a JSON list of objects.")
        if not isinstance(raw_results, list) or not all(isinstance(item, Mapping) for item in raw_results):
            raise ValueError("results must be a JSON list of objects.")
        state = cls(
            season_id=_payload_positive_int(payload, "season_id"),
            round_id=_payload_positive_int(payload, "round_id"),
            round_status=Win5RoundStatus(_payload_string(payload, "round_status")),
            race_ids=tuple(raw_race_ids),  # type: ignore[arg-type]
            voids=tuple(Win5SpecialRaceVoidFact.from_payload(item) for item in raw_voids),
            results=tuple(Win5SpecialVoidResultSnapshot.from_payload(item) for item in raw_results),
        )
        if _payload_string(payload, "void_fingerprint") != state.void_fingerprint:
            raise ValueError("Stored Special void fingerprint does not match its void facts.")
        return state


@dataclass(frozen=True, slots=True)
class UpdatedSpecialWin5Void:
    """Committed current state returned after mutation, no-op, or exact retry."""

    state: Win5SpecialVoidStateSnapshot
    target_race_id: int
    target_voided: bool
    operation_type: Win5SpecialVoidAuditType | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.target_race_id, field_name="target_race_id")
        if self.target_race_id not in set(self.state.race_ids):
            raise ValueError("Special void result target Race is outside its Round graph.")
        if not isinstance(self.target_voided, bool):
            raise ValueError("target_voided must be a boolean.")
        current_voided = self.target_race_id in {item.race_id for item in self.state.voids}
        if current_voided != self.target_voided:
            raise ValueError("Special void result does not reflect the requested target state.")
        if self.operation_type is not None:
            object.__setattr__(self, "operation_type", Win5SpecialVoidAuditType(self.operation_type))

    def to_audit_payload(self) -> dict[str, object]:
        if self.operation_type is None:
            raise ValueError("A no-op Special void result has no audit payload.")
        return {
            **self.state.to_payload(),
            "command_scope": "race",
            "target_race_id": self.target_race_id,
            "target_voided": self.target_voided,
            "operation_type": self.operation_type.value,
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> UpdatedSpecialWin5Void:
        if _payload_string(payload, "command_scope") != "race":
            raise ValueError("Special Race-void audit payload has the wrong command scope.")
        return cls(
            state=Win5SpecialVoidStateSnapshot.from_payload(payload),
            target_race_id=_payload_positive_int(payload, "target_race_id"),
            target_voided=_payload_bool(payload, "target_voided"),
            operation_type=Win5SpecialVoidAuditType(_payload_string(payload, "operation_type")),
        )


@dataclass(frozen=True, slots=True)
class CancelledSpecialWin5Round:
    """Committed all-void terminal state returned by the batch command."""

    state: Win5SpecialVoidStateSnapshot
    newly_voided_race_ids: tuple[int, ...]
    operation_type: Win5SpecialVoidAuditType = Win5SpecialVoidAuditType.ALL_VOID_CANCELLED

    def __post_init__(self) -> None:
        race_ids = tuple(sorted(self.newly_voided_race_ids))
        if not race_ids or len(set(race_ids)) != len(race_ids):
            raise ValueError("Round cancellation must void one or more unique Races.")
        for race_id in race_ids:
            _require_positive_int(race_id, field_name="newly_voided_race_id")
        if not set(race_ids) <= set(self.state.race_ids):
            raise ValueError("Round cancellation contains a Race outside its Round graph.")
        object.__setattr__(self, "newly_voided_race_ids", race_ids)
        object.__setattr__(self, "operation_type", Win5SpecialVoidAuditType(self.operation_type))
        if self.operation_type != Win5SpecialVoidAuditType.ALL_VOID_CANCELLED:
            raise ValueError("Round cancellation must use the all-void audit operation type.")
        if self.state.round_status != Win5RoundStatus.CANCELLED:
            raise ValueError("Round cancellation must return a terminal cancelled state.")
        if tuple(void.race_id for void in self.state.voids) != self.state.race_ids or self.state.results:
            raise ValueError("Round cancellation must return an all-void state without Results.")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            **self.state.to_payload(),
            "command_scope": "round",
            "newly_voided_race_ids": list(self.newly_voided_race_ids),
            "operation_type": self.operation_type.value,
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> CancelledSpecialWin5Round:
        if _payload_string(payload, "command_scope") != "round":
            raise ValueError("Special Round cancellation audit payload has the wrong command scope.")
        return cls(
            state=Win5SpecialVoidStateSnapshot.from_payload(payload),
            newly_voided_race_ids=_payload_positive_int_tuple(payload, "newly_voided_race_ids"),
            operation_type=Win5SpecialVoidAuditType(_payload_string(payload, "operation_type")),
        )


@dataclass(frozen=True, slots=True)
class Win5SpecialVoidAuditRecord:
    """One complete before/after Special void mutation fact set."""

    type: Win5SpecialVoidAuditType
    before: Win5SpecialVoidStateSnapshot
    after: UpdatedSpecialWin5Void

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", Win5SpecialVoidAuditType(self.type))
        if self.after.operation_type != self.type:
            raise ValueError("Special void audit type does not match its after result.")
        if (self.before.season_id, self.before.round_id, self.before.race_ids) != (
            self.after.state.season_id,
            self.after.state.round_id,
            self.after.state.race_ids,
        ):
            raise ValueError("Special void mutation must preserve Season/Round/Race identity.")
        before_voided = self.after.target_race_id in {item.race_id for item in self.before.voids}
        if before_voided == self.after.target_voided:
            raise ValueError("A no-op Special void change must not create an audit record.")
        expected_type = (
            Win5SpecialVoidAuditType.RESTORED
            if not self.after.target_voided
            else Win5SpecialVoidAuditType.ALL_VOID_CANCELLED
            if self.after.state.round_status == Win5RoundStatus.CANCELLED
            else Win5SpecialVoidAuditType.VOIDED
        )
        if self.type != expected_type:
            raise ValueError("Special void audit type does not match its transition.")
        if self.after.state.results:
            raise ValueError("A changed Special void set must reset the current Result bundle.")

    @property
    def before_data(self) -> dict[str, object]:
        return self.before.to_payload()

    @property
    def after_data(self) -> dict[str, object]:
        return self.after.to_audit_payload()


@dataclass(frozen=True, slots=True)
class Win5SpecialRoundCancellationAuditRecord:
    """One atomic whole-Round cancellation and its complete provenance."""

    before: Win5SpecialVoidStateSnapshot
    after: CancelledSpecialWin5Round

    def __post_init__(self) -> None:
        if (self.before.season_id, self.before.round_id, self.before.race_ids) != (
            self.after.state.season_id,
            self.after.state.round_id,
            self.after.state.race_ids,
        ):
            raise ValueError("Special Round cancellation must preserve Season/Round/Race identity.")
        before_void_ids = {item.race_id for item in self.before.voids}
        expected_new_ids = tuple(race_id for race_id in self.before.race_ids if race_id not in before_void_ids)
        if self.after.newly_voided_race_ids != expected_new_ids:
            raise ValueError("Special Round cancellation must void every previously non-void Race exactly once.")
        retained_by_race_id = {item.race_id: item for item in self.after.state.voids}
        if any(retained_by_race_id.get(item.race_id) != item for item in self.before.voids):
            raise ValueError("Special Round cancellation must retain existing Race-void provenance.")
        new_facts = tuple(retained_by_race_id[race_id] for race_id in expected_new_ids)
        if len({(item.reason, item.voided_at) for item in new_facts}) != 1:
            raise ValueError("Special Round cancellation must use one shared reason/time for every new void fact.")

    @property
    def before_data(self) -> dict[str, object]:
        return self.before.to_payload()

    @property
    def after_data(self) -> dict[str, object]:
        return self.after.to_audit_payload()


@dataclass(frozen=True, slots=True)
class StoredWin5SpecialVoidOperation:
    """Minimal persisted operation state used for exact retries."""

    request_fingerprint: str | None
    type: str | None
    round_id: int | None
    after_data: Mapping[str, object] | None


class Win5SpecialVoidRepository(Protocol):
    """Persistence operations required by Special Race-void mutation."""

    def lock_round(self, *, round_id: int) -> Win5SpecialVoidRoundTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SpecialVoidOperation | None: ...

    def lock_state(self, *, round_: Win5SpecialVoidRoundTarget) -> Win5SpecialVoidStateSnapshot: ...

    def has_score_events(self, *, round_id: int) -> bool: ...

    def clear_results(self, *, results: tuple[Win5SpecialVoidResultSnapshot, ...]) -> None: ...

    def update_race_void(
        self,
        *,
        race_id: int,
        expected_voided: bool,
        target_voided: bool,
        reason: str,
        changed_at: datetime,
    ) -> None: ...

    def void_races(
        self,
        *,
        race_ids: tuple[int, ...],
        reason: str,
        changed_at: datetime,
    ) -> None: ...

    def cancel_round(self, *, round_id: int, changed_at: datetime) -> None: ...

    def add_void_audit(
        self,
        *,
        command: SetSpecialWin5RaceVoid,
        record: Win5SpecialVoidAuditRecord,
        created_at: datetime,
    ) -> None: ...

    def add_round_cancellation_audit(
        self,
        *,
        command: CancelSpecialWin5Round,
        record: Win5SpecialRoundCancellationAuditRecord,
        created_at: datetime,
    ) -> None: ...


class Win5SpecialVoidUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the Special void repository."""

    @property
    def win5_special_voids(self) -> Win5SpecialVoidRepository: ...


@dataclass(frozen=True, slots=True)
class Win5SpecialVoidCommands:
    """Application entry point for Race void/restore and batch Round cancellation."""

    command_runner: CommandRunner[Win5SpecialVoidUnitOfWork]
    clock: Callable[[], datetime]

    def set_race_void(self, command: SetSpecialWin5RaceVoid) -> UpdatedSpecialWin5Void:
        return self.command_runner.run(
            lambda unit_of_work: self._set_race_void(unit_of_work.win5_special_voids, command)
        )

    def cancel_round(self, command: CancelSpecialWin5Round) -> CancelledSpecialWin5Round:
        return self.command_runner.run(
            lambda unit_of_work: self._cancel_round(unit_of_work.win5_special_voids, command)
        )

    def _set_race_void(
        self,
        repository: Win5SpecialVoidRepository,
        command: SetSpecialWin5RaceVoid,
    ) -> UpdatedSpecialWin5Void:
        round_ = repository.lock_round(round_id=command.round_id)
        if round_ is None:
            raise Win5SpecialVoidUnavailableError("WIN5 Round does not exist.")
        if round_.source_kind != Win5RoundSourceKind.NATIVE_V2:
            raise Win5SpecialVoidUnavailableError("Imported WIN5 Rounds are read-only.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_race_exact_retry(stored=stored, command=command)

        if round_.status in {Win5RoundStatus.SCORED, Win5RoundStatus.CANCELLED}:
            raise Win5SpecialVoidImmutableError("A scored or cancelled WIN5 Round has immutable void facts.")
        if (
            round_.type != Win5RoundType.SPECIAL
            or round_.season_status != Win5SeasonStatus.ACTIVE
            or round_.status != Win5RoundStatus.CLOSED
        ):
            raise Win5SpecialVoidUnavailableError(
                "Special Race void changes require a closed Special Round in an active Season."
            )

        try:
            before = repository.lock_state(round_=round_)
        except (TypeError, ValueError) as exc:
            raise Win5SpecialVoidInvalidSourceError(str(exc)) from exc
        if repository.has_score_events(round_id=round_.id):
            raise Win5SpecialVoidImmutableError("A score-backed Round has immutable void facts.")
        if before.void_fingerprint != command.expected_void_fingerprint:
            raise Win5SpecialVoidVersionConflictError(
                "Special void fingerprint conflict: "
                f"expected {command.expected_void_fingerprint!r}, current {before.void_fingerprint}."
            )
        if command.race_id not in set(before.race_ids):
            raise Win5SpecialVoidInvalidSourceError("Special void target Race is outside the locked Round graph.")

        current_voided = command.race_id in {item.race_id for item in before.voids}
        if current_voided == command.voided:
            return UpdatedSpecialWin5Void(
                state=before,
                target_race_id=command.race_id,
                target_voided=command.voided,
            )

        changed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        current_by_race_id = {item.race_id: item for item in before.voids}
        if command.voided:
            current_by_race_id[command.race_id] = Win5SpecialRaceVoidFact(
                race_id=command.race_id,
                reason=command.reason,
                voided_at=changed_at,
            )
        else:
            del current_by_race_id[command.race_id]
        after_voids = tuple(sorted(current_by_race_id.values(), key=lambda item: item.race_id))
        all_void = len(after_voids) == len(before.race_ids)
        target_status = Win5RoundStatus.CANCELLED if all_void else Win5RoundStatus.CLOSED

        repository.clear_results(results=before.results)
        repository.update_race_void(
            race_id=command.race_id,
            expected_voided=current_voided,
            target_voided=command.voided,
            reason=command.reason,
            changed_at=changed_at,
        )
        if all_void:
            repository.cancel_round(round_id=round_.id, changed_at=changed_at)

        operation_type = (
            Win5SpecialVoidAuditType.RESTORED
            if not command.voided
            else Win5SpecialVoidAuditType.ALL_VOID_CANCELLED
            if all_void
            else Win5SpecialVoidAuditType.VOIDED
        )
        after_state = Win5SpecialVoidStateSnapshot(
            season_id=round_.season_id,
            round_id=round_.id,
            round_status=target_status,
            race_ids=before.race_ids,
            voids=after_voids,
            results=(),
        )
        result = UpdatedSpecialWin5Void(
            state=after_state,
            target_race_id=command.race_id,
            target_voided=command.voided,
            operation_type=operation_type,
        )
        repository.add_void_audit(
            command=command,
            record=Win5SpecialVoidAuditRecord(
                type=operation_type,
                before=before,
                after=result,
            ),
            created_at=changed_at,
        )
        return result

    def _cancel_round(
        self,
        repository: Win5SpecialVoidRepository,
        command: CancelSpecialWin5Round,
    ) -> CancelledSpecialWin5Round:
        round_ = repository.lock_round(round_id=command.round_id)
        if round_ is None:
            raise Win5SpecialVoidUnavailableError("WIN5 Round does not exist.")
        if round_.source_kind != Win5RoundSourceKind.NATIVE_V2:
            raise Win5SpecialVoidUnavailableError("Imported WIN5 Rounds are read-only.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_round_cancellation_exact_retry(stored=stored, command=command)

        if round_.status in {Win5RoundStatus.SCORED, Win5RoundStatus.CANCELLED}:
            raise Win5SpecialVoidImmutableError("A scored or cancelled WIN5 Round cannot be cancelled again.")
        if (
            round_.type != Win5RoundType.SPECIAL
            or round_.season_status != Win5SeasonStatus.ACTIVE
            or round_.status != Win5RoundStatus.CLOSED
        ):
            raise Win5SpecialVoidUnavailableError(
                "Whole-Round cancellation requires a closed Special Round in an active Season."
            )

        try:
            before = repository.lock_state(round_=round_)
        except (TypeError, ValueError) as exc:
            raise Win5SpecialVoidInvalidSourceError(str(exc)) from exc
        if repository.has_score_events(round_id=round_.id):
            raise Win5SpecialVoidImmutableError("A score-backed Round cannot be cancelled.")
        if before.void_fingerprint != command.expected_void_fingerprint:
            raise Win5SpecialVoidVersionConflictError(
                "Special void fingerprint conflict: "
                f"expected {command.expected_void_fingerprint!r}, current {before.void_fingerprint}."
            )

        current_void_ids = {item.race_id for item in before.voids}
        newly_voided_race_ids = tuple(race_id for race_id in before.race_ids if race_id not in current_void_ids)
        if not newly_voided_race_ids:
            raise Win5SpecialVoidInvalidSourceError("An all-void Special Round must already be cancelled.")

        changed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        after_voids = tuple(
            sorted(
                (
                    *before.voids,
                    *(
                        Win5SpecialRaceVoidFact(
                            race_id=race_id,
                            reason=command.reason,
                            voided_at=changed_at,
                        )
                        for race_id in newly_voided_race_ids
                    ),
                ),
                key=lambda item: item.race_id,
            )
        )

        repository.clear_results(results=before.results)
        repository.void_races(
            race_ids=newly_voided_race_ids,
            reason=command.reason,
            changed_at=changed_at,
        )
        repository.cancel_round(round_id=round_.id, changed_at=changed_at)

        result = CancelledSpecialWin5Round(
            state=Win5SpecialVoidStateSnapshot(
                season_id=round_.season_id,
                round_id=round_.id,
                round_status=Win5RoundStatus.CANCELLED,
                race_ids=before.race_ids,
                voids=after_voids,
                results=(),
            ),
            newly_voided_race_ids=newly_voided_race_ids,
        )
        repository.add_round_cancellation_audit(
            command=command,
            record=Win5SpecialRoundCancellationAuditRecord(before=before, after=result),
            created_at=changed_at,
        )
        return result

    @staticmethod
    def _resolve_race_exact_retry(
        *,
        stored: StoredWin5SpecialVoidOperation,
        command: SetSpecialWin5RaceVoid,
    ) -> UpdatedSpecialWin5Void:
        try:
            operation_type = Win5SpecialVoidAuditType(stored.type)
        except (TypeError, ValueError) as exc:
            raise Win5SpecialVoidIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from exc
        if stored.request_fingerprint != command.request_fingerprint or stored.round_id != command.round_id:
            raise Win5SpecialVoidIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise Win5SpecialVoidAuditError("Exact-retry operation has no stored Special void payload.")
        try:
            result = UpdatedSpecialWin5Void.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise Win5SpecialVoidAuditError("Exact-retry Special void payload is malformed.") from exc
        if (
            result.operation_type != operation_type
            or result.state.round_id != command.round_id
            or result.target_race_id != command.race_id
            or result.target_voided != command.voided
        ):
            raise Win5SpecialVoidAuditError("Exact-retry Special void payload does not match its command.")
        return result

    @staticmethod
    def _resolve_round_cancellation_exact_retry(
        *,
        stored: StoredWin5SpecialVoidOperation,
        command: CancelSpecialWin5Round,
    ) -> CancelledSpecialWin5Round:
        if (
            stored.type != Win5SpecialVoidAuditType.ALL_VOID_CANCELLED.value
            or stored.request_fingerprint != command.request_fingerprint
            or stored.round_id != command.round_id
        ):
            raise Win5SpecialVoidIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise Win5SpecialVoidAuditError("Exact-retry operation has no stored Round cancellation payload.")
        try:
            result = CancelledSpecialWin5Round.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise Win5SpecialVoidAuditError("Exact-retry Round cancellation payload is malformed.") from exc
        if result.state.round_id != command.round_id:
            raise Win5SpecialVoidAuditError("Exact-retry Round cancellation payload does not match its command.")
        return result
