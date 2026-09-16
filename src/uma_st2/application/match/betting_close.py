"""Native V2 Match betting-close command boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.application.publication import PublicationIntent
from uma_st2.application.publication.match import MatchPublicationDestination
from uma_st2.application.publication.match_close import (
    MatchBettingClosePublicationSource,
    MatchBettingCloseSelection,
    build_match_betting_close_publication_intent,
)
from uma_st2.domain.betting import (
    BetPoolStake,
    calculate_provisional_odds,
    quantize_applied_odds,
)
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.domain.publication import PublicationStatus
from uma_st2.shared import normalize_utc_datetime

MATCH_BETTING_CLOSE_AUDIT_SCHEMA_VERSION: Final = 2


class MatchBettingCloseAuditType(StrEnum):
    """Canonical operation type for the close transition."""

    CLOSED = "match_betting_closed"


class MatchBettingCloseError(ValueError):
    """Base error for rejected betting-close commands."""


class MatchBettingCloseUnavailableError(MatchBettingCloseError):
    """The target is absent or not a native betting-open Match."""


class MatchBettingCloseInvalidSourceError(MatchBettingCloseError):
    """Persisted Match or active Bet aggregate facts are malformed."""


class MatchBettingCloseIdempotencyConflictError(MatchBettingCloseError):
    """An idempotency key is bound to another logical command."""


class MatchBettingCloseAuditError(MatchBettingCloseError):
    """Stored exact-retry evidence is absent or malformed."""


def _require_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _require_non_negative_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


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


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


def _payload_positive_int(payload: Mapping[str, object], key: str) -> int:
    return _require_positive_int(payload[key], field_name=key)  # type: ignore[arg-type]


def _payload_non_negative_int(payload: Mapping[str, object], key: str) -> int:
    return _require_non_negative_int(payload[key], field_name=key)  # type: ignore[arg-type]


def _payload_optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload[key]
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null.")
    return value


@dataclass(frozen=True, slots=True)
class MatchBettingCloseEntry:
    """Internal Entry identity mapped to its public operator number."""

    entry_id: int
    entry_number: int

    def __post_init__(self) -> None:
        _require_positive_int(self.entry_id, field_name="entry_id")
        _require_positive_int(self.entry_number, field_name="entry_number")


@dataclass(frozen=True, slots=True)
class MatchBettingCloseTarget:
    """Closed-session Match, pool, and destination authority for final close."""

    match_id: int
    match_name: str
    source_kind: MatchSourceKind
    status: MatchStatus
    grade: MatchGrade
    scheduled_at: datetime
    entry_count: int
    active_bet_count: int
    active_stake_total: int
    entries: tuple[MatchBettingCloseEntry, ...]
    active_bets: tuple[BetPoolStake, ...]
    destination: MatchPublicationDestination

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchStatus(self.status))
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        _require_positive_int(self.entry_count, field_name="entry_count")
        _require_non_negative_int(self.active_bet_count, field_name="active_bet_count")
        _require_non_negative_int(self.active_stake_total, field_name="active_stake_total")
        if (self.active_bet_count == 0) != (self.active_stake_total == 0):
            raise ValueError("Active Bet count and stake total must both be zero or both be positive.")
        entries = tuple(sorted(self.entries, key=lambda entry: entry.entry_number))
        if tuple(entry.entry_number for entry in entries) != tuple(range(1, len(entries) + 1)):
            raise ValueError("Betting-close Entries must be contiguous from 1.")
        if len({entry.entry_id for entry in entries}) != len(entries) or len(entries) != self.entry_count:
            raise ValueError("Betting-close Entry identity/count is inconsistent.")
        object.__setattr__(self, "entries", entries)
        active_bets = tuple(self.active_bets)
        if any(not isinstance(bet, BetPoolStake) for bet in active_bets):
            raise ValueError("active_bets must contain BetPoolStake values.")
        if (
            len(active_bets) != self.active_bet_count
            or sum(bet.amount for bet in active_bets) != self.active_stake_total
        ):
            raise ValueError("Betting-close active Bet aggregate is inconsistent.")
        object.__setattr__(self, "active_bets", active_bets)
        if not isinstance(self.destination, MatchPublicationDestination):
            raise ValueError("destination must be MatchPublicationDestination.")

    def to_publication_source(self, *, closed_at: datetime) -> MatchBettingClosePublicationSource:
        """Calculate every final applied selection from the locked immutable pool."""

        number_by_id = {entry.entry_id: entry.entry_number for entry in self.entries}
        odds = calculate_provisional_odds(tuple(number_by_id), self.active_bets)
        return MatchBettingClosePublicationSource(
            destination=self.destination,
            match_id=self.match_id,
            match_name=self.match_name,
            grade=self.grade,
            scheduled_at=self.scheduled_at,
            closed_at=closed_at,
            selections=tuple(
                MatchBettingCloseSelection(
                    bet_type=item.bet_type,
                    entry_numbers=tuple(number_by_id[entry_id] for entry_id in item.selection_ids),
                    confirmed_odds=quantize_applied_odds(item.odds, field_size=len(self.entries)),
                )
                for item in odds
            ),
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_BETTING_CLOSE_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "source_kind": self.source_kind.value,
            "status": self.status.value,
            "grade": self.grade.value,
            "scheduled_at": self.scheduled_at.isoformat(),
            "entry_count": self.entry_count,
            "active_bet_count": self.active_bet_count,
            "active_stake_total": self.active_stake_total,
        }


@dataclass(frozen=True, slots=True)
class StoredMatchBettingClosePublication:
    """Durable final-odds publication inserted by the close UoW."""

    publication_id: int
    event_key: str
    payload_fingerprint: str
    status: PublicationStatus
    target_channel_id: str | None

    def __post_init__(self) -> None:
        _require_positive_int(self.publication_id, field_name="publication_id")
        object.__setattr__(
            self,
            "event_key",
            _normalized_string(self.event_key, field_name="event_key", max_length=128),
        )
        if len(self.payload_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.payload_fingerprint
        ):
            raise ValueError("payload_fingerprint must be a lowercase SHA-256 digest.")
        object.__setattr__(self, "status", PublicationStatus(self.status))
        object.__setattr__(
            self,
            "target_channel_id",
            _normalized_string(
                self.target_channel_id,
                field_name="target_channel_id",
                max_length=32,
                optional=True,
            ),
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "event_key": self.event_key,
            "payload_fingerprint": self.payload_fingerprint,
            "status": self.status.value,
            "target_channel_id": self.target_channel_id,
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> StoredMatchBettingClosePublication:
        return cls(
            publication_id=_payload_positive_int(payload, "publication_id"),
            event_key=_payload_string(payload, "event_key"),
            payload_fingerprint=_payload_string(payload, "payload_fingerprint"),
            status=PublicationStatus(_payload_string(payload, "status")),
            target_channel_id=_payload_optional_string(payload, "target_channel_id"),
        )


@dataclass(frozen=True, slots=True)
class ClosedMatchBetting:
    """Committed close receipt returned by save or exact retry."""

    match_id: int
    match_name: str
    status: MatchStatus
    closed_at: datetime
    entry_count: int
    active_bet_count: int
    active_stake_total: int
    publication: StoredMatchBettingClosePublication

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "status", MatchStatus(self.status))
        if self.status != MatchStatus.BETTING_CLOSED:
            raise ValueError("A closed Match receipt must have betting_closed status.")
        object.__setattr__(
            self,
            "closed_at",
            normalize_utc_datetime(self.closed_at, field_name="closed_at"),
        )
        _require_positive_int(self.entry_count, field_name="entry_count")
        _require_non_negative_int(self.active_bet_count, field_name="active_bet_count")
        _require_non_negative_int(self.active_stake_total, field_name="active_stake_total")
        if (self.active_bet_count == 0) != (self.active_stake_total == 0):
            raise ValueError("Active Bet count and stake total must both be zero or both be positive.")
        if not isinstance(self.publication, StoredMatchBettingClosePublication):
            raise ValueError("publication must be StoredMatchBettingClosePublication.")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_BETTING_CLOSE_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "status": self.status.value,
            "closed_at": self.closed_at.isoformat(),
            "entry_count": self.entry_count,
            "active_bet_count": self.active_bet_count,
            "active_stake_total": self.active_stake_total,
            "publication": self.publication.to_audit_payload(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> ClosedMatchBetting:
        if payload.get("schema_version") != MATCH_BETTING_CLOSE_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Match betting-close audit schema version.")
        raw_publication = payload["publication"]
        if not isinstance(raw_publication, Mapping):
            raise ValueError("publication must be an object.")
        return cls(
            match_id=_payload_positive_int(payload, "match_id"),
            match_name=_payload_string(payload, "match_name"),
            status=MatchStatus(_payload_string(payload, "status")),
            closed_at=datetime.fromisoformat(_payload_string(payload, "closed_at")),
            entry_count=_payload_positive_int(payload, "entry_count"),
            active_bet_count=_payload_non_negative_int(payload, "active_bet_count"),
            active_stake_total=_payload_non_negative_int(payload, "active_stake_total"),
            publication=StoredMatchBettingClosePublication.from_audit_payload(raw_publication),
        )


@dataclass(frozen=True, slots=True)
class CloseMatchBetting:
    """Final-confirm command for one current native betting-open Match."""

    match_id: int
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        for field_name, max_length, optional in (
            ("idempotency_key", 128, False),
            ("actor_discord_user_id", 32, False),
            ("guild_id", 32, False),
            ("correlation_id", 128, True),
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
        return _fingerprint(
            {
                "schema": "match-betting-close-command-v1",
                "match_id": self.match_id,
                "guild_id": self.guild_id,
            }
        )


@dataclass(frozen=True, slots=True)
class StoredMatchBettingCloseOperation:
    """Minimal operation record used to resolve exact retry."""

    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    after_data: Mapping[str, object] | None


class MatchBettingCloseRepository(Protocol):
    """Persistence operations for one atomic close transition."""

    def lock_target(self, *, match_id: int, guild_id: str) -> MatchBettingCloseTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredMatchBettingCloseOperation | None: ...

    def transition_to_betting_closed(self, *, match_id: int, changed_at: datetime) -> None: ...

    def add_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> StoredMatchBettingClosePublication: ...

    def add_audit(
        self,
        *,
        command: CloseMatchBetting,
        before: MatchBettingCloseTarget,
        after: ClosedMatchBetting,
        created_at: datetime,
    ) -> None: ...


class MatchBettingCloseUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the close command repository."""

    @property
    def match_betting_close(self) -> MatchBettingCloseRepository: ...


@dataclass(frozen=True, slots=True)
class MatchBettingCloseCommands:
    """Application entry point for native betting_open -> betting_closed."""

    command_runner: CommandRunner[MatchBettingCloseUnitOfWork]
    clock: Callable[[], datetime]

    def close_betting(self, command: CloseMatchBetting) -> ClosedMatchBetting:
        return self.command_runner.run(lambda unit_of_work: self._close(unit_of_work.match_betting_close, command))

    def _close(
        self,
        repository: MatchBettingCloseRepository,
        command: CloseMatchBetting,
    ) -> ClosedMatchBetting:
        try:
            target = repository.lock_target(match_id=command.match_id, guild_id=command.guild_id)
        except (TypeError, ValueError) as exc:
            raise MatchBettingCloseInvalidSourceError("Stored Match close facts are malformed.") from exc
        if target is None:
            raise MatchBettingCloseUnavailableError("Match does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if target.source_kind != MatchSourceKind.NATIVE_V2 or target.status != MatchStatus.BETTING_OPEN:
            raise MatchBettingCloseUnavailableError("Betting close requires a native betting-open Match.")
        if target.destination.guild_id != command.guild_id:
            raise MatchBettingCloseInvalidSourceError("Match close destination belongs to another guild.")

        closed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        try:
            source = target.to_publication_source(closed_at=closed_at)
            intent = build_match_betting_close_publication_intent(source)
            repository.transition_to_betting_closed(match_id=target.match_id, changed_at=closed_at)
            publication = repository.add_publication(intent=intent, created_at=closed_at)
            after = ClosedMatchBetting(
                match_id=target.match_id,
                match_name=target.match_name,
                status=MatchStatus.BETTING_CLOSED,
                closed_at=closed_at,
                entry_count=target.entry_count,
                active_bet_count=target.active_bet_count,
                active_stake_total=target.active_stake_total,
                publication=publication,
            )
        except MatchBettingCloseError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchBettingCloseInvalidSourceError(
                "Match close did not produce complete canonical evidence."
            ) from exc
        repository.add_audit(command=command, before=target, after=after, created_at=closed_at)
        return after

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredMatchBettingCloseOperation,
        command: CloseMatchBetting,
    ) -> ClosedMatchBetting:
        if (
            stored.type != MatchBettingCloseAuditType.CLOSED.value
            or stored.request_fingerprint != command.request_fingerprint
            or stored.match_id != command.match_id
        ):
            raise MatchBettingCloseIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise MatchBettingCloseAuditError("Exact-retry operation has no stored after snapshot.")
        try:
            closed = ClosedMatchBetting.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchBettingCloseAuditError("Exact-retry operation has malformed stored evidence.") from exc
        if closed.match_id != command.match_id:
            raise MatchBettingCloseAuditError("Exact-retry snapshot belongs to another Match.")
        return closed
