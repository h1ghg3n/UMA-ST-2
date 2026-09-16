"""Staff-only direct registration of a fresh Discord-backed Persona."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.domain.identity import GameRegion, PersonaStatus, normalize_registration_pid
from uma_st2.shared import normalize_utc_datetime

from .registration_bootstrap import (
    ACCOUNT_REGISTRATION_INITIAL_GRANT,
    AccountRegistrationAuditType,
    AccountRegistrationBootstrap,
    AccountRegistrationBootstrapService,
    CreateAccountRegistrationBootstrap,
    RegistrationBootstrapAuditError,
    RegistrationBootstrapConcurrentConflictError,
    RegistrationBootstrapIdempotencyConflictError,
    RegistrationBootstrapPidUnavailableError,
    RegistrationBootstrapRepository,
)

STAFF_DIRECT_REGISTRATION_AUDIT_SCHEMA_VERSION: Final = 1
STAFF_DIRECT_REGISTRATION_SOURCE: Final = "discord_staff"


class StaffDirectRegistrationError(ValueError):
    """Base error for rejected staff direct registrations."""


class StaffDirectRegistrationTargetLinkedError(StaffDirectRegistrationError):
    """The target Discord account already belongs to a Persona."""


class StaffDirectRegistrationPendingRequestError(StaffDirectRegistrationError):
    """The target has a pending request that must use the review workflow."""


class StaffDirectRegistrationPidUnavailableError(StaffDirectRegistrationError):
    """The requested region/PID is already registered."""


class StaffDirectRegistrationStaleError(StaffDirectRegistrationError):
    """The target state changed after Preview."""


class StaffDirectRegistrationIdempotencyConflictError(StaffDirectRegistrationError):
    """The Final interaction key belongs to another mutation."""


class StaffDirectRegistrationConcurrentConflictError(StaffDirectRegistrationError):
    """Another Identity mutation won."""


class StaffDirectRegistrationAuditError(StaffDirectRegistrationError):
    """Stored direct-registration evidence is malformed."""


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


def _payload_optional_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"{key} must be non-empty text or null.")
    return value


@dataclass(frozen=True, slots=True)
class StaffDirectRegistrationState:
    """Current DB authority projected for one proposed direct registration."""

    guild_id: str
    target_discord_user_id: str
    game_region: GameRegion
    uma_pid: str
    current_persona_id: str | None
    active_registration_request_id: int | None
    registered_game_account_id: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "target_discord_user_id", _discord_user_id(self.target_discord_user_id))
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))
        for field_name in (
            "active_registration_request_id",
            "registered_game_account_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _positive_int(value, field_name=field_name))
        if self.current_persona_id is not None:
            object.__setattr__(
                self,
                "current_persona_id",
                _text(self.current_persona_id, field_name="current_persona_id", max_length=36),
            )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_DIRECT_REGISTRATION_AUDIT_SCHEMA_VERSION,
            "source": STAFF_DIRECT_REGISTRATION_SOURCE,
            "guild_id": self.guild_id,
            "target_discord_user_id": self.target_discord_user_id,
            "game_region": self.game_region.value,
            "uma_pid": self.uma_pid,
            "current_persona_id": self.current_persona_id,
            "active_registration_request_id": self.active_registration_request_id,
            "registered_game_account_id": self.registered_game_account_id,
        }

    @property
    def state_fingerprint(self) -> str:
        return _fingerprint(self.to_audit_payload())


@dataclass(frozen=True, slots=True)
class StaffDirectRegistrationPreview:
    """Normalized complete private Preview with current DB authority."""

    state: StaffDirectRegistrationState
    target_display_name_snapshot: str
    nickname: str
    affiliation: str | None
    operational_note: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.state, StaffDirectRegistrationState):
            raise ValueError("state must be a StaffDirectRegistrationState.")
        for field_name, max_length, optional in (
            ("target_display_name_snapshot", 100, False),
            ("nickname", 100, False),
            ("affiliation", 100, True),
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


@dataclass(frozen=True, slots=True)
class DirectlyRegisterDiscordAccount:
    """Create one fresh approved Persona from one staff-reviewed input."""

    guild_id: str
    target_discord_user_id: str
    target_display_name_snapshot: str
    game_region: GameRegion
    uma_pid: str
    nickname: str
    affiliation: str | None
    registered_by_discord_user_id: str
    expected_target_fingerprint: str
    idempotency_key: str
    correlation_id: str | None = None
    operational_note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "target_discord_user_id", _discord_user_id(self.target_discord_user_id))
        object.__setattr__(
            self,
            "registered_by_discord_user_id",
            _discord_user_id(self.registered_by_discord_user_id),
        )
        for field_name, max_length, optional in (
            ("target_display_name_snapshot", 100, False),
            ("nickname", 100, False),
            ("affiliation", 100, True),
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
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "staff-direct-registration-command-v1",
                "guild_id": self.guild_id,
                "target_discord_user_id": self.target_discord_user_id,
                "target_display_name_snapshot": self.target_display_name_snapshot,
                "game_region": self.game_region.value,
                "uma_pid": self.uma_pid,
                "nickname": self.nickname,
                "affiliation": self.affiliation,
                "registered_by_discord_user_id": self.registered_by_discord_user_id,
                "expected_target_fingerprint": self.expected_target_fingerprint,
                "operational_note": self.operational_note,
            }
        )


@dataclass(frozen=True, slots=True)
class DirectlyRegisteredAccount:
    """Committed staff direct-registration receipt."""

    target_discord_user_id: str
    persona_id: str
    persona_display_name: str
    persona_status: PersonaStatus
    game_account_id: int
    game_region: GameRegion
    uma_pid: str
    nickname: str
    affiliation: str | None
    wallet_balance: int
    initial_grant_amount: int
    operational_note: str | None
    registered_at: datetime
    exact_retry: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_discord_user_id", _discord_user_id(self.target_discord_user_id))
        object.__setattr__(self, "game_account_id", _positive_int(self.game_account_id, field_name="game_account_id"))
        for field_name, max_length, optional in (
            ("persona_id", 36, False),
            ("persona_display_name", 100, False),
            ("nickname", 100, False),
            ("affiliation", 100, True),
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
        object.__setattr__(self, "persona_status", PersonaStatus(self.persona_status))
        if self.persona_status is not PersonaStatus.NORMAL:
            raise ValueError("Direct registration Persona must start normal.")
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))
        if self.wallet_balance != ACCOUNT_REGISTRATION_INITIAL_GRANT:
            raise ValueError("Direct registration wallet balance is invalid.")
        if self.initial_grant_amount != ACCOUNT_REGISTRATION_INITIAL_GRANT:
            raise ValueError("Direct registration grant is invalid.")
        object.__setattr__(
            self,
            "registered_at",
            normalize_utc_datetime(self.registered_at, field_name="registered_at"),
        )
        if not isinstance(self.exact_retry, bool):
            raise ValueError("exact_retry must be a boolean.")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_DIRECT_REGISTRATION_AUDIT_SCHEMA_VERSION,
            "source": STAFF_DIRECT_REGISTRATION_SOURCE,
            "target_discord_user_id": self.target_discord_user_id,
            "persona_id": self.persona_id,
            "persona_display_name": self.persona_display_name,
            "persona_status": self.persona_status.value,
            "game_account_id": self.game_account_id,
            "game_region": self.game_region.value,
            "uma_pid": self.uma_pid,
            "nickname": self.nickname,
            "affiliation": self.affiliation,
            "wallet_balance": self.wallet_balance,
            "initial_grant_amount": self.initial_grant_amount,
            "operational_note": self.operational_note,
            "registered_at": self.registered_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> DirectlyRegisteredAccount:
        if payload.get("schema_version") != STAFF_DIRECT_REGISTRATION_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported staff direct-registration audit schema version.")
        if payload.get("source") != STAFF_DIRECT_REGISTRATION_SOURCE:
            raise ValueError("Staff direct-registration audit source is invalid.")
        return cls(
            target_discord_user_id=_payload_text(payload, "target_discord_user_id"),
            persona_id=_payload_text(payload, "persona_id"),
            persona_display_name=_payload_text(payload, "persona_display_name"),
            persona_status=PersonaStatus(_payload_text(payload, "persona_status")),
            game_account_id=_positive_int(payload["game_account_id"], field_name="game_account_id"),
            game_region=GameRegion(_payload_text(payload, "game_region")),
            uma_pid=_payload_text(payload, "uma_pid"),
            nickname=_payload_text(payload, "nickname"),
            affiliation=_payload_optional_text(payload, "affiliation"),
            wallet_balance=_positive_int(payload["wallet_balance"], field_name="wallet_balance"),
            initial_grant_amount=_positive_int(
                payload["initial_grant_amount"],
                field_name="initial_grant_amount",
            ),
            operational_note=_payload_optional_text(payload, "operational_note"),
            registered_at=datetime.fromisoformat(_payload_text(payload, "registered_at")),
        )


@dataclass(frozen=True, slots=True)
class StoredStaffDirectRegistrationOperation:
    request_fingerprint: str | None
    type: str | None
    persona_id: str | None
    game_account_id: int | None
    discord_user_id: str | None
    after_data: Mapping[str, object] | None


class StaffDirectRegistrationQueryRepository(Protocol):
    def get_target_state(
        self,
        *,
        guild_id: str,
        target_discord_user_id: str,
        game_region: GameRegion,
        uma_pid: str,
    ) -> StaffDirectRegistrationState: ...


class StaffDirectRegistrationQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_direct_registration(self) -> StaffDirectRegistrationQueryRepository: ...


@dataclass(frozen=True, slots=True)
class StaffDirectRegistrationQueries:
    query_runner: QueryRunner[StaffDirectRegistrationQueryUnitOfWork]

    def get_preview(
        self,
        *,
        guild_id: str,
        target_discord_user_id: str,
        target_display_name_snapshot: str,
        game_region: GameRegion,
        uma_pid: str,
        nickname: str,
        affiliation: str | None,
        operational_note: str | None,
    ) -> StaffDirectRegistrationPreview:
        normalized_guild = _text(guild_id, field_name="guild_id", max_length=32)
        assert normalized_guild is not None
        state = self.query_runner.run(
            lambda uow: uow.staff_direct_registration.get_target_state(
                guild_id=normalized_guild,
                target_discord_user_id=_discord_user_id(target_discord_user_id),
                game_region=GameRegion(game_region),
                uma_pid=normalize_registration_pid(uma_pid),
            )
        )
        return StaffDirectRegistrationPreview(
            state=_validate_available_state(state),
            target_display_name_snapshot=target_display_name_snapshot,
            nickname=nickname,
            affiliation=affiliation,
            operational_note=operational_note,
        )


class StaffDirectRegistrationRepository(RegistrationBootstrapRepository, Protocol):
    def lock_target_state(
        self,
        *,
        guild_id: str,
        target_discord_user_id: str,
        game_region: GameRegion,
        uma_pid: str,
        created_at: datetime,
    ) -> StaffDirectRegistrationState: ...

    def find_operation(self, *, idempotency_key: str) -> StoredStaffDirectRegistrationOperation | None: ...


class StaffDirectRegistrationUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_direct_registration(self) -> StaffDirectRegistrationRepository: ...


@dataclass(frozen=True, slots=True)
class StaffDirectRegistrationCommands:
    command_runner: CommandRunner[StaffDirectRegistrationUnitOfWork]
    clock: Callable[[], datetime]
    persona_id_factory: Callable[[], str]
    bootstrap_service: AccountRegistrationBootstrapService = AccountRegistrationBootstrapService()

    def register(self, command: DirectlyRegisterDiscordAccount) -> DirectlyRegisteredAccount:
        return self.command_runner.run(lambda uow: self._register(uow.staff_direct_registration, command))

    def _register(
        self,
        repository: StaffDirectRegistrationRepository,
        command: DirectlyRegisterDiscordAccount,
    ) -> DirectlyRegisteredAccount:
        registered_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        state = repository.lock_target_state(
            guild_id=command.guild_id,
            target_discord_user_id=command.target_discord_user_id,
            game_region=command.game_region,
            uma_pid=command.uma_pid,
            created_at=registered_at,
        )
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_retry(stored=stored, command=command)
        target = _validate_available_state(state)
        if target.state_fingerprint != command.expected_target_fingerprint:
            raise StaffDirectRegistrationStaleError("Direct-registration authority changed after Preview.")
        persona_id = _text(self.persona_id_factory(), field_name="persona_id", max_length=36)
        assert persona_id is not None
        bootstrap_command = CreateAccountRegistrationBootstrap(
            guild_id=command.guild_id,
            actor_discord_user_id=command.registered_by_discord_user_id,
            target_discord_user_id=command.target_discord_user_id,
            persona_id=persona_id,
            persona_display_name=command.target_display_name_snapshot,
            game_region=command.game_region,
            uma_pid=command.uma_pid,
            nickname=command.nickname,
            affiliation=command.affiliation,
            audit_type=AccountRegistrationAuditType.DIRECTLY_REGISTERED,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            registered_at=registered_at,
            correlation_id=command.correlation_id,
            reason=command.operational_note,
        )
        try:
            result = self.bootstrap_service.register(
                repository,
                bootstrap_command,
                before_data=target.to_audit_payload(),
                receipt_factory=lambda bootstrap: self._receipt(
                    bootstrap=bootstrap,
                    operational_note=command.operational_note,
                ),
            )
        except RegistrationBootstrapPidUnavailableError as error:
            raise StaffDirectRegistrationPidUnavailableError(str(error)) from error
        except RegistrationBootstrapIdempotencyConflictError as error:
            raise StaffDirectRegistrationIdempotencyConflictError(str(error)) from error
        except RegistrationBootstrapConcurrentConflictError as error:
            raise StaffDirectRegistrationConcurrentConflictError(str(error)) from error
        except RegistrationBootstrapAuditError as error:
            raise StaffDirectRegistrationAuditError(str(error)) from error
        self._verify_result(result=result, command=command, persona_id=persona_id, registered_at=registered_at)
        return result

    @staticmethod
    def _receipt(
        *,
        bootstrap: AccountRegistrationBootstrap,
        operational_note: str | None,
    ) -> DirectlyRegisteredAccount:
        return DirectlyRegisteredAccount(
            target_discord_user_id=bootstrap.target_discord_user_id,
            persona_id=bootstrap.persona_id,
            persona_display_name=bootstrap.persona_display_name,
            persona_status=bootstrap.persona_status,
            game_account_id=bootstrap.game_account_id,
            game_region=bootstrap.game_region,
            uma_pid=bootstrap.uma_pid,
            nickname=bootstrap.nickname,
            affiliation=bootstrap.affiliation,
            wallet_balance=bootstrap.wallet_balance,
            initial_grant_amount=bootstrap.initial_grant_amount,
            operational_note=operational_note,
            registered_at=bootstrap.registered_at,
        )

    @staticmethod
    def _verify_result(
        *,
        result: DirectlyRegisteredAccount,
        command: DirectlyRegisterDiscordAccount,
        persona_id: str,
        registered_at: datetime,
    ) -> None:
        if (
            result.target_discord_user_id != command.target_discord_user_id
            or result.persona_id != persona_id
            or result.persona_display_name != command.target_display_name_snapshot
            or result.persona_status is not PersonaStatus.NORMAL
            or result.game_region is not command.game_region
            or result.uma_pid != command.uma_pid
            or result.nickname != command.nickname
            or result.affiliation != command.affiliation
            or result.operational_note != command.operational_note
            or result.wallet_balance != ACCOUNT_REGISTRATION_INITIAL_GRANT
            or result.initial_grant_amount != ACCOUNT_REGISTRATION_INITIAL_GRANT
            or result.registered_at != registered_at
            or result.exact_retry
        ):
            raise StaffDirectRegistrationAuditError("Direct-registration receipt is inconsistent.")

    @staticmethod
    def _resolve_retry(
        *,
        stored: StoredStaffDirectRegistrationOperation,
        command: DirectlyRegisterDiscordAccount,
    ) -> DirectlyRegisteredAccount:
        if (
            stored.type != AccountRegistrationAuditType.DIRECTLY_REGISTERED.value
            or stored.request_fingerprint != command.request_fingerprint
        ):
            raise StaffDirectRegistrationIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise StaffDirectRegistrationAuditError("Direct-registration retry has no receipt payload.")
        try:
            result = DirectlyRegisteredAccount.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as error:
            raise StaffDirectRegistrationAuditError("Direct-registration retry receipt is malformed.") from error
        if (
            stored.persona_id != result.persona_id
            or stored.game_account_id != result.game_account_id
            or stored.discord_user_id != result.target_discord_user_id
            or result.target_discord_user_id != command.target_discord_user_id
            or result.persona_display_name != command.target_display_name_snapshot
            or result.game_region is not command.game_region
            or result.uma_pid != command.uma_pid
            or result.nickname != command.nickname
            or result.affiliation != command.affiliation
            or result.operational_note != command.operational_note
        ):
            raise StaffDirectRegistrationAuditError("Direct-registration retry context is malformed.")
        return replace(result, exact_retry=True)


def _validate_available_state(state: StaffDirectRegistrationState) -> StaffDirectRegistrationState:
    if state.current_persona_id is not None:
        raise StaffDirectRegistrationTargetLinkedError("Discord target is already linked to a Persona.")
    if state.active_registration_request_id is not None:
        raise StaffDirectRegistrationPendingRequestError(
            "Pending registration request must be reviewed before direct registration."
        )
    if state.registered_game_account_id is not None:
        raise StaffDirectRegistrationPidUnavailableError("Registration PID is already registered.")
    return state
