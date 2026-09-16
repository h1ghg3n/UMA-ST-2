"""Native scheduled Match setup replacement boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.shared import normalize_utc_datetime

from .conditions import MatchConditionRecord, MatchConditionValues
from .creation import MatchCreationCourse

MATCH_SETUP_AUDIT_SCHEMA_VERSION: Final = 1


class MatchSetupAuditType(StrEnum):
    """Canonical operation type for one complete pre-open setup edit."""

    UPDATED = "match_setup_updated"


class MatchSetupError(ValueError):
    """Base error for rejected Match setup commands."""


class MatchSetupUnavailableError(MatchSetupError):
    """The selected Match cannot be edited as a native scheduled setup."""


class MatchSetupStaleError(MatchSetupError):
    """The setup authority changed after the operator preview."""


class MatchSetupReasonRequiredError(MatchSetupError):
    """An existing Match setup change requires an operator reason."""


class MatchSetupNoChangeError(MatchSetupError):
    """The desired setup is identical to current canonical state."""


class MatchSetupIdempotencyConflictError(MatchSetupError):
    """An idempotency key is bound to another logical operation."""


class MatchSetupAuditError(MatchSetupError):
    """Stored setup evidence is absent, malformed, or inconsistent."""


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
class MatchSetupTarget:
    """Detached complete Match setup plus its optimistic concurrency authority."""

    match_id: int
    name: str
    description: str | None
    source_kind: MatchSourceKind
    grade: MatchGrade
    course: MatchCreationCourse
    scheduled_at: datetime
    status: MatchStatus
    condition: MatchConditionRecord | None
    updated_at: datetime
    setup_version: int | None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(self, "name", _normalized_string(self.name, field_name="name", max_length=200))
        object.__setattr__(
            self,
            "description",
            _normalized_string(self.description, field_name="description", max_length=4000, optional=True),
        )
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        if not isinstance(self.course, MatchCreationCourse):
            raise ValueError("course must be a MatchCreationCourse.")
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        object.__setattr__(self, "status", MatchStatus(self.status))
        if self.condition is not None and not isinstance(self.condition, MatchConditionRecord):
            raise ValueError("condition must be MatchConditionRecord or null.")
        object.__setattr__(
            self,
            "updated_at",
            normalize_utc_datetime(self.updated_at, field_name="updated_at"),
        )
        if self.setup_version is not None:
            _require_positive_int(self.setup_version, field_name="setup_version")

    @property
    def state_fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_audit_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def semantic_values(self) -> tuple[object, ...]:
        return (
            self.name,
            self.description,
            self.grade,
            self.course.id,
            self.scheduled_at,
            None if self.condition is None else self.condition.values,
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_SETUP_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "name": self.name,
            "description": self.description,
            "source_kind": self.source_kind.value,
            "grade": self.grade.value,
            "course": self.course.to_audit_payload(),
            "scheduled_at": self.scheduled_at.isoformat(),
            "status": self.status.value,
            "condition": None if self.condition is None else self.condition.to_audit_payload(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> MatchSetupTarget:
        if payload.get("schema_version") != MATCH_SETUP_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Match setup audit schema version.")
        description = payload.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError("description must be a string or null.")
        course = payload["course"]
        if not isinstance(course, Mapping):
            raise ValueError("course must be an object.")
        condition = payload.get("condition")
        if condition is not None and not isinstance(condition, Mapping):
            raise ValueError("condition must be an object or null.")
        return cls(
            match_id=_payload_positive_int(payload, "match_id"),
            name=_payload_string(payload, "name"),
            description=description,
            source_kind=MatchSourceKind(_payload_string(payload, "source_kind")),
            grade=MatchGrade(_payload_string(payload, "grade")),
            course=MatchCreationCourse.from_audit_payload(course),
            scheduled_at=datetime.fromisoformat(_payload_string(payload, "scheduled_at")),
            status=MatchStatus(_payload_string(payload, "status")),
            condition=None if condition is None else MatchConditionRecord.from_audit_payload(condition),
            updated_at=datetime.fromisoformat(_payload_string(payload, "updated_at")),
            setup_version=None,
        )


@dataclass(frozen=True, slots=True)
class UpdateMatchSetup:
    """Replace one native scheduled Match setup against previewed authority."""

    match_id: int
    name: str
    description: str | None
    grade: MatchGrade
    stadium_course_id: int
    scheduled_at: datetime
    condition: MatchConditionValues
    expected_state_fingerprint: str
    expected_setup_version: int | None
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str | None = None
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(self, "name", _normalized_string(self.name, field_name="name", max_length=200))
        object.__setattr__(
            self,
            "description",
            _normalized_string(self.description, field_name="description", max_length=4000, optional=True),
        )
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        _require_positive_int(self.stadium_course_id, field_name="stadium_course_id")
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        if not isinstance(self.condition, MatchConditionValues):
            raise ValueError("condition must be MatchConditionValues.")
        if self.expected_setup_version is not None:
            _require_positive_int(self.expected_setup_version, field_name="expected_setup_version")
        for field_name, max_length, optional in (
            ("expected_state_fingerprint", 64, False),
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
        if len(self.expected_state_fingerprint) != 64:
            raise ValueError("expected_state_fingerprint must be a SHA-256 hex digest.")

    @property
    def desired_semantic_values(self) -> tuple[object, ...]:
        return (
            self.name,
            self.description,
            self.grade,
            self.stadium_course_id,
            self.scheduled_at,
            self.condition,
        )

    @property
    def request_fingerprint(self) -> str:
        canonical = {
            "schema": "match-setup-command-v1",
            "match_id": self.match_id,
            "name": self.name,
            "description": self.description,
            "grade": self.grade.value,
            "stadium_course_id": self.stadium_course_id,
            "scheduled_at": self.scheduled_at.isoformat(),
            "condition": self.condition.to_audit_payload(),
            "expected_state_fingerprint": self.expected_state_fingerprint,
            "expected_setup_version": self.expected_setup_version,
        }
        encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class UpdatedMatchSetup:
    """Closed-session result of one complete scheduled setup edit."""

    snapshot: MatchSetupTarget
    operation_type: MatchSetupAuditType = MatchSetupAuditType.UPDATED

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, MatchSetupTarget) or self.snapshot.condition is None:
            raise ValueError("snapshot must contain one complete Match setup.")
        object.__setattr__(self, "operation_type", MatchSetupAuditType(self.operation_type))


@dataclass(frozen=True, slots=True)
class StoredMatchSetupOperation:
    """Minimal stored operation facts used to resolve an exact retry."""

    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    after_data: Mapping[str, object] | None


class MatchSetupRepository(Protocol):
    """Persistence operations required by complete scheduled setup replacement."""

    def lock_target(self, *, match_id: int) -> MatchSetupTarget | None: ...

    def lock_course(self, *, stadium_course_id: int) -> MatchCreationCourse | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredMatchSetupOperation | None: ...

    def replace_setup(
        self,
        *,
        target: MatchSetupTarget,
        command: UpdateMatchSetup,
        course: MatchCreationCourse,
        changed_at: datetime,
    ) -> MatchSetupTarget: ...

    def add_audit(
        self,
        *,
        command: UpdateMatchSetup,
        before: MatchSetupTarget,
        after: MatchSetupTarget,
        created_at: datetime,
    ) -> None: ...


class MatchSetupUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing one complete Match setup replacement repository."""

    @property
    def match_setup(self) -> MatchSetupRepository: ...


@dataclass(frozen=True, slots=True)
class MatchSetupCommands:
    """Application entry point for atomic scheduled Match setup edits."""

    command_runner: CommandRunner[MatchSetupUnitOfWork]
    clock: Callable[[], datetime]

    def update_setup(self, command: UpdateMatchSetup) -> UpdatedMatchSetup:
        return self.command_runner.run(lambda unit_of_work: self._update(unit_of_work.match_setup, command))

    def _update(self, repository: MatchSetupRepository, command: UpdateMatchSetup) -> UpdatedMatchSetup:
        try:
            target = repository.lock_target(match_id=command.match_id)
        except (TypeError, ValueError) as exc:
            raise MatchSetupAuditError("Stored Match setup facts are malformed.") from exc
        if target is None:
            raise MatchSetupUnavailableError("The selected Match does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if target.source_kind != MatchSourceKind.NATIVE_V2 or target.status != MatchStatus.SCHEDULED:
            raise MatchSetupUnavailableError("Setup edit requires a native scheduled Match.")
        if (
            target.state_fingerprint != command.expected_state_fingerprint
            or target.setup_version != command.expected_setup_version
        ):
            raise MatchSetupStaleError("Match setup changed after Preview.")

        course = repository.lock_course(stadium_course_id=command.stadium_course_id)
        if course is None:
            raise MatchSetupUnavailableError("The selected stadium course no longer exists.")
        if target.semantic_values == command.desired_semantic_values:
            raise MatchSetupNoChangeError("The requested Match setup is already current.")
        if command.reason is None:
            raise MatchSetupReasonRequiredError("Changing an existing Match setup requires a reason.")

        changed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        try:
            after = repository.replace_setup(
                target=target,
                command=command,
                course=course,
                changed_at=changed_at,
            )
        except MatchSetupError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchSetupAuditError("Match setup replacement produced malformed facts.") from exc
        if (
            after.match_id != target.match_id
            or after.name != command.name
            or after.description != command.description
            or after.source_kind != MatchSourceKind.NATIVE_V2
            or after.grade != command.grade
            or after.course != course
            or after.scheduled_at != command.scheduled_at
            or after.status != MatchStatus.SCHEDULED
            or after.condition is None
            or after.condition.values != command.condition
            or after.condition.updated_at != changed_at
            or after.updated_at != changed_at
        ):
            raise MatchSetupAuditError("Stored Match setup facts do not match the requested replacement.")
        committed = replace(after, setup_version=None)
        repository.add_audit(command=command, before=target, after=committed, created_at=changed_at)
        return UpdatedMatchSetup(snapshot=committed)

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredMatchSetupOperation,
        command: UpdateMatchSetup,
    ) -> UpdatedMatchSetup:
        if (
            stored.type != MatchSetupAuditType.UPDATED.value
            or stored.request_fingerprint != command.request_fingerprint
            or stored.match_id != command.match_id
        ):
            raise MatchSetupIdempotencyConflictError("Idempotency key is already bound to another logical operation.")
        if stored.after_data is None:
            raise MatchSetupAuditError("Exact-retry operation has no stored Match setup payload.")
        try:
            snapshot = MatchSetupTarget.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchSetupAuditError("Exact-retry Match setup payload is malformed.") from exc
        if (
            snapshot.match_id != command.match_id
            or snapshot.source_kind != MatchSourceKind.NATIVE_V2
            or snapshot.status != MatchStatus.SCHEDULED
            or snapshot.condition is None
            or snapshot.semantic_values != command.desired_semantic_values
        ):
            raise MatchSetupAuditError("Exact-retry Match setup payload does not match its operation context.")
        return UpdatedMatchSetup(snapshot=snapshot)
