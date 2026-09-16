"""Staff-only Persona status management."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.domain.identity import PersonaStatus, allows_member_mutation
from uma_st2.shared import normalize_utc_datetime

STAFF_PERSONA_STATUS_AUDIT_SCHEMA_VERSION: Final = 1
STAFF_PERSONA_STATUS_SOURCE: Final = "discord_staff"


class StaffPersonaStatusAuditType(StrEnum):
    PERSONA_STATUS_CHANGED = "persona_status_changed"


class StaffPersonaStatusError(ValueError):
    """Base error for rejected Persona status mutations."""


class StaffPersonaStatusNotFoundError(StaffPersonaStatusError):
    """The selected Persona no longer exists."""


class StaffPersonaStatusNoChangeError(StaffPersonaStatusError):
    """The desired status equals current canonical state."""


class StaffPersonaStatusStaleError(StaffPersonaStatusError):
    """The selected Persona changed after Preview."""


class StaffPersonaStatusIdempotencyConflictError(StaffPersonaStatusError):
    """The Final interaction key belongs to another mutation."""


class StaffPersonaStatusConcurrentConflictError(StaffPersonaStatusError):
    """Another Identity mutation won."""


class StaffPersonaStatusAuditError(StaffPersonaStatusError):
    """Stored status-change evidence is malformed."""


def _text(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text.")
    normalized = value.strip()
    if optional and not normalized:
        return None
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must contain 1 to {max_length} characters.")
    return normalized


def _persona_id(value: str) -> str:
    normalized = _text(value, field_name="persona_id", max_length=36)
    assert normalized is not None
    return normalized


def _discord_user_id(value: str) -> str:
    normalized = _text(value, field_name="discord_user_id", max_length=32)
    assert normalized is not None
    if not normalized.isascii() or not normalized.isdecimal() or normalized.startswith("0") or int(normalized) <= 0:
        raise ValueError("discord_user_id must be a positive ASCII Discord snowflake.")
    return normalized


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _payload_text(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be non-empty text.")
    return value


@dataclass(frozen=True, slots=True)
class StaffPersonaStatusState:
    guild_id: str
    persona_id: str
    display_name: str
    status: PersonaStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(self, "display_name", _text(self.display_name, field_name="display_name", max_length=100))
        object.__setattr__(self, "status", PersonaStatus(self.status))

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_PERSONA_STATUS_AUDIT_SCHEMA_VERSION,
            "source": STAFF_PERSONA_STATUS_SOURCE,
            "guild_id": self.guild_id,
            "persona_id": self.persona_id,
            "display_name": self.display_name,
            "status": self.status.value,
        }

    @property
    def state_fingerprint(self) -> str:
        return _fingerprint(self.to_audit_payload())


@dataclass(frozen=True, slots=True)
class StaffPersonaStatusPreview:
    state: StaffPersonaStatusState
    desired_status: PersonaStatus
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.state, StaffPersonaStatusState):
            raise ValueError("state must be a StaffPersonaStatusState.")
        object.__setattr__(self, "desired_status", PersonaStatus(self.desired_status))
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))

    @property
    def resulting_status_gate_open(self) -> bool:
        return allows_member_mutation(self.desired_status)


@dataclass(frozen=True, slots=True)
class ChangePersonaStatus:
    guild_id: str
    target_persona_id: str
    desired_status: PersonaStatus
    reason: str
    updated_by_discord_user_id: str
    expected_target_fingerprint: str
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "target_persona_id", _persona_id(self.target_persona_id))
        object.__setattr__(self, "desired_status", PersonaStatus(self.desired_status))
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        object.__setattr__(
            self,
            "updated_by_discord_user_id",
            _discord_user_id(self.updated_by_discord_user_id),
        )
        for field_name, max_length, optional in (
            ("expected_target_fingerprint", 64, False),
            ("idempotency_key", 128, False),
            ("correlation_id", 128, True),
        ):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name=field_name, max_length=max_length, optional=optional),
            )

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "staff-persona-status-change-command-v1",
                "guild_id": self.guild_id,
                "target_persona_id": self.target_persona_id,
                "desired_status": self.desired_status.value,
                "reason": self.reason,
                "updated_by_discord_user_id": self.updated_by_discord_user_id,
                "expected_target_fingerprint": self.expected_target_fingerprint,
            }
        )


@dataclass(frozen=True, slots=True)
class ChangedPersonaStatus:
    persona_id: str
    display_name: str
    previous_status: PersonaStatus
    status: PersonaStatus
    reason: str
    updated_at: datetime
    exact_retry: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(self, "display_name", _text(self.display_name, field_name="display_name", max_length=100))
        object.__setattr__(self, "previous_status", PersonaStatus(self.previous_status))
        object.__setattr__(self, "status", PersonaStatus(self.status))
        if self.previous_status is self.status:
            raise ValueError("A status-change receipt must contain an actual change.")
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        object.__setattr__(self, "updated_at", normalize_utc_datetime(self.updated_at, field_name="updated_at"))
        if not isinstance(self.exact_retry, bool):
            raise ValueError("exact_retry must be a boolean.")

    @property
    def resulting_status_gate_open(self) -> bool:
        return allows_member_mutation(self.status)

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_PERSONA_STATUS_AUDIT_SCHEMA_VERSION,
            "source": STAFF_PERSONA_STATUS_SOURCE,
            "persona_id": self.persona_id,
            "display_name": self.display_name,
            "previous_status": self.previous_status.value,
            "status": self.status.value,
            "reason": self.reason,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> ChangedPersonaStatus:
        if payload.get("schema_version") != STAFF_PERSONA_STATUS_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Persona status audit schema version.")
        if payload.get("source") != STAFF_PERSONA_STATUS_SOURCE:
            raise ValueError("Persona status audit source is invalid.")
        return cls(
            persona_id=_payload_text(payload, "persona_id"),
            display_name=_payload_text(payload, "display_name"),
            previous_status=PersonaStatus(_payload_text(payload, "previous_status")),
            status=PersonaStatus(_payload_text(payload, "status")),
            reason=_payload_text(payload, "reason"),
            updated_at=datetime.fromisoformat(_payload_text(payload, "updated_at")),
        )


@dataclass(frozen=True, slots=True)
class StoredStaffPersonaStatusOperation:
    request_fingerprint: str | None
    type: str | None
    persona_id: str | None
    game_account_id: int | None
    after_data: Mapping[str, object] | None


class StaffPersonaStatusQueryRepository(Protocol):
    def get_state(self, *, guild_id: str, persona_id: str) -> StaffPersonaStatusState | None: ...


class StaffPersonaStatusQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_persona_status(self) -> StaffPersonaStatusQueryRepository: ...


@dataclass(frozen=True, slots=True)
class StaffPersonaStatusQueries:
    query_runner: QueryRunner[StaffPersonaStatusQueryUnitOfWork]

    def get_state(self, *, guild_id: str, persona_id: str) -> StaffPersonaStatusState:
        state = self.query_runner.run(
            lambda uow: uow.staff_persona_status.get_state(
                guild_id=_required_guild_id(guild_id),
                persona_id=_persona_id(persona_id),
            )
        )
        if state is None:
            raise StaffPersonaStatusNotFoundError("Selected Persona does not exist.")
        return state

    def get_preview(
        self,
        *,
        guild_id: str,
        persona_id: str,
        desired_status: PersonaStatus | str,
        reason: str,
    ) -> StaffPersonaStatusPreview:
        state = self.get_state(guild_id=guild_id, persona_id=persona_id)
        preview = StaffPersonaStatusPreview(
            state=state,
            desired_status=PersonaStatus(desired_status),
            reason=reason,
        )
        if preview.desired_status is state.status:
            raise StaffPersonaStatusNoChangeError("Persona status is unchanged.")
        return preview


class StaffPersonaStatusRepository(Protocol):
    def lock_state(self, *, guild_id: str, persona_id: str) -> StaffPersonaStatusState | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredStaffPersonaStatusOperation | None: ...

    def change_status(
        self,
        *,
        command: ChangePersonaStatus,
        state: StaffPersonaStatusState,
        updated_at: datetime,
    ) -> ChangedPersonaStatus: ...


class StaffPersonaStatusUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_persona_status(self) -> StaffPersonaStatusRepository: ...


@dataclass(frozen=True, slots=True)
class StaffPersonaStatusCommands:
    command_runner: CommandRunner[StaffPersonaStatusUnitOfWork]
    clock: Callable[[], datetime]

    def change(self, command: ChangePersonaStatus) -> ChangedPersonaStatus:
        return self.command_runner.run(lambda uow: self._change(uow.staff_persona_status, command))

    def _change(
        self,
        repository: StaffPersonaStatusRepository,
        command: ChangePersonaStatus,
    ) -> ChangedPersonaStatus:
        updated_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        state = repository.lock_state(
            guild_id=command.guild_id,
            persona_id=command.target_persona_id,
        )
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_retry(stored=stored, command=command)
        if state is None:
            raise StaffPersonaStatusNotFoundError("Selected Persona does not exist.")
        if state.state_fingerprint != command.expected_target_fingerprint:
            raise StaffPersonaStatusStaleError("Persona status authority changed after Preview.")
        if state.status is command.desired_status:
            raise StaffPersonaStatusNoChangeError("Persona status is unchanged.")
        result = repository.change_status(command=command, state=state, updated_at=updated_at)
        if (
            result.persona_id != state.persona_id
            or result.display_name != state.display_name
            or result.previous_status is not state.status
            or result.status is not command.desired_status
            or result.reason != command.reason
            or result.updated_at != updated_at
            or result.exact_retry
        ):
            raise StaffPersonaStatusAuditError("Persona status-change receipt is inconsistent.")
        return result

    @staticmethod
    def _resolve_retry(
        *,
        stored: StoredStaffPersonaStatusOperation,
        command: ChangePersonaStatus,
    ) -> ChangedPersonaStatus:
        if (
            stored.type != StaffPersonaStatusAuditType.PERSONA_STATUS_CHANGED.value
            or stored.request_fingerprint != command.request_fingerprint
        ):
            raise StaffPersonaStatusIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise StaffPersonaStatusAuditError("Persona status-change retry has no receipt.")
        try:
            result = ChangedPersonaStatus.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as error:
            raise StaffPersonaStatusAuditError("Persona status-change retry receipt is malformed.") from error
        if (
            stored.persona_id != result.persona_id
            or stored.game_account_id is not None
            or result.persona_id != command.target_persona_id
            or result.status is not command.desired_status
            or result.reason != command.reason
        ):
            raise StaffPersonaStatusAuditError("Persona status-change retry context is malformed.")
        return replace(result, exact_retry=True)


def _required_guild_id(value: str) -> str:
    normalized = _text(value, field_name="guild_id", max_length=32)
    assert normalized is not None
    return normalized
