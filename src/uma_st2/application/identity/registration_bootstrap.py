"""Shared canonical bootstrap for approved native account registration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol, TypeVar

from uma_st2.domain.identity import GameRegion, PersonaStatus, normalize_registration_pid
from uma_st2.shared import normalize_utc_datetime

ACCOUNT_REGISTRATION_INITIAL_GRANT: Final = 500
ACCOUNT_REGISTRATION_INITIAL_GRANT_POINT_ACTION: Final = "initial_grant"


class AccountRegistrationAuditType(StrEnum):
    """Canonical Identity operation types for native registration workflows."""

    REQUESTED = "account_registration_requested"
    APPROVED = "account_registration_approved"
    REJECTED = "account_registration_rejected"
    DIRECTLY_REGISTERED = "account_directly_registered"


class RegistrationBootstrapError(ValueError):
    """Base error for canonical registration bootstrap failures."""


class RegistrationBootstrapPidUnavailableError(RegistrationBootstrapError):
    """The requested canonical region/PID is already registered."""


class RegistrationBootstrapIdempotencyConflictError(RegistrationBootstrapError):
    """The operation key belongs to another logical mutation."""


class RegistrationBootstrapConcurrentConflictError(RegistrationBootstrapError):
    """A concurrent registration bootstrap won."""


class RegistrationBootstrapAuditError(RegistrationBootstrapError):
    """The shared bootstrap could not preserve complete evidence."""


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


@dataclass(frozen=True, slots=True)
class CreateAccountRegistrationBootstrap:
    """Complete input for the shared Persona/account/wallet/grant bootstrap."""

    guild_id: str
    actor_discord_user_id: str
    target_discord_user_id: str
    persona_id: str
    persona_display_name: str
    game_region: GameRegion
    uma_pid: str
    nickname: str
    affiliation: str | None
    audit_type: AccountRegistrationAuditType
    idempotency_key: str
    request_fingerprint: str
    registered_at: datetime
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        for field_name, max_length, optional in (
            ("guild_id", 32, False),
            ("actor_discord_user_id", 32, False),
            ("target_discord_user_id", 32, False),
            ("persona_id", 36, False),
            ("persona_display_name", 100, False),
            ("nickname", 100, False),
            ("affiliation", 100, True),
            ("idempotency_key", 128, False),
            ("request_fingerprint", 64, False),
            ("correlation_id", 128, True),
            ("reason", 255, True),
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
        object.__setattr__(self, "audit_type", AccountRegistrationAuditType(self.audit_type))
        if self.audit_type not in {
            AccountRegistrationAuditType.APPROVED,
            AccountRegistrationAuditType.DIRECTLY_REGISTERED,
        }:
            raise ValueError("audit_type is not a registration bootstrap operation.")
        object.__setattr__(
            self,
            "registered_at",
            normalize_utc_datetime(self.registered_at, field_name="registered_at"),
        )


@dataclass(frozen=True, slots=True)
class AccountRegistrationBootstrap:
    """Detached shared bootstrap facts created before the source-specific audit."""

    operation_id: int
    persona_id: str
    persona_display_name: str
    persona_status: PersonaStatus
    target_discord_user_id: str
    game_account_id: int
    game_region: GameRegion
    uma_pid: str
    nickname: str
    affiliation: str | None
    wallet_balance: int
    initial_grant_amount: int
    registered_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", _positive_int(self.operation_id, field_name="operation_id"))
        object.__setattr__(self, "game_account_id", _positive_int(self.game_account_id, field_name="game_account_id"))
        for field_name, max_length, optional in (
            ("persona_id", 36, False),
            ("persona_display_name", 100, False),
            ("target_discord_user_id", 32, False),
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
        object.__setattr__(self, "persona_status", PersonaStatus(self.persona_status))
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))
        if self.wallet_balance != ACCOUNT_REGISTRATION_INITIAL_GRANT:
            raise ValueError("Registration bootstrap wallet balance is invalid.")
        if self.initial_grant_amount != ACCOUNT_REGISTRATION_INITIAL_GRANT:
            raise ValueError("Registration bootstrap grant is invalid.")
        object.__setattr__(
            self,
            "registered_at",
            normalize_utc_datetime(self.registered_at, field_name="registered_at"),
        )


class RegistrationBootstrapRepository(Protocol):
    """Persistence steps that remain inside one caller-owned transaction."""

    def create_registration_bootstrap(
        self,
        *,
        command: CreateAccountRegistrationBootstrap,
    ) -> AccountRegistrationBootstrap: ...

    def complete_registration_bootstrap_audit(
        self,
        *,
        command: CreateAccountRegistrationBootstrap,
        bootstrap: AccountRegistrationBootstrap,
        before_data: Mapping[str, object] | None,
        after_data: Mapping[str, object],
    ) -> None: ...


class RegistrationBootstrapReceipt(Protocol):
    def to_audit_payload(self) -> dict[str, object]: ...


_ReceiptT = TypeVar("_ReceiptT", bound=RegistrationBootstrapReceipt)


@dataclass(frozen=True, slots=True)
class AccountRegistrationBootstrapService:
    """One final canonical path shared by approval and direct registration."""

    def register(
        self,
        repository: RegistrationBootstrapRepository,
        command: CreateAccountRegistrationBootstrap,
        *,
        before_data: Mapping[str, object] | None,
        receipt_factory: Callable[[AccountRegistrationBootstrap], _ReceiptT],
    ) -> _ReceiptT:
        bootstrap = repository.create_registration_bootstrap(command=command)
        self._verify_bootstrap(command=command, bootstrap=bootstrap)
        receipt = receipt_factory(bootstrap)
        repository.complete_registration_bootstrap_audit(
            command=command,
            bootstrap=bootstrap,
            before_data=before_data,
            after_data=receipt.to_audit_payload(),
        )
        return receipt

    @staticmethod
    def _verify_bootstrap(
        *,
        command: CreateAccountRegistrationBootstrap,
        bootstrap: AccountRegistrationBootstrap,
    ) -> None:
        if (
            bootstrap.persona_id != command.persona_id
            or bootstrap.persona_display_name != command.persona_display_name
            or bootstrap.persona_status is not PersonaStatus.NORMAL
            or bootstrap.target_discord_user_id != command.target_discord_user_id
            or bootstrap.game_region is not command.game_region
            or bootstrap.uma_pid != command.uma_pid
            or bootstrap.nickname != command.nickname
            or bootstrap.affiliation != command.affiliation
            or bootstrap.wallet_balance != ACCOUNT_REGISTRATION_INITIAL_GRANT
            or bootstrap.initial_grant_amount != ACCOUNT_REGISTRATION_INITIAL_GRANT
            or bootstrap.registered_at != command.registered_at
        ):
            raise RegistrationBootstrapAuditError("Registration bootstrap receipt is inconsistent.")
