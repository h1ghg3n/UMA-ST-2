"""WIN5 Round open/close application boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.win5 import (
    Win5DomainError,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    validate_win5_round_open_readiness,
)
from uma_st2.shared import normalize_utc_datetime

MAX_OPEN_WIN5_ROUNDS_PER_ACTIVE_SEASON: Final = 25
WIN5_ROUND_LIFECYCLE_AUDIT_SCHEMA_VERSION: Final = 1


class Win5RoundLifecycleAction(StrEnum):
    """Supported staff lifecycle transitions for one Round."""

    OPEN = "open"
    CLOSE = "close"


class Win5RoundLifecycleAuditType(StrEnum):
    """Canonical operation types for Round lifecycle mutations."""

    OPENED = "round_opened"
    CLOSED = "round_closed"


class Win5RoundLifecycleError(ValueError):
    """Base error for rejected Round lifecycle commands."""


class Win5RoundLifecycleUnavailableError(Win5RoundLifecycleError):
    """The target Season/Round no longer permits the requested transition."""


class Win5RoundLifecycleInvalidSourceError(Win5RoundLifecycleError):
    """Stored Round graph facts cannot safely enter the requested state."""


class Win5RoundOpenLimitError(Win5RoundLifecycleError):
    """The active Season already owns the maximum open Round count."""


class Win5RoundLifecycleIdempotencyConflictError(Win5RoundLifecycleError):
    """An idempotency key is bound to another logical command."""


class Win5RoundLifecycleAuditError(Win5RoundLifecycleError):
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
class TransitionWin5Round:
    """Open or close one WIN5 Round in one command UoW."""

    round_id: int
    action: Win5RoundLifecycleAction
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str | None = None
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.round_id, field_name="round_id")
        object.__setattr__(self, "action", Win5RoundLifecycleAction(self.action))
        _require_bounded_string(self.idempotency_key, field_name="idempotency_key", max_length=128)
        _require_bounded_string(
            self.actor_discord_user_id,
            field_name="actor_discord_user_id",
            max_length=32,
        )
        _require_bounded_string(self.guild_id, field_name="guild_id", max_length=32, optional=True)
        _require_bounded_string(
            self.correlation_id,
            field_name="correlation_id",
            max_length=128,
            optional=True,
        )
        _require_bounded_string(self.reason, field_name="reason", max_length=255, optional=True)

    @property
    def request_fingerprint(self) -> str:
        canonical = "\n".join(
            (
                "win5-round-lifecycle-command-v1",
                f"action:{self.action.value}",
                f"round:{self.round_id}",
            )
        )
        return sha256(canonical.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class Win5RoundLifecycleSeason:
    """Parent Season state used by a lifecycle command."""

    id: int
    name: str
    status: Win5SeasonStatus

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_bounded_string(self.name, field_name="name", max_length=100)
        object.__setattr__(self, "status", Win5SeasonStatus(self.status))


@dataclass(frozen=True, slots=True)
class Win5RoundLifecycleRoundTarget:
    """Locked Round state used by a lifecycle command."""

    id: int
    season_id: int
    name: str
    type: Win5RoundType
    source_kind: Win5RoundSourceKind
    status: Win5RoundStatus

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.season_id, field_name="season_id")
        _require_bounded_string(self.name, field_name="name", max_length=100)
        object.__setattr__(self, "type", Win5RoundType(self.type))
        object.__setattr__(self, "status", Win5RoundStatus(self.status))
        object.__setattr__(self, "source_kind", Win5RoundSourceKind(self.source_kind))


@dataclass(frozen=True, slots=True)
class Win5RoundGraphState:
    """Bounded aggregate counts required to validate Round opening."""

    race_count: int
    race_entry_count: int
    result_count: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.race_count, field_name="race_count")
        _require_non_negative_int(self.race_entry_count, field_name="race_entry_count")
        _require_non_negative_int(self.result_count, field_name="result_count")


@dataclass(frozen=True, slots=True)
class Win5RoundLifecycleSnapshot:
    """Deterministic before/after lifecycle audit snapshot."""

    season_id: int
    season_name: str
    round_id: int
    round_name: str
    round_type: Win5RoundType
    status: Win5RoundStatus

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_bounded_string(self.season_name, field_name="season_name", max_length=100)
        _require_bounded_string(self.round_name, field_name="round_name", max_length=100)
        object.__setattr__(self, "round_type", Win5RoundType(self.round_type))
        object.__setattr__(self, "status", Win5RoundStatus(self.status))

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": WIN5_ROUND_LIFECYCLE_AUDIT_SCHEMA_VERSION,
            "season_id": self.season_id,
            "season_name": self.season_name,
            "round_id": self.round_id,
            "round_name": self.round_name,
            "round_type": self.round_type.value,
            "round_status": self.status.value,
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> Win5RoundLifecycleSnapshot:
        if _payload_positive_int(payload, "schema_version") != WIN5_ROUND_LIFECYCLE_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported WIN5 Round lifecycle audit schema version.")
        return cls(
            season_id=_payload_positive_int(payload, "season_id"),
            season_name=_payload_string(payload, "season_name"),
            round_id=_payload_positive_int(payload, "round_id"),
            round_name=_payload_string(payload, "round_name"),
            round_type=Win5RoundType(_payload_string(payload, "round_type")),
            status=Win5RoundStatus(_payload_string(payload, "round_status")),
        )


@dataclass(frozen=True, slots=True)
class TransitionedWin5Round:
    """Closed-session lifecycle result returned after commit or exact retry."""

    season_id: int
    season_name: str
    round_id: int
    round_name: str
    round_type: Win5RoundType
    status: Win5RoundStatus
    operation_type: Win5RoundLifecycleAuditType

    def __post_init__(self) -> None:
        snapshot = self.snapshot
        object.__setattr__(self, "season_id", snapshot.season_id)
        object.__setattr__(self, "season_name", snapshot.season_name)
        object.__setattr__(self, "round_id", snapshot.round_id)
        object.__setattr__(self, "round_name", snapshot.round_name)
        object.__setattr__(self, "round_type", snapshot.round_type)
        object.__setattr__(self, "status", snapshot.status)
        object.__setattr__(self, "operation_type", Win5RoundLifecycleAuditType(self.operation_type))

    @property
    def snapshot(self) -> Win5RoundLifecycleSnapshot:
        return Win5RoundLifecycleSnapshot(
            season_id=self.season_id,
            season_name=self.season_name,
            round_id=self.round_id,
            round_name=self.round_name,
            round_type=self.round_type,
            status=self.status,
        )


@dataclass(frozen=True, slots=True)
class Win5RoundLifecycleAuditRecord:
    """One state-changing Round transition audit prepared by Application."""

    type: Win5RoundLifecycleAuditType
    before: Win5RoundLifecycleSnapshot
    after: Win5RoundLifecycleSnapshot

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", Win5RoundLifecycleAuditType(self.type))
        if (
            self.before.season_id,
            self.before.season_name,
            self.before.round_id,
            self.before.round_name,
            self.before.round_type,
        ) != (
            self.after.season_id,
            self.after.season_name,
            self.after.round_id,
            self.after.round_name,
            self.after.round_type,
        ):
            raise ValueError("Round lifecycle transition may change only status.")
        expected = {
            Win5RoundLifecycleAuditType.OPENED: (Win5RoundStatus.SETUP, Win5RoundStatus.OPEN),
            Win5RoundLifecycleAuditType.CLOSED: (Win5RoundStatus.OPEN, Win5RoundStatus.CLOSED),
        }[self.type]
        if (self.before.status, self.after.status) != expected:
            raise ValueError("Round lifecycle audit statuses do not match the operation type.")

    @property
    def before_data(self) -> dict[str, object]:
        return self.before.to_audit_payload()

    @property
    def after_data(self) -> dict[str, object]:
        return self.after.to_audit_payload()


@dataclass(frozen=True, slots=True)
class StoredWin5RoundLifecycleOperation:
    """Minimal persisted operation used to resolve an exact retry."""

    request_fingerprint: str | None
    type: str | None
    season_id: int | None
    round_id: int | None
    after_data: Mapping[str, object] | None


class Win5RoundLifecycleRepository(Protocol):
    """Persistence operations required by Round open/close commands."""

    def find_round_season_id(self, *, round_id: int) -> int | None: ...

    def lock_season(self, *, season_id: int) -> Win5RoundLifecycleSeason | None: ...

    def get_season(self, *, season_id: int) -> Win5RoundLifecycleSeason | None: ...

    def lock_round(self, *, round_id: int) -> Win5RoundLifecycleRoundTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredWin5RoundLifecycleOperation | None: ...

    def load_round_graph_state(self, *, round_id: int) -> Win5RoundGraphState: ...

    def count_open_rounds(self, *, season_id: int) -> int: ...

    def update_round_status(
        self,
        *,
        round_id: int,
        expected_status: Win5RoundStatus,
        target_status: Win5RoundStatus,
        changed_at: datetime,
    ) -> None: ...

    def add_lifecycle_audit(
        self,
        *,
        command: TransitionWin5Round,
        record: Win5RoundLifecycleAuditRecord,
        created_at: datetime,
    ) -> None: ...


class Win5RoundLifecycleUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the Round lifecycle repository."""

    @property
    def win5_round_lifecycle(self) -> Win5RoundLifecycleRepository: ...


@dataclass(frozen=True, slots=True)
class Win5RoundLifecycleCommands:
    """Application entry point for atomic Round open/close transitions."""

    command_runner: CommandRunner[Win5RoundLifecycleUnitOfWork]
    clock: Callable[[], datetime]

    def transition_round(self, command: TransitionWin5Round) -> TransitionedWin5Round:
        return self.command_runner.run(
            lambda unit_of_work: self._transition_round(unit_of_work.win5_round_lifecycle, command)
        )

    def _transition_round(
        self,
        repository: Win5RoundLifecycleRepository,
        command: TransitionWin5Round,
    ) -> TransitionedWin5Round:
        if command.action == Win5RoundLifecycleAction.OPEN:
            season, round_ = self._lock_open_target(repository, round_id=command.round_id)
        else:
            round_ = repository.lock_round(round_id=command.round_id)
            if round_ is None:
                raise Win5RoundLifecycleUnavailableError("WIN5 Round does not exist.")
            season = repository.get_season(season_id=round_.season_id)
            if season is None:
                raise Win5RoundLifecycleInvalidSourceError("WIN5 Round references a missing Season.")

        if round_.source_kind != Win5RoundSourceKind.NATIVE_V2:
            raise Win5RoundLifecycleUnavailableError("Imported WIN5 Rounds are read-only.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        expected_status, target_status, operation_type = self._transition_contract(command.action)
        if round_.status != expected_status:
            raise Win5RoundLifecycleUnavailableError(
                f"WIN5 Round must be {expected_status.value} before {target_status.value}."
            )

        if command.action == Win5RoundLifecycleAction.OPEN:
            if season.status != Win5SeasonStatus.ACTIVE:
                raise Win5RoundLifecycleUnavailableError("Only an active WIN5 Season may open a Round.")
            graph = repository.load_round_graph_state(round_id=round_.id)
            try:
                validate_win5_round_open_readiness(
                    round_type=round_.type,
                    race_count=graph.race_count,
                    race_entry_count=graph.race_entry_count,
                    result_count=graph.result_count,
                )
            except Win5DomainError as exc:
                raise Win5RoundLifecycleInvalidSourceError(str(exc)) from exc
            if repository.count_open_rounds(season_id=season.id) >= MAX_OPEN_WIN5_ROUNDS_PER_ACTIVE_SEASON:
                raise Win5RoundOpenLimitError("The active WIN5 Season already has 25 open Rounds.")

        changed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        before = self._snapshot(season=season, round_=round_, status=round_.status)
        repository.update_round_status(
            round_id=round_.id,
            expected_status=expected_status,
            target_status=target_status,
            changed_at=changed_at,
        )
        after = self._snapshot(season=season, round_=round_, status=target_status)
        repository.add_lifecycle_audit(
            command=command,
            record=Win5RoundLifecycleAuditRecord(
                type=operation_type,
                before=before,
                after=after,
            ),
            created_at=changed_at,
        )
        return TransitionedWin5Round(
            season_id=after.season_id,
            season_name=after.season_name,
            round_id=after.round_id,
            round_name=after.round_name,
            round_type=after.round_type,
            status=after.status,
            operation_type=operation_type,
        )

    @staticmethod
    def _lock_open_target(
        repository: Win5RoundLifecycleRepository,
        *,
        round_id: int,
    ) -> tuple[Win5RoundLifecycleSeason, Win5RoundLifecycleRoundTarget]:
        season_id = repository.find_round_season_id(round_id=round_id)
        if season_id is None:
            raise Win5RoundLifecycleUnavailableError("WIN5 Round does not exist.")
        season = repository.lock_season(season_id=season_id)
        if season is None:
            raise Win5RoundLifecycleInvalidSourceError("WIN5 Round references a missing Season.")
        round_ = repository.lock_round(round_id=round_id)
        if round_ is None:
            raise Win5RoundLifecycleUnavailableError("WIN5 Round no longer exists.")
        if round_.season_id != season.id:
            raise Win5RoundLifecycleInvalidSourceError("WIN5 Round changed parent Season during opening.")
        return season, round_

    @staticmethod
    def _transition_contract(
        action: Win5RoundLifecycleAction,
    ) -> tuple[Win5RoundStatus, Win5RoundStatus, Win5RoundLifecycleAuditType]:
        if action == Win5RoundLifecycleAction.OPEN:
            return Win5RoundStatus.SETUP, Win5RoundStatus.OPEN, Win5RoundLifecycleAuditType.OPENED
        return Win5RoundStatus.OPEN, Win5RoundStatus.CLOSED, Win5RoundLifecycleAuditType.CLOSED

    @staticmethod
    def _snapshot(
        *,
        season: Win5RoundLifecycleSeason,
        round_: Win5RoundLifecycleRoundTarget,
        status: Win5RoundStatus,
    ) -> Win5RoundLifecycleSnapshot:
        return Win5RoundLifecycleSnapshot(
            season_id=season.id,
            season_name=season.name,
            round_id=round_.id,
            round_name=round_.name,
            round_type=round_.type,
            status=status,
        )

    @classmethod
    def _resolve_exact_retry(
        cls,
        *,
        stored: StoredWin5RoundLifecycleOperation,
        command: TransitionWin5Round,
    ) -> TransitionedWin5Round:
        expected_type = cls._transition_contract(command.action)[2]
        try:
            operation_type = Win5RoundLifecycleAuditType(stored.type)
        except (TypeError, ValueError) as exc:
            raise Win5RoundLifecycleIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from exc
        if (
            operation_type != expected_type
            or stored.request_fingerprint != command.request_fingerprint
            or stored.round_id != command.round_id
        ):
            raise Win5RoundLifecycleIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise Win5RoundLifecycleAuditError("Exact-retry operation has no stored lifecycle payload.")
        try:
            snapshot = Win5RoundLifecycleSnapshot.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise Win5RoundLifecycleAuditError("Exact-retry lifecycle payload is malformed.") from exc
        target_status = cls._transition_contract(command.action)[1]
        if (
            snapshot.season_id != stored.season_id
            or snapshot.round_id != command.round_id
            or snapshot.status != target_status
        ):
            raise Win5RoundLifecycleAuditError("Exact-retry lifecycle payload does not match its operation context.")
        return TransitionedWin5Round(
            season_id=snapshot.season_id,
            season_name=snapshot.season_name,
            round_id=snapshot.round_id,
            round_name=snapshot.round_name,
            round_type=snapshot.round_type,
            status=snapshot.status,
            operation_type=operation_type,
        )
