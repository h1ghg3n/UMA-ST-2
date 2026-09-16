"""WIN5 Season creation, lifecycle, and metadata application boundary."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.win5 import Win5RoundStatus, Win5SeasonStatus
from uma_st2.shared import normalize_utc_datetime

WIN5_SEASON_LIFECYCLE_AUDIT_SCHEMA_VERSION = 1


class Win5SeasonAction(StrEnum):
    """Operator-facing Season management actions."""

    CREATE = "create"
    ACTIVATE = "activate"
    CLOSE = "close"
    CANCEL = "cancel"
    EDIT = "edit"


class Win5SeasonAuditType(StrEnum):
    """Canonical operation types for Season mutations."""

    CREATED = "season_created"
    ACTIVATED = "season_activated"
    CLOSED = "season_closed"
    CANCELLED = "season_cancelled"
    METADATA_UPDATED = "season_metadata_updated"


class Win5SeasonLifecycleError(ValueError):
    """Base error for rejected Season commands."""


class Win5SeasonLifecycleUnavailableError(Win5SeasonLifecycleError):
    """The selected Season no longer permits the requested operation."""


class Win5SeasonLifecycleInvalidSourceError(Win5SeasonLifecycleError):
    """Stored Season or child-Round facts are malformed."""


class Win5SeasonActivationConflictError(Win5SeasonLifecycleError):
    """Another active Season won the singleton constraint."""


class Win5SeasonLifecycleIdempotencyConflictError(Win5SeasonLifecycleError):
    """An idempotency key is bound to another logical command."""


class Win5SeasonLifecycleAuditError(Win5SeasonLifecycleError):
    """Stored exact-retry audit data is absent or malformed."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _require_non_negative_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


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


def _normalized_name(value: str, *, field_name: str) -> str:
    _require_bounded_string(value, field_name=field_name, max_length=100)
    return value.strip()


def _normalize_optional_datetime(value: datetime | None, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    return normalize_utc_datetime(value, field_name=field_name)


def _datetime_token(value: datetime | None) -> str:
    return "null" if value is None else value.isoformat()


@dataclass(frozen=True, slots=True)
class CreateWin5Season:
    """Create one draft Season with operator-authored reference metadata."""

    name: str
    starts_at: datetime | None
    ends_at: datetime | None
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str | None = None
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _normalized_name(self.name, field_name="name"))
        object.__setattr__(self, "starts_at", _normalize_optional_datetime(self.starts_at, field_name="starts_at"))
        object.__setattr__(self, "ends_at", _normalize_optional_datetime(self.ends_at, field_name="ends_at"))
        _validate_operation_context(self)

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            "create",
            self.name,
            _datetime_token(self.starts_at),
            _datetime_token(self.ends_at),
        )


@dataclass(frozen=True, slots=True)
class TransitionWin5Season:
    """Apply one forward-only Season status transition."""

    season_id: int
    action: Win5SeasonAction
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str | None = None
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        object.__setattr__(self, "action", Win5SeasonAction(self.action))
        if self.action not in {
            Win5SeasonAction.ACTIVATE,
            Win5SeasonAction.CLOSE,
            Win5SeasonAction.CANCEL,
        }:
            raise ValueError("action must be activate, close, or cancel.")
        _validate_operation_context(self)

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(self.action.value, str(self.season_id))


@dataclass(frozen=True, slots=True)
class UpdateWin5SeasonMetadata:
    """Replace the complete desired mutable metadata of one Season."""

    season_id: int
    name: str
    starts_at: datetime | None
    ends_at: datetime | None
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str | None = None
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        object.__setattr__(self, "name", _normalized_name(self.name, field_name="name"))
        object.__setattr__(self, "starts_at", _normalize_optional_datetime(self.starts_at, field_name="starts_at"))
        object.__setattr__(self, "ends_at", _normalize_optional_datetime(self.ends_at, field_name="ends_at"))
        _validate_operation_context(self)

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            "edit",
            str(self.season_id),
            self.name,
            _datetime_token(self.starts_at),
            _datetime_token(self.ends_at),
        )


def _validate_operation_context(
    command: CreateWin5Season | TransitionWin5Season | UpdateWin5SeasonMetadata,
) -> None:
    _require_bounded_string(command.idempotency_key, field_name="idempotency_key", max_length=128)
    _require_bounded_string(
        command.actor_discord_user_id,
        field_name="actor_discord_user_id",
        max_length=32,
    )
    _require_bounded_string(command.guild_id, field_name="guild_id", max_length=32, optional=True)
    _require_bounded_string(
        command.correlation_id,
        field_name="correlation_id",
        max_length=128,
        optional=True,
    )
    _require_bounded_string(command.reason, field_name="reason", max_length=255, optional=True)


def _fingerprint(*parts: str) -> str:
    canonical = "\n".join(("win5-season-command-v1", *parts))
    return sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Win5SeasonRoundState:
    """Current child-Round status counts captured at the command boundary."""

    setup: int = 0
    open: int = 0
    closed: int = 0
    scored: int = 0
    cancelled: int = 0

    def __post_init__(self) -> None:
        for field_name in ("setup", "open", "closed", "scored", "cancelled"):
            _require_non_negative_int(getattr(self, field_name), field_name=field_name)

    @property
    def total(self) -> int:
        return self.setup + self.open + self.closed + self.scored + self.cancelled

    @classmethod
    def from_statuses(cls, statuses: tuple[Win5RoundStatus, ...]) -> Win5SeasonRoundState:
        counts = Counter(Win5RoundStatus(status) for status in statuses)
        return cls(
            setup=counts[Win5RoundStatus.SETUP],
            open=counts[Win5RoundStatus.OPEN],
            closed=counts[Win5RoundStatus.CLOSED],
            scored=counts[Win5RoundStatus.SCORED],
            cancelled=counts[Win5RoundStatus.CANCELLED],
        )

    def to_audit_payload(self) -> dict[str, int]:
        return {
            "setup": self.setup,
            "open": self.open,
            "closed": self.closed,
            "scored": self.scored,
            "cancelled": self.cancelled,
        }


@dataclass(frozen=True, slots=True)
class Win5SeasonSnapshot:
    """Closed-session Season result and deterministic audit snapshot."""

    id: int
    name: str
    status: Win5SeasonStatus
    starts_at: datetime | None
    ends_at: datetime | None
    rounds: Win5SeasonRoundState

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_bounded_string(self.name, field_name="name", max_length=100)
        object.__setattr__(self, "status", Win5SeasonStatus(self.status))
        object.__setattr__(self, "starts_at", _normalize_optional_datetime(self.starts_at, field_name="starts_at"))
        object.__setattr__(self, "ends_at", _normalize_optional_datetime(self.ends_at, field_name="ends_at"))
        if not isinstance(self.rounds, Win5SeasonRoundState):
            raise ValueError("rounds must be a Win5SeasonRoundState.")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": WIN5_SEASON_LIFECYCLE_AUDIT_SCHEMA_VERSION,
            "season_id": self.id,
            "season_name": self.name,
            "season_status": self.status.value,
            "starts_at": None if self.starts_at is None else self.starts_at.isoformat(),
            "ends_at": None if self.ends_at is None else self.ends_at.isoformat(),
            "round_status_counts": self.rounds.to_audit_payload(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> Win5SeasonSnapshot:
        if _payload_int(payload, "schema_version") != WIN5_SEASON_LIFECYCLE_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported WIN5 Season audit schema version.")
        counts = payload["round_status_counts"]
        if not isinstance(counts, Mapping):
            raise ValueError("round_status_counts must be an object.")
        return cls(
            id=_payload_int(payload, "season_id"),
            name=_payload_string(payload, "season_name"),
            status=Win5SeasonStatus(_payload_string(payload, "season_status")),
            starts_at=_payload_datetime(payload, "starts_at"),
            ends_at=_payload_datetime(payload, "ends_at"),
            rounds=Win5SeasonRoundState(
                setup=_payload_int(counts, "setup", allow_zero=True),
                open=_payload_int(counts, "open", allow_zero=True),
                closed=_payload_int(counts, "closed", allow_zero=True),
                scored=_payload_int(counts, "scored", allow_zero=True),
                cancelled=_payload_int(counts, "cancelled", allow_zero=True),
            ),
        )


def _payload_int(payload: Mapping[str, object], key: str, *, allow_zero: bool = False) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise ValueError(f"{key} must be a valid integer.")
    return value


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


def _payload_datetime(payload: Mapping[str, object], key: str) -> datetime | None:
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be an ISO datetime or null.")
    return normalize_utc_datetime(datetime.fromisoformat(value), field_name=key)


@dataclass(frozen=True, slots=True)
class ChangedWin5Season:
    """Committed or exact-retry Season result; changed false denotes a no-op."""

    snapshot: Win5SeasonSnapshot
    operation_type: Win5SeasonAuditType | None
    changed: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, Win5SeasonSnapshot):
            raise ValueError("snapshot must be a Win5SeasonSnapshot.")
        if self.operation_type is not None:
            object.__setattr__(self, "operation_type", Win5SeasonAuditType(self.operation_type))
        if not isinstance(self.changed, bool):
            raise ValueError("changed must be a boolean.")
        if self.changed != (self.operation_type is not None):
            raise ValueError("changed and operation_type must describe the same mutation result.")


@dataclass(frozen=True, slots=True)
class Win5SeasonAuditRecord:
    """One canonical Season operation audit prepared by Application."""

    type: Win5SeasonAuditType
    before: Win5SeasonSnapshot | None
    after: Win5SeasonSnapshot

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", Win5SeasonAuditType(self.type))
        if self.type == Win5SeasonAuditType.CREATED:
            if self.before is not None or self.after.status != Win5SeasonStatus.DRAFT:
                raise ValueError("Season creation audit must create one draft from no before state.")
            return
        if self.before is None or self.before.id != self.after.id or self.before.rounds != self.after.rounds:
            raise ValueError("Season mutation audit must preserve identity and captured Round counts.")
        if self.type == Win5SeasonAuditType.METADATA_UPDATED:
            if self.before.status != self.after.status:
                raise ValueError("Season metadata update must preserve status.")
            if (self.before.name, self.before.starts_at, self.before.ends_at) == (
                self.after.name,
                self.after.starts_at,
                self.after.ends_at,
            ):
                raise ValueError("Season metadata audit must describe an actual change.")
            return
        expected = {
            Win5SeasonAuditType.ACTIVATED: (Win5SeasonStatus.DRAFT, Win5SeasonStatus.ACTIVE),
            Win5SeasonAuditType.CLOSED: (Win5SeasonStatus.ACTIVE, Win5SeasonStatus.CLOSED),
            Win5SeasonAuditType.CANCELLED: (Win5SeasonStatus.DRAFT, Win5SeasonStatus.CANCELLED),
        }[self.type]
        if (self.before.status, self.after.status) != expected:
            raise ValueError("Season transition audit statuses do not match its operation type.")
        if (self.before.name, self.before.starts_at, self.before.ends_at) != (
            self.after.name,
            self.after.starts_at,
            self.after.ends_at,
        ):
            raise ValueError("Season status transition must preserve metadata.")

    @property
    def before_data(self) -> dict[str, object] | None:
        return None if self.before is None else self.before.to_audit_payload()

    @property
    def after_data(self) -> dict[str, object]:
        return self.after.to_audit_payload()


@dataclass(frozen=True, slots=True)
class StoredWin5SeasonOperation:
    """Minimal operation data used for exact retry resolution."""

    request_fingerprint: str | None
    type: str | None
    season_id: int | None
    after_data: Mapping[str, object] | None


class Win5SeasonLifecycleRepository(Protocol):
    """Persistence operations required by Season management commands."""

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SeasonOperation | None: ...

    def lock_season(self, *, season_id: int) -> Win5SeasonSnapshot | None: ...

    def lock_round_statuses(self, *, season_id: int) -> tuple[Win5RoundStatus, ...]: ...

    def create_draft(self, *, command: CreateWin5Season, created_at: datetime) -> Win5SeasonSnapshot: ...

    def update_status(
        self,
        *,
        season_id: int,
        expected_status: Win5SeasonStatus,
        target_status: Win5SeasonStatus,
        changed_at: datetime,
    ) -> None: ...

    def update_metadata(self, *, command: UpdateWin5SeasonMetadata, changed_at: datetime) -> None: ...

    def add_audit(
        self,
        *,
        command: CreateWin5Season | TransitionWin5Season | UpdateWin5SeasonMetadata,
        record: Win5SeasonAuditRecord,
        created_at: datetime,
    ) -> None: ...


class Win5SeasonLifecycleUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only Season lifecycle persistence."""

    @property
    def win5_season_lifecycle(self) -> Win5SeasonLifecycleRepository: ...


@dataclass(frozen=True, slots=True)
class Win5SeasonLifecycleCommands:
    """Application entry point for atomic Season management mutations."""

    command_runner: CommandRunner[Win5SeasonLifecycleUnitOfWork]
    clock: Callable[[], datetime]

    def create_season(self, command: CreateWin5Season) -> ChangedWin5Season:
        return self.command_runner.run(lambda uow: self._create(uow.win5_season_lifecycle, command))

    def transition_season(self, command: TransitionWin5Season) -> ChangedWin5Season:
        return self.command_runner.run(lambda uow: self._transition(uow.win5_season_lifecycle, command))

    def update_metadata(self, command: UpdateWin5SeasonMetadata) -> ChangedWin5Season:
        return self.command_runner.run(lambda uow: self._update_metadata(uow.win5_season_lifecycle, command))

    def _create(self, repository: Win5SeasonLifecycleRepository, command: CreateWin5Season) -> ChangedWin5Season:
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)
        changed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        snapshot = repository.create_draft(command=command, created_at=changed_at)
        record = Win5SeasonAuditRecord(type=Win5SeasonAuditType.CREATED, before=None, after=snapshot)
        repository.add_audit(command=command, record=record, created_at=changed_at)
        return ChangedWin5Season(snapshot=snapshot, operation_type=record.type)

    def _transition(
        self,
        repository: Win5SeasonLifecycleRepository,
        command: TransitionWin5Season,
    ) -> ChangedWin5Season:
        try:
            current = repository.lock_season(season_id=command.season_id)
        except (TypeError, ValueError) as exc:
            raise Win5SeasonLifecycleInvalidSourceError("Stored WIN5 Season state is malformed.") from exc
        if current is None:
            raise Win5SeasonLifecycleUnavailableError("WIN5 Season does not exist.")
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)
        expected, target, operation_type = _transition_contract(command.action)
        if current.status != expected:
            raise Win5SeasonLifecycleUnavailableError(f"WIN5 Season must be {expected.value} before {target.value}.")
        try:
            statuses = repository.lock_round_statuses(season_id=current.id)
            rounds = Win5SeasonRoundState.from_statuses(statuses)
        except (TypeError, ValueError) as exc:
            raise Win5SeasonLifecycleInvalidSourceError("Stored WIN5 Round status is malformed.") from exc
        self._validate_transition_rounds(action=command.action, rounds=rounds)
        before = Win5SeasonSnapshot(
            id=current.id,
            name=current.name,
            status=current.status,
            starts_at=current.starts_at,
            ends_at=current.ends_at,
            rounds=rounds,
        )
        changed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        repository.update_status(
            season_id=current.id,
            expected_status=expected,
            target_status=target,
            changed_at=changed_at,
        )
        after = Win5SeasonSnapshot(
            id=before.id,
            name=before.name,
            status=target,
            starts_at=before.starts_at,
            ends_at=before.ends_at,
            rounds=rounds,
        )
        record = Win5SeasonAuditRecord(type=operation_type, before=before, after=after)
        repository.add_audit(command=command, record=record, created_at=changed_at)
        return ChangedWin5Season(snapshot=after, operation_type=operation_type)

    def _update_metadata(
        self,
        repository: Win5SeasonLifecycleRepository,
        command: UpdateWin5SeasonMetadata,
    ) -> ChangedWin5Season:
        try:
            current = repository.lock_season(season_id=command.season_id)
        except (TypeError, ValueError) as exc:
            raise Win5SeasonLifecycleInvalidSourceError("Stored WIN5 Season state is malformed.") from exc
        if current is None:
            raise Win5SeasonLifecycleUnavailableError("WIN5 Season does not exist.")
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)
        try:
            rounds = Win5SeasonRoundState.from_statuses(repository.lock_round_statuses(season_id=current.id))
        except (TypeError, ValueError) as exc:
            raise Win5SeasonLifecycleInvalidSourceError("Stored WIN5 Round status is malformed.") from exc
        before = Win5SeasonSnapshot(
            id=current.id,
            name=current.name,
            status=current.status,
            starts_at=current.starts_at,
            ends_at=current.ends_at,
            rounds=rounds,
        )
        if (current.name, current.starts_at, current.ends_at) == (
            command.name,
            command.starts_at,
            command.ends_at,
        ):
            return ChangedWin5Season(snapshot=before, operation_type=None, changed=False)
        changed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        repository.update_metadata(command=command, changed_at=changed_at)
        after = Win5SeasonSnapshot(
            id=before.id,
            name=command.name,
            status=before.status,
            starts_at=command.starts_at,
            ends_at=command.ends_at,
            rounds=rounds,
        )
        record = Win5SeasonAuditRecord(
            type=Win5SeasonAuditType.METADATA_UPDATED,
            before=before,
            after=after,
        )
        repository.add_audit(command=command, record=record, created_at=changed_at)
        return ChangedWin5Season(snapshot=after, operation_type=record.type)

    @staticmethod
    def _validate_transition_rounds(*, action: Win5SeasonAction, rounds: Win5SeasonRoundState) -> None:
        if action == Win5SeasonAction.ACTIVATE and rounds.total != rounds.setup:
            raise Win5SeasonLifecycleUnavailableError("Season activation requires every child Round to be setup.")
        if action == Win5SeasonAction.CLOSE and rounds.total != rounds.scored + rounds.cancelled:
            raise Win5SeasonLifecycleUnavailableError(
                "Season close requires every child Round to be scored or cancelled."
            )
        if action == Win5SeasonAction.CANCEL and rounds.total != 0:
            raise Win5SeasonLifecycleUnavailableError("Draft Season cancellation requires zero child Rounds.")

    @classmethod
    def _resolve_exact_retry(
        cls,
        *,
        stored: StoredWin5SeasonOperation,
        command: CreateWin5Season | TransitionWin5Season | UpdateWin5SeasonMetadata,
    ) -> ChangedWin5Season:
        expected_type = _command_audit_type(command)
        try:
            operation_type = Win5SeasonAuditType(stored.type)
        except (TypeError, ValueError) as exc:
            raise Win5SeasonLifecycleIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from exc
        expected_season_id = None if isinstance(command, CreateWin5Season) else command.season_id
        if (
            operation_type != expected_type
            or stored.request_fingerprint != command.request_fingerprint
            or (expected_season_id is not None and stored.season_id != expected_season_id)
        ):
            raise Win5SeasonLifecycleIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise Win5SeasonLifecycleAuditError("Exact-retry operation has no stored Season payload.")
        try:
            snapshot = Win5SeasonSnapshot.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise Win5SeasonLifecycleAuditError("Exact-retry Season payload is malformed.") from exc
        if snapshot.id != stored.season_id or (expected_season_id is not None and snapshot.id != expected_season_id):
            raise Win5SeasonLifecycleAuditError("Exact-retry Season payload does not match its operation context.")
        return ChangedWin5Season(snapshot=snapshot, operation_type=operation_type)


def _transition_contract(
    action: Win5SeasonAction,
) -> tuple[Win5SeasonStatus, Win5SeasonStatus, Win5SeasonAuditType]:
    return {
        Win5SeasonAction.ACTIVATE: (
            Win5SeasonStatus.DRAFT,
            Win5SeasonStatus.ACTIVE,
            Win5SeasonAuditType.ACTIVATED,
        ),
        Win5SeasonAction.CLOSE: (
            Win5SeasonStatus.ACTIVE,
            Win5SeasonStatus.CLOSED,
            Win5SeasonAuditType.CLOSED,
        ),
        Win5SeasonAction.CANCEL: (
            Win5SeasonStatus.DRAFT,
            Win5SeasonStatus.CANCELLED,
            Win5SeasonAuditType.CANCELLED,
        ),
    }[action]


def _command_audit_type(
    command: CreateWin5Season | TransitionWin5Season | UpdateWin5SeasonMetadata,
) -> Win5SeasonAuditType:
    if isinstance(command, CreateWin5Season):
        return Win5SeasonAuditType.CREATED
    if isinstance(command, UpdateWin5SeasonMetadata):
        return Win5SeasonAuditType.METADATA_UPDATED
    return _transition_contract(command.action)[2]
