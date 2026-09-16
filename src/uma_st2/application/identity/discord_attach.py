"""Staff-only direct Discord access attachment to an existing Persona."""

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

STAFF_DISCORD_ATTACH_AUDIT_SCHEMA_VERSION: Final = 1
STAFF_DISCORD_ATTACH_SOURCE: Final = "discord_staff"


class StaffDiscordAttachAuditType(StrEnum):
    ATTACHED = "discord_account_attached"


class StaffDiscordAttachError(ValueError):
    """Base error for a rejected direct Discord attach operation."""


class StaffDiscordAttachPersonaNotFoundError(StaffDiscordAttachError):
    """The selected Persona no longer exists."""


class StaffDiscordAttachTargetLinkedError(StaffDiscordAttachError):
    """The target Discord account is already linked to a Persona."""


class StaffDiscordAttachPendingRequestError(StaffDiscordAttachError):
    """A pending registration request must be resolved before direct attach."""


class StaffDiscordAttachStaleError(StaffDiscordAttachError):
    """The selected target changed after Preview."""


class StaffDiscordAttachIdempotencyConflictError(StaffDiscordAttachError):
    """The idempotency key is already bound to another logical operation."""


class StaffDiscordAttachConcurrentConflictError(StaffDiscordAttachError):
    """Another Identity mutation won and this transaction was rolled back."""


class StaffDiscordAttachAuditError(StaffDiscordAttachError):
    """Stored attach evidence is incomplete or internally inconsistent."""


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


def _non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _payload_text(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be non-empty text.")
    return value


def _payload_optional_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"{key} must be non-empty text or null.")
    return value


@dataclass(frozen=True, slots=True)
class StaffDiscordAttachState:
    """Complete detached authority used by Preview and final revalidation."""

    guild_id: str
    persona_id: str
    persona_display_name: str
    persona_status: PersonaStatus
    target_discord_user_id: str
    current_persona_id: str | None
    active_registration_request_id: int | None
    has_wallet: bool
    qualifying_game_account_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(
            self,
            "persona_display_name",
            _text(self.persona_display_name, field_name="persona_display_name", max_length=100),
        )
        object.__setattr__(self, "persona_status", PersonaStatus(self.persona_status))
        object.__setattr__(
            self,
            "target_discord_user_id",
            _discord_user_id(self.target_discord_user_id),
        )
        if self.current_persona_id is not None:
            object.__setattr__(self, "current_persona_id", _persona_id(self.current_persona_id))
        if self.active_registration_request_id is not None:
            object.__setattr__(
                self,
                "active_registration_request_id",
                _positive_int(
                    self.active_registration_request_id,
                    field_name="active_registration_request_id",
                ),
            )
        if not isinstance(self.has_wallet, bool):
            raise ValueError("has_wallet must be a boolean.")
        object.__setattr__(
            self,
            "qualifying_game_account_count",
            _non_negative_int(
                self.qualifying_game_account_count,
                field_name="qualifying_game_account_count",
            ),
        )

    @property
    def member_mutation_eligible(self) -> bool:
        return (
            allows_member_mutation(self.persona_status) and self.has_wallet and self.qualifying_game_account_count > 0
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_DISCORD_ATTACH_AUDIT_SCHEMA_VERSION,
            "source": STAFF_DISCORD_ATTACH_SOURCE,
            "guild_id": self.guild_id,
            "persona_id": self.persona_id,
            "persona_display_name": self.persona_display_name,
            "persona_status": self.persona_status.value,
            "target_discord_user_id": self.target_discord_user_id,
            "current_persona_id": self.current_persona_id,
            "active_registration_request_id": self.active_registration_request_id,
            "has_wallet": self.has_wallet,
            "qualifying_game_account_count": self.qualifying_game_account_count,
            "member_mutation_eligible": self.member_mutation_eligible,
        }

    @property
    def state_fingerprint(self) -> str:
        return _fingerprint(self.to_audit_payload())


@dataclass(frozen=True, slots=True)
class AttachDiscordAccountToPersona:
    """Attach one exact unlinked Discord target to one selected Persona."""

    guild_id: str
    target_persona_id: str
    target_discord_user_id: str
    attached_by_discord_user_id: str
    expected_target_fingerprint: str
    idempotency_key: str
    correlation_id: str | None = None
    operational_note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "target_persona_id", _persona_id(self.target_persona_id))
        object.__setattr__(
            self,
            "target_discord_user_id",
            _discord_user_id(self.target_discord_user_id),
        )
        object.__setattr__(
            self,
            "attached_by_discord_user_id",
            _discord_user_id(self.attached_by_discord_user_id),
        )
        for field_name, max_length, optional in (
            ("expected_target_fingerprint", 64, False),
            ("idempotency_key", 128, False),
            ("correlation_id", 128, True),
            ("operational_note", 255, True),
        ):
            object.__setattr__(
                self,
                field_name,
                _text(
                    getattr(self, field_name),
                    field_name=field_name,
                    max_length=max_length,
                    optional=optional,
                ),
            )

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "staff-discord-attach-command-v1",
                "guild_id": self.guild_id,
                "target_persona_id": self.target_persona_id,
                "target_discord_user_id": self.target_discord_user_id,
                "attached_by_discord_user_id": self.attached_by_discord_user_id,
                "expected_target_fingerprint": self.expected_target_fingerprint,
                "operational_note": self.operational_note,
            }
        )


@dataclass(frozen=True, slots=True)
class AttachedDiscordAccount:
    """Committed direct-attach receipt detached from persistence."""

    persona_id: str
    persona_display_name: str
    persona_status: PersonaStatus
    target_discord_user_id: str
    has_wallet: bool
    qualifying_game_account_count: int
    operational_note: str | None
    attached_at: datetime
    exact_retry: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(
            self,
            "persona_display_name",
            _text(self.persona_display_name, field_name="persona_display_name", max_length=100),
        )
        object.__setattr__(self, "persona_status", PersonaStatus(self.persona_status))
        object.__setattr__(
            self,
            "target_discord_user_id",
            _discord_user_id(self.target_discord_user_id),
        )
        if not isinstance(self.has_wallet, bool):
            raise ValueError("has_wallet must be a boolean.")
        object.__setattr__(
            self,
            "qualifying_game_account_count",
            _non_negative_int(
                self.qualifying_game_account_count,
                field_name="qualifying_game_account_count",
            ),
        )
        object.__setattr__(
            self,
            "operational_note",
            _text(
                self.operational_note,
                field_name="operational_note",
                max_length=255,
                optional=True,
            ),
        )
        object.__setattr__(
            self,
            "attached_at",
            normalize_utc_datetime(self.attached_at, field_name="attached_at"),
        )
        if not isinstance(self.exact_retry, bool):
            raise ValueError("exact_retry must be a boolean.")

    @property
    def member_mutation_eligible(self) -> bool:
        return (
            allows_member_mutation(self.persona_status) and self.has_wallet and self.qualifying_game_account_count > 0
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_DISCORD_ATTACH_AUDIT_SCHEMA_VERSION,
            "source": STAFF_DISCORD_ATTACH_SOURCE,
            "persona_id": self.persona_id,
            "persona_display_name": self.persona_display_name,
            "persona_status": self.persona_status.value,
            "target_discord_user_id": self.target_discord_user_id,
            "has_wallet": self.has_wallet,
            "qualifying_game_account_count": self.qualifying_game_account_count,
            "member_mutation_eligible": self.member_mutation_eligible,
            "operational_note": self.operational_note,
            "attached_at": self.attached_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> AttachedDiscordAccount:
        if payload.get("schema_version") != STAFF_DISCORD_ATTACH_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported direct-attach audit schema version.")
        if payload.get("source") != STAFF_DISCORD_ATTACH_SOURCE:
            raise ValueError("Direct-attach audit source is invalid.")
        result = cls(
            persona_id=_payload_text(payload, "persona_id"),
            persona_display_name=_payload_text(payload, "persona_display_name"),
            persona_status=PersonaStatus(_payload_text(payload, "persona_status")),
            target_discord_user_id=_payload_text(payload, "target_discord_user_id"),
            has_wallet=payload["has_wallet"],  # type: ignore[arg-type]
            qualifying_game_account_count=_non_negative_int(
                payload["qualifying_game_account_count"],
                field_name="qualifying_game_account_count",
            ),
            operational_note=_payload_optional_text(payload, "operational_note"),
            attached_at=datetime.fromisoformat(_payload_text(payload, "attached_at")),
        )
        if payload.get("member_mutation_eligible") is not result.member_mutation_eligible:
            raise ValueError("Direct-attach audit eligibility is inconsistent.")
        return result


@dataclass(frozen=True, slots=True)
class StoredStaffDiscordAttachOperation:
    request_fingerprint: str | None
    type: str | None
    persona_id: str | None
    discord_user_id: str | None
    after_data: Mapping[str, object] | None


class StaffDiscordAttachQueryRepository(Protocol):
    def get_target_state(
        self,
        *,
        guild_id: str,
        persona_id: str,
        discord_user_id: str,
    ) -> StaffDiscordAttachState | None: ...


class StaffDiscordAttachQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_discord_attach(self) -> StaffDiscordAttachQueryRepository: ...


@dataclass(frozen=True, slots=True)
class StaffDiscordAttachQueries:
    query_runner: QueryRunner[StaffDiscordAttachQueryUnitOfWork]

    def get_preview(
        self,
        *,
        guild_id: str,
        persona_id: str,
        discord_user_id: str,
    ) -> StaffDiscordAttachState:
        normalized_guild_id = _text(guild_id, field_name="guild_id", max_length=32)
        assert normalized_guild_id is not None
        normalized_persona_id = _persona_id(persona_id)
        normalized_discord_user_id = _discord_user_id(discord_user_id)
        state = self.query_runner.run(
            lambda uow: uow.staff_discord_attach.get_target_state(
                guild_id=normalized_guild_id,
                persona_id=normalized_persona_id,
                discord_user_id=normalized_discord_user_id,
            )
        )
        return _validate_attachable_state(state)


class StaffDiscordAttachRepository(Protocol):
    def lock_target_state(
        self,
        *,
        guild_id: str,
        persona_id: str,
        discord_user_id: str,
        created_at: datetime,
    ) -> StaffDiscordAttachState | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredStaffDiscordAttachOperation | None: ...

    def attach(
        self,
        *,
        command: AttachDiscordAccountToPersona,
        state: StaffDiscordAttachState,
        attached_at: datetime,
    ) -> AttachedDiscordAccount: ...


class StaffDiscordAttachUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_discord_attach(self) -> StaffDiscordAttachRepository: ...


@dataclass(frozen=True, slots=True)
class StaffDiscordAttachCommands:
    command_runner: CommandRunner[StaffDiscordAttachUnitOfWork]
    clock: Callable[[], datetime]

    def attach(self, command: AttachDiscordAccountToPersona) -> AttachedDiscordAccount:
        return self.command_runner.run(lambda uow: self._attach(uow.staff_discord_attach, command))

    def _attach(
        self,
        repository: StaffDiscordAttachRepository,
        command: AttachDiscordAccountToPersona,
    ) -> AttachedDiscordAccount:
        attached_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        state = repository.lock_target_state(
            guild_id=command.guild_id,
            persona_id=command.target_persona_id,
            discord_user_id=command.target_discord_user_id,
            created_at=attached_at,
        )
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_retry(stored=stored, command=command)
        target = _validate_attachable_state(state)
        if target.state_fingerprint != command.expected_target_fingerprint:
            raise StaffDiscordAttachStaleError("Direct-attach authority changed after Preview.")
        result = repository.attach(command=command, state=target, attached_at=attached_at)
        if (
            result.persona_id != target.persona_id
            or result.persona_display_name != target.persona_display_name
            or result.persona_status is not target.persona_status
            or result.target_discord_user_id != target.target_discord_user_id
            or result.has_wallet is not target.has_wallet
            or result.qualifying_game_account_count != target.qualifying_game_account_count
            or result.operational_note != command.operational_note
            or result.attached_at != attached_at
            or result.exact_retry
        ):
            raise StaffDiscordAttachAuditError("Direct-attach receipt is inconsistent.")
        return result

    @staticmethod
    def _resolve_retry(
        *,
        stored: StoredStaffDiscordAttachOperation,
        command: AttachDiscordAccountToPersona,
    ) -> AttachedDiscordAccount:
        if (
            stored.type != StaffDiscordAttachAuditType.ATTACHED.value
            or stored.request_fingerprint != command.request_fingerprint
        ):
            raise StaffDiscordAttachIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise StaffDiscordAttachAuditError("Direct-attach exact-retry operation has no receipt payload.")
        try:
            result = AttachedDiscordAccount.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as error:
            raise StaffDiscordAttachAuditError("Direct-attach exact-retry receipt is malformed.") from error
        if (
            stored.persona_id != result.persona_id
            or stored.discord_user_id != result.target_discord_user_id
            or result.persona_id != command.target_persona_id
            or result.target_discord_user_id != command.target_discord_user_id
            or result.operational_note != command.operational_note
        ):
            raise StaffDiscordAttachAuditError("Direct-attach exact-retry context is malformed.")
        return replace(result, exact_retry=True)


def _validate_attachable_state(
    state: StaffDiscordAttachState | None,
) -> StaffDiscordAttachState:
    if state is None:
        raise StaffDiscordAttachPersonaNotFoundError("Selected Persona does not exist.")
    if state.current_persona_id is not None:
        raise StaffDiscordAttachTargetLinkedError("Discord target is already linked to a Persona.")
    if state.active_registration_request_id is not None:
        raise StaffDiscordAttachPendingRequestError(
            "Pending registration request must be resolved before direct attach."
        )
    return state
