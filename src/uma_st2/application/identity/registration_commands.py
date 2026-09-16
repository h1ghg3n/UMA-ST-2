"""Request-only native Account registration application boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.identity import (
    GameRegion,
    RegistrationRequestStatus,
    normalize_registration_pid,
)
from uma_st2.shared import normalize_utc_datetime

from .registration_bootstrap import AccountRegistrationAuditType

ACCOUNT_REGISTRATION_REQUEST_AUDIT_SCHEMA_VERSION: Final = 1


class AccountRegistrationError(ValueError):
    """Base error for rejected registration request commands."""


class AccountRegistrationAlreadyLinkedError(AccountRegistrationError):
    """The Discord actor already belongs to a Persona."""


class AccountRegistrationAlreadyPendingError(AccountRegistrationError):
    """The Discord actor already owns one active request in this guild."""

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(f"Registration request {request_id} is already pending.")


class AccountRegistrationPidUnavailableError(AccountRegistrationError):
    """The requested canonical region/PID is already registered."""


class AccountRegistrationIdempotencyConflictError(AccountRegistrationError):
    """An idempotency key is bound to another logical operation."""


class AccountRegistrationAuditError(AccountRegistrationError):
    """Stored request or exact-retry evidence is malformed."""


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


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


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
class SubmitAccountRegistrationRequest:
    """Submit one request without creating approved member identity or economy."""

    guild_id: str
    actor_discord_user_id: str
    discord_display_name_snapshot: str
    game_region: GameRegion
    uma_pid: str
    nickname: str
    affiliation: str | None
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        for field_name, max_length, optional in (
            ("guild_id", 32, False),
            ("actor_discord_user_id", 32, False),
            ("discord_display_name_snapshot", 100, False),
            ("nickname", 100, False),
            ("affiliation", 100, True),
            ("idempotency_key", 128, False),
            ("correlation_id", 128, True),
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
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))

    @property
    def request_fingerprint(self) -> str:
        canonical = {
            "schema": "account-registration-request-command-v1",
            "guild_id": self.guild_id,
            "actor_discord_user_id": self.actor_discord_user_id,
            "discord_display_name_snapshot": self.discord_display_name_snapshot,
            "game_region": self.game_region.value,
            "uma_pid": self.uma_pid,
            "nickname": self.nickname,
            "affiliation": self.affiliation,
        }
        encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AccountRegistrationRequester:
    """Locked Discord actor used as the request-submission serialization root."""

    discord_account_id: int
    discord_user_id: str
    persona_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "discord_account_id",
            _positive_int(self.discord_account_id, field_name="discord_account_id"),
        )
        object.__setattr__(
            self,
            "discord_user_id",
            _text(self.discord_user_id, field_name="discord_user_id", max_length=32),
        )
        object.__setattr__(
            self,
            "persona_id",
            _text(self.persona_id, field_name="persona_id", max_length=36, optional=True),
        )


@dataclass(frozen=True, slots=True)
class ActiveAccountRegistrationRequest:
    """Minimal current active-request authority."""

    request_id: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _positive_int(self.request_id, field_name="request_id"))


@dataclass(frozen=True, slots=True)
class AccountRegistrationRequestSnapshot:
    """Detached committed request facts retained by the Identity operation."""

    request_id: int
    discord_account_id: int
    guild_id: str
    requester_discord_user_id: str
    discord_display_name_snapshot: str
    game_region: GameRegion
    uma_pid: str
    nickname: str
    affiliation: str | None
    status: RegistrationRequestStatus
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _positive_int(self.request_id, field_name="request_id"))
        object.__setattr__(
            self,
            "discord_account_id",
            _positive_int(self.discord_account_id, field_name="discord_account_id"),
        )
        for field_name, max_length, optional in (
            ("guild_id", 32, False),
            ("requester_discord_user_id", 32, False),
            ("discord_display_name_snapshot", 100, False),
            ("nickname", 100, False),
            ("affiliation", 100, True),
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
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))
        object.__setattr__(self, "status", RegistrationRequestStatus(self.status))
        if self.status is not RegistrationRequestStatus.PENDING:
            raise ValueError("Submitted registration request snapshot must be pending.")
        object.__setattr__(
            self,
            "created_at",
            normalize_utc_datetime(self.created_at, field_name="created_at"),
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": ACCOUNT_REGISTRATION_REQUEST_AUDIT_SCHEMA_VERSION,
            "request_id": self.request_id,
            "discord_account_id": self.discord_account_id,
            "guild_id": self.guild_id,
            "requester_discord_user_id": self.requester_discord_user_id,
            "discord_display_name_snapshot": self.discord_display_name_snapshot,
            "game_region": self.game_region.value,
            "uma_pid": self.uma_pid,
            "nickname": self.nickname,
            "affiliation": self.affiliation,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> AccountRegistrationRequestSnapshot:
        if payload.get("schema_version") != ACCOUNT_REGISTRATION_REQUEST_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported registration request audit schema version.")
        return cls(
            request_id=_positive_int(payload["request_id"], field_name="request_id"),
            discord_account_id=_positive_int(
                payload["discord_account_id"],
                field_name="discord_account_id",
            ),
            guild_id=_payload_text(payload, "guild_id"),
            requester_discord_user_id=_payload_text(payload, "requester_discord_user_id"),
            discord_display_name_snapshot=_payload_text(payload, "discord_display_name_snapshot"),
            game_region=GameRegion(_payload_text(payload, "game_region")),
            uma_pid=_payload_text(payload, "uma_pid"),
            nickname=_payload_text(payload, "nickname"),
            affiliation=_payload_optional_text(payload, "affiliation"),
            status=RegistrationRequestStatus(_payload_text(payload, "status")),
            created_at=datetime.fromisoformat(_payload_text(payload, "created_at")),
        )


@dataclass(frozen=True, slots=True)
class SubmittedAccountRegistrationRequest:
    """Closed-session result returned to a private Discord receipt."""

    snapshot: AccountRegistrationRequestSnapshot
    exact_retry: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, AccountRegistrationRequestSnapshot):
            raise ValueError("snapshot must be an AccountRegistrationRequestSnapshot.")
        if not isinstance(self.exact_retry, bool):
            raise ValueError("exact_retry must be a boolean.")


@dataclass(frozen=True, slots=True)
class StoredAccountRegistrationOperation:
    """Minimal persisted operation facts used for exact retry."""

    request_fingerprint: str | None
    type: str | None
    persona_id: str | None
    game_account_id: int | None
    discord_user_id: str | None
    after_data: Mapping[str, object] | None


class AccountRegistrationRepository(Protocol):
    """Persistence capabilities for request-only registration submission."""

    def find_operation(self, *, idempotency_key: str) -> StoredAccountRegistrationOperation | None: ...

    def lock_or_create_requester(
        self,
        *,
        discord_user_id: str,
        created_at: datetime,
    ) -> AccountRegistrationRequester: ...

    def find_active_request(
        self,
        *,
        guild_id: str,
        requester_discord_user_id: str,
    ) -> ActiveAccountRegistrationRequest | None: ...

    def registered_game_account_exists(self, *, game_region: GameRegion, uma_pid: str) -> bool: ...

    def create_request(
        self,
        *,
        command: SubmitAccountRegistrationRequest,
        requester: AccountRegistrationRequester,
        created_at: datetime,
    ) -> AccountRegistrationRequestSnapshot: ...

    def add_request_audit(
        self,
        *,
        command: SubmitAccountRegistrationRequest,
        snapshot: AccountRegistrationRequestSnapshot,
        created_at: datetime,
    ) -> None: ...


class AccountRegistrationUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing request-only registration persistence."""

    @property
    def account_registration(self) -> AccountRegistrationRepository: ...


@dataclass(frozen=True, slots=True)
class AccountRegistrationCommands:
    """Application entry point for one pending member request."""

    command_runner: CommandRunner[AccountRegistrationUnitOfWork]
    clock: Callable[[], datetime]

    def submit_registration_request(
        self,
        command: SubmitAccountRegistrationRequest,
    ) -> SubmittedAccountRegistrationRequest:
        return self.command_runner.run(lambda unit_of_work: self._submit(unit_of_work.account_registration, command))

    def _submit(
        self,
        repository: AccountRegistrationRepository,
        command: SubmitAccountRegistrationRequest,
    ) -> SubmittedAccountRegistrationRequest:
        created_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        requester = repository.lock_or_create_requester(
            discord_user_id=command.actor_discord_user_id,
            created_at=created_at,
        )

        # The requester row is the first lock root. Looking up an absent
        # operation with FOR UPDATE before this point can take an InnoDB gap
        # lock and deadlock against another key for the same new requester.
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if requester.discord_user_id != command.actor_discord_user_id:
            raise AccountRegistrationAuditError("Locked Discord requester does not match the command actor.")
        if requester.persona_id is not None:
            raise AccountRegistrationAlreadyLinkedError("Discord actor is already linked to a Persona.")

        active = repository.find_active_request(
            guild_id=command.guild_id,
            requester_discord_user_id=command.actor_discord_user_id,
        )
        if active is not None:
            raise AccountRegistrationAlreadyPendingError(active.request_id)
        if repository.registered_game_account_exists(
            game_region=command.game_region,
            uma_pid=command.uma_pid,
        ):
            raise AccountRegistrationPidUnavailableError("The requested region/PID is already registered.")

        snapshot = repository.create_request(
            command=command,
            requester=requester,
            created_at=created_at,
        )
        self._verify_snapshot(snapshot=snapshot, command=command, requester=requester, created_at=created_at)
        repository.add_request_audit(command=command, snapshot=snapshot, created_at=created_at)
        return SubmittedAccountRegistrationRequest(snapshot=snapshot)

    @staticmethod
    def _verify_snapshot(
        *,
        snapshot: AccountRegistrationRequestSnapshot,
        command: SubmitAccountRegistrationRequest,
        requester: AccountRegistrationRequester,
        created_at: datetime,
    ) -> None:
        if (
            snapshot.discord_account_id != requester.discord_account_id
            or snapshot.guild_id != command.guild_id
            or snapshot.requester_discord_user_id != command.actor_discord_user_id
            or snapshot.discord_display_name_snapshot != command.discord_display_name_snapshot
            or snapshot.game_region is not command.game_region
            or snapshot.uma_pid != command.uma_pid
            or snapshot.nickname != command.nickname
            or snapshot.affiliation != command.affiliation
            or snapshot.status is not RegistrationRequestStatus.PENDING
            or snapshot.created_at != created_at
        ):
            raise AccountRegistrationAuditError("Created registration request does not match the command.")

    @classmethod
    def _resolve_exact_retry(
        cls,
        *,
        stored: StoredAccountRegistrationOperation,
        command: SubmitAccountRegistrationRequest,
    ) -> SubmittedAccountRegistrationRequest:
        try:
            operation_type = AccountRegistrationAuditType(stored.type)
        except (TypeError, ValueError) as error:
            raise AccountRegistrationIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from error
        if (
            operation_type is not AccountRegistrationAuditType.REQUESTED
            or stored.request_fingerprint != command.request_fingerprint
        ):
            raise AccountRegistrationIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise AccountRegistrationAuditError("Exact-retry operation has no registration payload.")
        try:
            snapshot = AccountRegistrationRequestSnapshot.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as error:
            raise AccountRegistrationAuditError("Exact-retry registration payload is malformed.") from error
        if (
            stored.persona_id is not None
            or stored.game_account_id is not None
            or stored.discord_user_id != command.actor_discord_user_id
        ):
            raise AccountRegistrationAuditError("Exact-retry Identity operation context is malformed.")
        cls._verify_snapshot(
            snapshot=snapshot,
            command=command,
            requester=AccountRegistrationRequester(
                discord_account_id=snapshot.discord_account_id,
                discord_user_id=snapshot.requester_discord_user_id,
                persona_id=None,
            ),
            created_at=snapshot.created_at,
        )
        return SubmittedAccountRegistrationRequest(snapshot=snapshot, exact_retry=True)
