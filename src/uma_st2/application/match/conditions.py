"""Native V2 Circle Match condition mutation boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.match import (
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
)
from uma_st2.shared import normalize_utc_datetime

MATCH_CONDITION_AUDIT_SCHEMA_VERSION: Final = 1
_EDITABLE_STATUSES: Final = frozenset({MatchStatus.SCHEDULED, MatchStatus.ENTRY_CONFIRMED})


class MatchConditionAuditType(StrEnum):
    """Canonical operation types for complete Match condition writes."""

    SET = "match_conditions_set"
    CHANGED = "match_conditions_changed"


class MatchConditionError(ValueError):
    """Base error for rejected Match condition commands."""


class MatchConditionUnavailableError(MatchConditionError):
    """The Match does not allow native condition mutation."""


class MatchConditionStaleError(MatchConditionError):
    """The condition facts changed after the operator preview."""


class MatchConditionReasonRequiredError(MatchConditionError):
    """Replacing existing condition metadata requires an operator reason."""


class MatchConditionNoChangeError(MatchConditionError):
    """The requested complete replacement changes no condition value."""


class MatchConditionIdempotencyConflictError(MatchConditionError):
    """An idempotency key is bound to another logical operation."""


class MatchConditionAuditError(MatchConditionError):
    """Stored exact-retry evidence is absent or malformed."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _normalized_string(
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
    if optional and not normalized:
        return None
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


@dataclass(frozen=True, slots=True)
class MatchConditionValues:
    """One complete canonical Match condition value set."""

    season: MatchSeason
    weather: MatchWeather
    time_of_day: MatchTimeOfDay
    track_condition: MatchTrackCondition

    def __post_init__(self) -> None:
        object.__setattr__(self, "season", MatchSeason(self.season))
        object.__setattr__(self, "weather", MatchWeather(self.weather))
        object.__setattr__(self, "time_of_day", MatchTimeOfDay(self.time_of_day))
        object.__setattr__(self, "track_condition", MatchTrackCondition(self.track_condition))

    def to_audit_payload(self) -> dict[str, str]:
        return {
            "season": self.season.value,
            "weather": self.weather.value,
            "time_of_day": self.time_of_day.value,
            "track_condition": self.track_condition.value,
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> MatchConditionValues:
        return cls(
            season=MatchSeason(_payload_string(payload, "season")),
            weather=MatchWeather(_payload_string(payload, "weather")),
            time_of_day=MatchTimeOfDay(_payload_string(payload, "time_of_day")),
            track_condition=MatchTrackCondition(_payload_string(payload, "track_condition")),
        )


@dataclass(frozen=True, slots=True)
class MatchConditionRecord:
    """Current complete condition row detached from persistence."""

    values: MatchConditionValues
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.values, MatchConditionValues):
            raise ValueError("values must be MatchConditionValues.")
        object.__setattr__(self, "created_at", normalize_utc_datetime(self.created_at, field_name="created_at"))
        object.__setattr__(self, "updated_at", normalize_utc_datetime(self.updated_at, field_name="updated_at"))

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "values": self.values.to_audit_payload(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> MatchConditionRecord:
        values = payload["values"]
        if not isinstance(values, Mapping):
            raise ValueError("values must be an object.")
        return cls(
            values=MatchConditionValues.from_audit_payload(values),
            created_at=datetime.fromisoformat(_payload_string(payload, "created_at")),
            updated_at=datetime.fromisoformat(_payload_string(payload, "updated_at")),
        )


@dataclass(frozen=True, slots=True)
class MatchConditionTarget:
    """Locked or queried Match facts relevant to condition mutation."""

    match_id: int
    name: str
    source_kind: MatchSourceKind
    status: MatchStatus
    scheduled_at: datetime
    condition: MatchConditionRecord | None
    condition_version: int | None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(self, "name", _normalized_string(self.name, field_name="name", max_length=200))
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchStatus(self.status))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        if self.condition is not None and not isinstance(self.condition, MatchConditionRecord):
            raise ValueError("condition must be MatchConditionRecord or null.")
        if self.condition_version is not None:
            _require_positive_int(self.condition_version, field_name="condition_version")
        if self.condition is None and self.condition_version is not None:
            raise ValueError("condition_version requires a current condition row.")

    def to_audit_payload(self) -> dict[str, object]:
        if self.condition is None:
            raise ValueError("A committed Match condition snapshot requires a condition row.")
        return {
            "schema_version": MATCH_CONDITION_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "name": self.name,
            "source_kind": self.source_kind.value,
            "status": self.status.value,
            "scheduled_at": self.scheduled_at.isoformat(),
            "condition": self.condition.to_audit_payload(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> MatchConditionTarget:
        if payload.get("schema_version") != MATCH_CONDITION_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Match condition audit schema version.")
        condition = payload["condition"]
        if not isinstance(condition, Mapping):
            raise ValueError("condition must be an object.")
        return cls(
            match_id=_payload_positive_int(payload, "match_id"),
            name=_payload_string(payload, "name"),
            source_kind=MatchSourceKind(_payload_string(payload, "source_kind")),
            status=MatchStatus(_payload_string(payload, "status")),
            scheduled_at=datetime.fromisoformat(_payload_string(payload, "scheduled_at")),
            condition=MatchConditionRecord.from_audit_payload(condition),
            condition_version=None,
        )


@dataclass(frozen=True, slots=True)
class SetMatchConditions:
    """Set the complete desired condition row against one previewed version."""

    match_id: int
    values: MatchConditionValues
    expected_condition: MatchConditionRecord | None
    expected_condition_version: int | None
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str | None = None
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        if not isinstance(self.values, MatchConditionValues):
            raise ValueError("values must be MatchConditionValues.")
        if self.expected_condition is not None and not isinstance(self.expected_condition, MatchConditionRecord):
            raise ValueError("expected_condition must be MatchConditionRecord or null.")
        if self.expected_condition_version is not None:
            _require_positive_int(self.expected_condition_version, field_name="expected_condition_version")
        if self.expected_condition is None and self.expected_condition_version is not None:
            raise ValueError("expected_condition_version requires expected_condition.")
        for field_name, max_length, optional in (
            ("idempotency_key", 128, False),
            ("actor_discord_user_id", 32, False),
            ("guild_id", 32, True),
            ("correlation_id", 128, True),
            ("reason", 255, True),
        ):
            object.__setattr__(
                self,
                field_name,
                _normalized_string(
                    getattr(self, field_name),
                    field_name=field_name,
                    max_length=max_length,
                    optional=optional,
                ),
            )

    @property
    def request_fingerprint(self) -> str:
        canonical = {
            "schema": "match-condition-command-v1",
            "match_id": self.match_id,
            "values": self.values.to_audit_payload(),
            "expected_condition": (
                None if self.expected_condition is None else self.expected_condition.to_audit_payload()
            ),
            "expected_condition_version": self.expected_condition_version,
        }
        encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class UpdatedMatchConditions:
    """Closed-session result of one complete condition mutation."""

    snapshot: MatchConditionTarget
    operation_type: MatchConditionAuditType

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, MatchConditionTarget) or self.snapshot.condition is None:
            raise ValueError("snapshot must contain committed Match conditions.")
        object.__setattr__(self, "operation_type", MatchConditionAuditType(self.operation_type))


@dataclass(frozen=True, slots=True)
class StoredMatchConditionOperation:
    """Minimal persisted operation facts used to resolve an exact retry."""

    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    after_data: Mapping[str, object] | None


class MatchConditionRepository(Protocol):
    """Persistence operations required by native Match condition mutation."""

    def lock_target(self, *, match_id: int) -> MatchConditionTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredMatchConditionOperation | None: ...

    def replace_conditions(
        self,
        *,
        target: MatchConditionTarget,
        values: MatchConditionValues,
        changed_at: datetime,
    ) -> MatchConditionTarget: ...

    def add_audit(
        self,
        *,
        command: SetMatchConditions,
        operation_type: MatchConditionAuditType,
        before: MatchConditionRecord | None,
        after: MatchConditionTarget,
        created_at: datetime,
    ) -> None: ...


class MatchConditionUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing native Match condition persistence."""

    @property
    def match_conditions(self) -> MatchConditionRepository: ...


@dataclass(frozen=True, slots=True)
class MatchConditionCommands:
    """Application entry point for atomic complete condition mutation."""

    command_runner: CommandRunner[MatchConditionUnitOfWork]
    clock: Callable[[], datetime]

    def set_conditions(self, command: SetMatchConditions) -> UpdatedMatchConditions:
        return self.command_runner.run(
            lambda unit_of_work: self._set_conditions(unit_of_work.match_conditions, command)
        )

    def _set_conditions(
        self,
        repository: MatchConditionRepository,
        command: SetMatchConditions,
    ) -> UpdatedMatchConditions:
        target = repository.lock_target(match_id=command.match_id)
        if target is None:
            raise MatchConditionUnavailableError("The selected Match does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if target.source_kind != MatchSourceKind.NATIVE_V2 or target.status not in _EDITABLE_STATUSES:
            raise MatchConditionUnavailableError("The selected Match no longer allows condition changes.")
        if (
            target.condition != command.expected_condition
            or target.condition_version != command.expected_condition_version
        ):
            raise MatchConditionStaleError("Match conditions changed after the preview was opened.")
        if target.condition is not None:
            if target.condition.values == command.values:
                raise MatchConditionNoChangeError("The requested Match conditions are already current.")
            if command.reason is None:
                raise MatchConditionReasonRequiredError("Replacing existing Match conditions requires a reason.")

        changed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        operation_type = MatchConditionAuditType.SET if target.condition is None else MatchConditionAuditType.CHANGED
        after = repository.replace_conditions(target=target, values=command.values, changed_at=changed_at)
        if (
            after.match_id != target.match_id
            or after.name != target.name
            or after.source_kind != target.source_kind
            or after.status != target.status
            or after.scheduled_at != target.scheduled_at
            or after.condition is None
            or after.condition.values != command.values
            or after.condition.updated_at != changed_at
        ):
            raise MatchConditionAuditError("Stored Match condition facts do not match the requested replacement.")
        committed = replace(after, condition_version=None)
        repository.add_audit(
            command=command,
            operation_type=operation_type,
            before=target.condition,
            after=committed,
            created_at=changed_at,
        )
        return UpdatedMatchConditions(snapshot=committed, operation_type=operation_type)

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredMatchConditionOperation,
        command: SetMatchConditions,
    ) -> UpdatedMatchConditions:
        try:
            operation_type = MatchConditionAuditType(stored.type)
        except (TypeError, ValueError) as exc:
            raise MatchConditionIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from exc
        if stored.request_fingerprint != command.request_fingerprint:
            raise MatchConditionIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise MatchConditionAuditError("Exact-retry operation has no stored Match condition payload.")
        try:
            snapshot = MatchConditionTarget.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchConditionAuditError("Exact-retry Match condition payload is malformed.") from exc
        if (
            snapshot.match_id != stored.match_id
            or snapshot.match_id != command.match_id
            or snapshot.source_kind != MatchSourceKind.NATIVE_V2
            or snapshot.condition is None
            or snapshot.condition.values != command.values
        ):
            raise MatchConditionAuditError("Exact-retry Match condition payload does not match its operation context.")
        return UpdatedMatchConditions(snapshot=snapshot, operation_type=operation_type)
