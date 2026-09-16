"""Native V2 Circle Match creation application boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    StadiumCourseLayout,
)
from uma_st2.shared import normalize_utc_datetime

from .conditions import MatchConditionRecord, MatchConditionValues

MATCH_CREATION_AUDIT_SCHEMA_VERSION: Final = 2


class MatchCreationAuditType(StrEnum):
    """Canonical operation type for native Match creation."""

    CREATED = "match_created"


class MatchCreationError(ValueError):
    """Base error for rejected Match creation commands."""


class MatchCreationUnavailableError(MatchCreationError):
    """The selected current master-data course cannot be used."""


class MatchCreationIdempotencyConflictError(MatchCreationError):
    """An idempotency key is bound to another logical operation."""


class MatchCreationAuditError(MatchCreationError):
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
class MatchCreationCourse:
    """Locked course and stadium facts returned by persistence."""

    id: int
    stadium_id: int
    stadium_name: str
    surface: MatchSurface
    distance: int
    direction: MatchDirection
    layout: StadiumCourseLayout

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.stadium_id, field_name="stadium_id")
        _require_positive_int(self.distance, field_name="distance")
        object.__setattr__(
            self,
            "stadium_name",
            _normalized_string(self.stadium_name, field_name="stadium_name", max_length=100),
        )
        object.__setattr__(self, "surface", MatchSurface(self.surface))
        object.__setattr__(self, "direction", MatchDirection(self.direction))
        object.__setattr__(self, "layout", StadiumCourseLayout(self.layout))

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "stadium_id": self.stadium_id,
            "stadium_name": self.stadium_name,
            "surface": self.surface.value,
            "distance": self.distance,
            "direction": self.direction.value,
            "layout": self.layout.value,
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> MatchCreationCourse:
        return cls(
            id=_payload_positive_int(payload, "id"),
            stadium_id=_payload_positive_int(payload, "stadium_id"),
            stadium_name=_payload_string(payload, "stadium_name"),
            surface=MatchSurface(_payload_string(payload, "surface")),
            distance=_payload_positive_int(payload, "distance"),
            direction=MatchDirection(_payload_string(payload, "direction")),
            layout=StadiumCourseLayout(_payload_string(payload, "layout")),
        )


@dataclass(frozen=True, slots=True)
class CreateMatch:
    """Create one native scheduled Match against an exact master course."""

    name: str
    description: str | None
    grade: MatchGrade
    stadium_course_id: int
    scheduled_at: datetime
    condition: MatchConditionValues
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str | None = None
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _normalized_string(self.name, field_name="name", max_length=200))
        object.__setattr__(
            self,
            "description",
            _normalized_string(
                self.description,
                field_name="description",
                max_length=4000,
                optional=True,
            ),
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
            "schema": "match-creation-command-v2",
            "name": self.name,
            "description": self.description,
            "grade": self.grade.value,
            "stadium_course_id": self.stadium_course_id,
            "scheduled_at": self.scheduled_at.isoformat(),
            "condition": self.condition.to_audit_payload(),
        }
        encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MatchCreationSnapshot:
    """Deterministic committed Match facts retained by its operation audit."""

    match_id: int
    name: str
    description: str | None
    source_kind: MatchSourceKind
    grade: MatchGrade
    course: MatchCreationCourse
    scheduled_at: datetime
    condition: MatchConditionRecord
    status: MatchStatus
    created_at: datetime

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(self, "name", _normalized_string(self.name, field_name="name", max_length=200))
        object.__setattr__(
            self,
            "description",
            _normalized_string(
                self.description,
                field_name="description",
                max_length=4000,
                optional=True,
            ),
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
        if not isinstance(self.condition, MatchConditionRecord):
            raise ValueError("condition must be a MatchConditionRecord.")
        object.__setattr__(self, "status", MatchStatus(self.status))
        object.__setattr__(
            self,
            "created_at",
            normalize_utc_datetime(self.created_at, field_name="created_at"),
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_CREATION_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "name": self.name,
            "description": self.description,
            "source_kind": self.source_kind.value,
            "grade": self.grade.value,
            "course": self.course.to_audit_payload(),
            "scheduled_at": self.scheduled_at.isoformat(),
            "condition": self.condition.to_audit_payload(),
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> MatchCreationSnapshot:
        if payload.get("schema_version") != MATCH_CREATION_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Match creation audit schema version.")
        description = payload.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError("description must be a string or null.")
        course_payload = payload["course"]
        if not isinstance(course_payload, Mapping):
            raise ValueError("course must be an object.")
        condition_payload = payload["condition"]
        if not isinstance(condition_payload, Mapping):
            raise ValueError("condition must be an object.")
        return cls(
            match_id=_payload_positive_int(payload, "match_id"),
            name=_payload_string(payload, "name"),
            description=description,
            source_kind=MatchSourceKind(_payload_string(payload, "source_kind")),
            grade=MatchGrade(_payload_string(payload, "grade")),
            course=MatchCreationCourse.from_audit_payload(course_payload),
            scheduled_at=datetime.fromisoformat(_payload_string(payload, "scheduled_at")),
            condition=MatchConditionRecord.from_audit_payload(condition_payload),
            status=MatchStatus(_payload_string(payload, "status")),
            created_at=datetime.fromisoformat(_payload_string(payload, "created_at")),
        )


@dataclass(frozen=True, slots=True)
class CreatedMatch:
    """Closed-session result of one native Match creation command."""

    snapshot: MatchCreationSnapshot
    operation_type: MatchCreationAuditType = MatchCreationAuditType.CREATED

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, MatchCreationSnapshot):
            raise ValueError("snapshot must be a MatchCreationSnapshot.")
        object.__setattr__(self, "operation_type", MatchCreationAuditType(self.operation_type))


@dataclass(frozen=True, slots=True)
class StoredMatchCreationOperation:
    """Minimal persisted operation facts used to resolve an exact retry."""

    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    after_data: Mapping[str, object] | None


class MatchCreationRepository(Protocol):
    """Persistence operations required by native Match creation."""

    def lock_course(self, *, stadium_course_id: int) -> MatchCreationCourse | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredMatchCreationOperation | None: ...

    def create_match(
        self,
        *,
        command: CreateMatch,
        course: MatchCreationCourse,
        created_at: datetime,
    ) -> MatchCreationSnapshot: ...

    def add_creation_audit(
        self,
        *,
        command: CreateMatch,
        snapshot: MatchCreationSnapshot,
        created_at: datetime,
    ) -> None: ...


class MatchCreationUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only native Match creation persistence."""

    @property
    def match_creation(self) -> MatchCreationRepository: ...


@dataclass(frozen=True, slots=True)
class MatchCreationCommands:
    """Application entry point for atomic native Match creation."""

    command_runner: CommandRunner[MatchCreationUnitOfWork]
    clock: Callable[[], datetime]

    def create_match(self, command: CreateMatch) -> CreatedMatch:
        return self.command_runner.run(lambda unit_of_work: self._create_match(unit_of_work.match_creation, command))

    def _create_match(
        self,
        repository: MatchCreationRepository,
        command: CreateMatch,
    ) -> CreatedMatch:
        course = repository.lock_course(stadium_course_id=command.stadium_course_id)
        if course is None:
            raise MatchCreationUnavailableError("The selected stadium course does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        created_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        snapshot = repository.create_match(command=command, course=course, created_at=created_at)
        if (
            snapshot.name != command.name
            or snapshot.description != command.description
            or snapshot.source_kind != MatchSourceKind.NATIVE_V2
            or snapshot.grade != command.grade
            or snapshot.course != course
            or snapshot.scheduled_at != command.scheduled_at
            or snapshot.condition.values != command.condition
            or snapshot.status != MatchStatus.SCHEDULED
            or snapshot.created_at != created_at
        ):
            raise MatchCreationAuditError("Created Match facts do not match the requested native Match.")
        repository.add_creation_audit(command=command, snapshot=snapshot, created_at=created_at)
        return CreatedMatch(snapshot=snapshot)

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredMatchCreationOperation,
        command: CreateMatch,
    ) -> CreatedMatch:
        try:
            operation_type = MatchCreationAuditType(stored.type)
        except (TypeError, ValueError) as exc:
            raise MatchCreationIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from exc
        if (
            operation_type != MatchCreationAuditType.CREATED
            or stored.request_fingerprint != command.request_fingerprint
        ):
            raise MatchCreationIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise MatchCreationAuditError("Exact-retry operation has no stored Match creation payload.")
        try:
            snapshot = MatchCreationSnapshot.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchCreationAuditError("Exact-retry Match creation payload is malformed.") from exc
        if (
            snapshot.match_id != stored.match_id
            or snapshot.name != command.name
            or snapshot.description != command.description
            or snapshot.source_kind != MatchSourceKind.NATIVE_V2
            or snapshot.grade != command.grade
            or snapshot.course.id != command.stadium_course_id
            or snapshot.scheduled_at != command.scheduled_at
            or snapshot.condition.values != command.condition
            or snapshot.status != MatchStatus.SCHEDULED
        ):
            raise MatchCreationAuditError("Exact-retry Match creation payload does not match its operation context.")
        return CreatedMatch(snapshot=snapshot, operation_type=operation_type)
