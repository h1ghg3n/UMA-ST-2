"""Staff-only Persona-owned Circle Point grant and adjustment operations."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.domain.identity import PersonaStatus
from uma_st2.domain.point import calculate_next_circle_point_balance
from uma_st2.shared import normalize_utc_datetime

MAX_CIRCLE_POINT_BALANCE: Final = (1 << 63) - 1
MANUAL_GRANT_POINT_ACTION: Final = "manual_grant"
MANUAL_ADJUSTMENT_POINT_ACTION: Final = "manual_adjustment"


class StaffCirclePointOperation(StrEnum):
    """Explicit staff mutation meaning exposed by the two Discord leaves."""

    GRANT = "grant"
    ADJUSTMENT = "adjustment"

    @property
    def point_action(self) -> str:
        if self is StaffCirclePointOperation.GRANT:
            return MANUAL_GRANT_POINT_ACTION
        return MANUAL_ADJUSTMENT_POINT_ACTION


class StaffCirclePointError(ValueError):
    """Base error for rejected staff Circle Point operations."""


class StaffCirclePointNotFoundError(StaffCirclePointError):
    """The selected Persona no longer exists."""


class StaffCirclePointWalletUnavailableError(StaffCirclePointError):
    """The selected Persona has no canonical wallet."""


class StaffCirclePointAmountError(StaffCirclePointError):
    """The signed amount or resulting balance is invalid."""


class StaffCirclePointStaleError(StaffCirclePointError):
    """The selected Persona or wallet changed after Preview."""


class StaffCirclePointIdempotencyConflictError(StaffCirclePointError):
    """The Final interaction key belongs to another logical operation."""


class StaffCirclePointConcurrentConflictError(StaffCirclePointError):
    """Another concurrent Point mutation won."""


class StaffCirclePointAuditError(StaffCirclePointError):
    """Stored operation/transaction evidence is incomplete or malformed."""


def _text(
    value: object,
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


def _persona_id(value: object) -> str:
    normalized = _text(value, field_name="persona_id", max_length=36)
    assert normalized is not None
    return normalized


def _discord_user_id(value: object) -> str:
    normalized = _text(value, field_name="discord_user_id", max_length=32)
    assert normalized is not None
    if not normalized.isascii() or not normalized.isdecimal() or normalized.startswith("0"):
        raise ValueError("discord_user_id must be a positive ASCII Discord snowflake.")
    return normalized


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _balance(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_CIRCLE_POINT_BALANCE:
        raise StaffCirclePointAmountError(f"{field_name} must be a non-negative signed BIGINT.")
    return value


def _operation_amount(operation: StaffCirclePointOperation, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StaffCirclePointAmountError("Circle Point amount must be an integer.")
    if operation is StaffCirclePointOperation.GRANT:
        if not 0 < value <= MAX_CIRCLE_POINT_BALANCE:
            raise StaffCirclePointAmountError("Circle Point grant must be a positive signed BIGINT.")
    elif value == 0 or not -MAX_CIRCLE_POINT_BALANCE <= value <= MAX_CIRCLE_POINT_BALANCE:
        raise StaffCirclePointAmountError("Circle Point adjustment must be a non-zero signed BIGINT.")
    return value


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StaffCirclePointChoice:
    """One bounded Persona autocomplete projection."""

    persona_id: str
    display_name: str
    status: PersonaStatus
    wallet_available: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(self, "display_name", _text(self.display_name, field_name="display_name", max_length=100))
        object.__setattr__(self, "status", PersonaStatus(self.status))
        if not isinstance(self.wallet_available, bool):
            raise ValueError("wallet_available must be a boolean.")


@dataclass(frozen=True, slots=True)
class StaffCirclePointState:
    """Complete detached Persona and wallet authority used by Preview."""

    guild_id: str
    persona_id: str
    display_name: str
    status: PersonaStatus
    persona_updated_at: datetime
    balance: int | None
    wallet_updated_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(self, "display_name", _text(self.display_name, field_name="display_name", max_length=100))
        object.__setattr__(self, "status", PersonaStatus(self.status))
        object.__setattr__(
            self,
            "persona_updated_at",
            normalize_utc_datetime(self.persona_updated_at, field_name="persona_updated_at"),
        )
        if (self.balance is None) != (self.wallet_updated_at is None):
            raise ValueError("Wallet balance and timestamp must be present together.")
        if self.balance is not None:
            object.__setattr__(self, "balance", _balance(self.balance, field_name="balance"))
            object.__setattr__(
                self,
                "wallet_updated_at",
                normalize_utc_datetime(self.wallet_updated_at, field_name="wallet_updated_at"),
            )

    @property
    def state_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "staff-circle-point-state-v1",
                "guild_id": self.guild_id,
                "persona_id": self.persona_id,
                "display_name": self.display_name,
                "status": self.status.value,
                "persona_updated_at": self.persona_updated_at.isoformat(),
                "balance": self.balance,
                "wallet_updated_at": None if self.wallet_updated_at is None else self.wallet_updated_at.isoformat(),
            }
        )


@dataclass(frozen=True, slots=True)
class StaffCirclePointPreview:
    """One complete zero-write staff Point consequence Preview."""

    state: StaffCirclePointState
    operation: StaffCirclePointOperation
    amount: int
    reason: str
    resulting_balance: int

    def __post_init__(self) -> None:
        if not isinstance(self.state, StaffCirclePointState):
            raise ValueError("state must be a StaffCirclePointState.")
        object.__setattr__(self, "operation", StaffCirclePointOperation(self.operation))
        object.__setattr__(self, "amount", _operation_amount(self.operation, self.amount))
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        object.__setattr__(self, "resulting_balance", _balance(self.resulting_balance, field_name="resulting_balance"))
        if self.state.balance is None:
            raise StaffCirclePointWalletUnavailableError("Selected Persona has no canonical Circle Point wallet.")
        if calculate_next_circle_point_balance(self.state.balance, self.amount) != self.resulting_balance:
            raise ValueError("resulting_balance does not match the current balance and amount.")


@dataclass(frozen=True, slots=True)
class ApplyStaffCirclePoint:
    """Final staff mutation command bound to one complete Preview."""

    guild_id: str
    target_persona_id: str
    operation: StaffCirclePointOperation
    amount: int
    reason: str
    expected_target_fingerprint: str
    actor_discord_user_id: str
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "target_persona_id", _persona_id(self.target_persona_id))
        object.__setattr__(self, "operation", StaffCirclePointOperation(self.operation))
        object.__setattr__(self, "amount", _operation_amount(self.operation, self.amount))
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        object.__setattr__(
            self,
            "expected_target_fingerprint",
            _text(
                self.expected_target_fingerprint,
                field_name="expected_target_fingerprint",
                max_length=64,
            ),
        )
        object.__setattr__(self, "actor_discord_user_id", _discord_user_id(self.actor_discord_user_id))
        object.__setattr__(
            self,
            "idempotency_key",
            _text(self.idempotency_key, field_name="idempotency_key", max_length=128),
        )
        object.__setattr__(
            self,
            "correlation_id",
            _text(self.correlation_id, field_name="correlation_id", max_length=128, optional=True),
        )

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "staff-circle-point-command-v1",
                "guild_id": self.guild_id,
                "target_persona_id": self.target_persona_id,
                "operation": self.operation.value,
                "amount": self.amount,
                "reason": self.reason,
                "expected_target_fingerprint": self.expected_target_fingerprint,
                "actor_discord_user_id": self.actor_discord_user_id,
            }
        )


@dataclass(frozen=True, slots=True)
class AppliedStaffCirclePoint:
    """Committed Point transaction and receipt-time current wallet projection."""

    transaction_id: int
    persona_id: str
    display_name: str
    status: PersonaStatus
    operation: StaffCirclePointOperation
    action: str
    amount: int
    current_balance: int
    reason: str
    created_at: datetime
    exact_retry: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_id", _positive_int(self.transaction_id, field_name="transaction_id"))
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(self, "display_name", _text(self.display_name, field_name="display_name", max_length=100))
        object.__setattr__(self, "status", PersonaStatus(self.status))
        object.__setattr__(self, "operation", StaffCirclePointOperation(self.operation))
        object.__setattr__(self, "action", _text(self.action, field_name="action", max_length=32))
        object.__setattr__(self, "amount", _operation_amount(self.operation, self.amount))
        object.__setattr__(self, "current_balance", _balance(self.current_balance, field_name="current_balance"))
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        object.__setattr__(self, "created_at", normalize_utc_datetime(self.created_at, field_name="created_at"))
        if self.action != self.operation.point_action:
            raise ValueError("Point action does not match the operation kind.")
        if not isinstance(self.exact_retry, bool):
            raise ValueError("exact_retry must be a boolean.")


@dataclass(frozen=True, slots=True)
class StoredStaffCirclePointOperation:
    """Existing generic Operation plus linked Point transaction evidence."""

    request_fingerprint: str | None
    result: AppliedStaffCirclePoint | None


class StaffCirclePointQueryRepository(Protocol):
    def search_targets(self, *, query: str, limit: int) -> tuple[StaffCirclePointChoice, ...]: ...

    def get_state(self, *, guild_id: str, persona_id: str) -> StaffCirclePointState | None: ...


class StaffCirclePointQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_circle_points(self) -> StaffCirclePointQueryRepository: ...


@dataclass(frozen=True, slots=True)
class StaffCirclePointQueries:
    """Read-only target search and complete consequence Preview entry point."""

    query_runner: QueryRunner[StaffCirclePointQueryUnitOfWork]

    def search_targets(self, *, query: str, limit: int = 25) -> tuple[StaffCirclePointChoice, ...]:
        normalized_query = _text(query, field_name="query", max_length=100, optional=True) or ""
        if not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25.")
        return self.query_runner.run(
            lambda uow: uow.staff_circle_points.search_targets(query=normalized_query, limit=limit)
        )

    def get_state(self, *, guild_id: str, persona_id: str) -> StaffCirclePointState:
        state = self.query_runner.run(
            lambda uow: uow.staff_circle_points.get_state(
                guild_id=_required_guild_id(guild_id),
                persona_id=_persona_id(persona_id),
            )
        )
        if state is None:
            raise StaffCirclePointNotFoundError("Selected Persona does not exist.")
        return state

    def get_preview(
        self,
        *,
        guild_id: str,
        persona_id: str,
        operation: StaffCirclePointOperation | str,
        amount: int,
        reason: str,
    ) -> StaffCirclePointPreview:
        state = self.get_state(guild_id=guild_id, persona_id=persona_id)
        normalized_operation = StaffCirclePointOperation(operation)
        normalized_amount = _operation_amount(normalized_operation, amount)
        if state.balance is None:
            raise StaffCirclePointWalletUnavailableError("Selected Persona has no canonical Circle Point wallet.")
        resulting_balance = calculate_next_circle_point_balance(state.balance, normalized_amount)
        _balance(resulting_balance, field_name="resulting_balance")
        return StaffCirclePointPreview(
            state=state,
            operation=normalized_operation,
            amount=normalized_amount,
            reason=reason,
            resulting_balance=resulting_balance,
        )


class StaffCirclePointRepository(Protocol):
    def lock_state(self, *, guild_id: str, persona_id: str) -> StaffCirclePointState | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredStaffCirclePointOperation | None: ...

    def apply(
        self,
        *,
        command: ApplyStaffCirclePoint,
        state: StaffCirclePointState,
        resulting_balance: int,
        created_at: datetime,
    ) -> AppliedStaffCirclePoint: ...


class StaffCirclePointUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_circle_points(self) -> StaffCirclePointRepository: ...


@dataclass(frozen=True, slots=True)
class StaffCirclePointCommands:
    """Apply one staff Point delta in one fresh command UoW."""

    command_runner: CommandRunner[StaffCirclePointUnitOfWork]
    clock: Callable[[], datetime]

    def apply(self, command: ApplyStaffCirclePoint) -> AppliedStaffCirclePoint:
        return self.command_runner.run(lambda uow: self._apply(uow.staff_circle_points, command))

    def _apply(
        self,
        repository: StaffCirclePointRepository,
        command: ApplyStaffCirclePoint,
    ) -> AppliedStaffCirclePoint:
        created_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        state = repository.lock_state(guild_id=command.guild_id, persona_id=command.target_persona_id)
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_retry(stored=stored, command=command)
        if state is None:
            raise StaffCirclePointNotFoundError("Selected Persona does not exist.")
        if state.balance is None:
            raise StaffCirclePointWalletUnavailableError("Selected Persona has no canonical Circle Point wallet.")
        if state.state_fingerprint != command.expected_target_fingerprint:
            raise StaffCirclePointStaleError("Persona or Circle Point authority changed after Preview.")
        resulting_balance = calculate_next_circle_point_balance(state.balance, command.amount)
        _balance(resulting_balance, field_name="resulting_balance")
        result = repository.apply(
            command=command,
            state=state,
            resulting_balance=resulting_balance,
            created_at=created_at,
        )
        if (
            result.persona_id != state.persona_id
            or result.display_name != state.display_name
            or result.status is not state.status
            or result.operation is not command.operation
            or result.action != command.operation.point_action
            or result.amount != command.amount
            or result.current_balance != resulting_balance
            or result.reason != command.reason
            or result.created_at != created_at
            or result.exact_retry
        ):
            raise StaffCirclePointAuditError("Staff Circle Point receipt is inconsistent.")
        return result

    @staticmethod
    def _resolve_retry(
        *,
        stored: StoredStaffCirclePointOperation,
        command: ApplyStaffCirclePoint,
    ) -> AppliedStaffCirclePoint:
        if stored.request_fingerprint != command.request_fingerprint:
            raise StaffCirclePointIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        result = stored.result
        if result is None:
            raise StaffCirclePointAuditError("Staff Circle Point retry has no linked transaction receipt.")
        if (
            result.persona_id != command.target_persona_id
            or result.operation is not command.operation
            or result.action != command.operation.point_action
            or result.amount != command.amount
            or result.reason != command.reason
        ):
            raise StaffCirclePointAuditError("Staff Circle Point retry context is malformed.")
        return replace(result, exact_retry=True)


def _required_guild_id(value: object) -> str:
    normalized = _text(value, field_name="guild_id", max_length=32)
    assert normalized is not None
    return normalized
