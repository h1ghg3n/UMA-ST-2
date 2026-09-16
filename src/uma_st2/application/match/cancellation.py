"""Native V2 whole-Match cancellation and active-Bet refund boundary."""

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
from uma_st2.application.publication.match import (
    MatchPublicationDestination,
    MatchRefundPublicationSource,
    build_match_refund_publication_intent,
)
from uma_st2.domain.betting import calculate_refund
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.domain.point import calculate_next_circle_point_balance
from uma_st2.domain.publication import PublicationStatus
from uma_st2.shared import normalize_utc_datetime

MATCH_CANCELLATION_AUDIT_SCHEMA_VERSION: Final = 2
_LEGACY_MATCH_CANCELLATION_AUDIT_SCHEMA_VERSION: Final = 1
MATCH_BET_REFUND_POINT_ACTION: Final = "match_bet_refund"
MATCH_CANCELLABLE_STATUSES: Final = frozenset(
    {
        MatchStatus.SCHEDULED,
        MatchStatus.BETTING_OPEN,
        MatchStatus.BETTING_CLOSED,
        MatchStatus.RESULT_CONFIRMED,
    }
)


class MatchCancellationAuditType(StrEnum):
    """Canonical operation type for whole-Match cancellation."""

    CANCELLED = "match_cancelled"


class MatchCancellationError(ValueError):
    """Base error for rejected whole-Match cancellation commands."""


class MatchCancellationUnavailableError(MatchCancellationError):
    """The target is absent or no longer cancellable."""


class MatchCancellationInvalidSourceError(MatchCancellationError):
    """Persisted Match, Bet, wallet, or audit facts are malformed."""


class MatchCancellationWalletUnavailableError(MatchCancellationError):
    """An active Bet owner has no canonical Circle Point wallet."""


class MatchCancellationIdempotencyConflictError(MatchCancellationError):
    """An idempotency key is bound to another logical command."""


class MatchCancellationAuditError(MatchCancellationError):
    """Stored exact-retry evidence is absent or malformed."""


def _require_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _require_non_negative_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


def _require_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer.")
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


def _payload_optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string or null.")
    return value


def _payload_positive_int(payload: Mapping[str, object], key: str) -> int:
    return _require_positive_int(payload[key], field_name=key)  # type: ignore[arg-type]


def _payload_non_negative_int(payload: Mapping[str, object], key: str) -> int:
    return _require_non_negative_int(payload[key], field_name=key)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class MatchCancellationBet:
    """One locked active Bet that must be cancelled and refunded."""

    id: int
    persona_id: str
    amount: int

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        _require_positive_int(self.amount, field_name="amount")

    def to_audit_payload(self) -> dict[str, object]:
        return {"bet_id": self.id, "persona_id": self.persona_id, "amount": self.amount}


@dataclass(frozen=True, slots=True)
class MatchCancellationWallet:
    """One locked wallet owned by an active-Bet Persona."""

    persona_id: str
    balance: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        _require_int(self.balance, field_name="balance")


@dataclass(frozen=True, slots=True)
class MatchCancellationTarget:
    """Locked pre-settlement Match, active Bets, and refund wallets."""

    match_id: int
    match_name: str
    source_kind: MatchSourceKind
    status: MatchStatus
    terminal_reason: str | None
    grade: MatchGrade
    scheduled_at: datetime
    entry_count: int
    active_bets: tuple[MatchCancellationBet, ...]
    wallets: tuple[MatchCancellationWallet, ...]
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
        object.__setattr__(
            self,
            "terminal_reason",
            _normalized_string(
                self.terminal_reason,
                field_name="terminal_reason",
                max_length=255,
                optional=True,
            ),
        )
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        _require_non_negative_int(self.entry_count, field_name="entry_count")

        active_bets = tuple(self.active_bets)
        if any(not isinstance(bet, MatchCancellationBet) for bet in active_bets):
            raise ValueError("active_bets must contain MatchCancellationBet values.")
        if len({bet.id for bet in active_bets}) != len(active_bets):
            raise ValueError("active_bets must have unique IDs.")
        object.__setattr__(self, "active_bets", tuple(sorted(active_bets, key=lambda bet: bet.id)))

        wallets = tuple(self.wallets)
        if any(not isinstance(wallet, MatchCancellationWallet) for wallet in wallets):
            raise ValueError("wallets must contain MatchCancellationWallet values.")
        if len({wallet.persona_id for wallet in wallets}) != len(wallets):
            raise ValueError("wallets must have unique Persona IDs.")
        expected_personas = {bet.persona_id for bet in active_bets}
        if {wallet.persona_id for wallet in wallets} != expected_personas:
            raise ValueError("Every active-Bet Persona must have exactly one locked wallet.")
        object.__setattr__(self, "wallets", tuple(sorted(wallets, key=lambda wallet: wallet.persona_id)))
        if not isinstance(self.destination, MatchPublicationDestination):
            raise ValueError("destination must be MatchPublicationDestination.")

    @property
    def active_bet_count(self) -> int:
        return len(self.active_bets)

    @property
    def refund_total(self) -> int:
        return sum(calculate_refund(bet.amount) for bet in self.active_bets)

    def to_refund_publication_source(
        self,
        *,
        refunded_at: datetime,
        reason: str | None,
    ) -> MatchRefundPublicationSource:
        if not self.active_bets:
            raise ValueError("A zero-refund cancellation cannot produce a publication source.")
        return MatchRefundPublicationSource(
            destination=self.destination,
            match_id=self.match_id,
            match_name=self.match_name,
            grade=self.grade,
            scheduled_at=self.scheduled_at,
            refunded_at=refunded_at,
            reason=reason,
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_CANCELLATION_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "source_kind": self.source_kind.value,
            "status": self.status.value,
            "terminal_reason": self.terminal_reason,
            "grade": self.grade.value,
            "scheduled_at": self.scheduled_at.isoformat(),
            "entry_count": self.entry_count,
            "active_bets": [bet.to_audit_payload() for bet in self.active_bets],
            "wallets": [{"persona_id": wallet.persona_id, "balance": wallet.balance} for wallet in self.wallets],
            "destination": self.destination.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class MatchCancellationRefundPlan:
    """Application-calculated aggregate refund for one Persona."""

    persona_id: str
    bet_ids: tuple[int, ...]
    amount: int
    balance_before: int
    balance_after: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        bet_ids = tuple(self.bet_ids)
        if not bet_ids or any(_require_positive_int(bet_id, field_name="bet_id") != bet_id for bet_id in bet_ids):
            raise ValueError("bet_ids must contain positive integers.")
        if len(set(bet_ids)) != len(bet_ids):
            raise ValueError("bet_ids must be unique.")
        object.__setattr__(self, "bet_ids", tuple(sorted(bet_ids)))
        _require_positive_int(self.amount, field_name="amount")
        _require_int(self.balance_before, field_name="balance_before")
        _require_int(self.balance_after, field_name="balance_after")
        if calculate_next_circle_point_balance(self.balance_before, self.amount) != self.balance_after:
            raise ValueError("balance_after must equal balance_before plus refund amount.")


@dataclass(frozen=True, slots=True)
class MatchCancellationRefund:
    """Committed Persona refund with its immutable Point transaction ID."""

    persona_id: str
    bet_ids: tuple[int, ...]
    amount: int
    balance_before: int
    balance_after: int
    point_transaction_id: int

    def __post_init__(self) -> None:
        MatchCancellationRefundPlan(
            persona_id=self.persona_id,
            bet_ids=self.bet_ids,
            amount=self.amount,
            balance_before=self.balance_before,
            balance_after=self.balance_after,
        )
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        object.__setattr__(self, "bet_ids", tuple(sorted(self.bet_ids)))
        _require_positive_int(self.point_transaction_id, field_name="point_transaction_id")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "persona_id": self.persona_id,
            "bet_ids": list(self.bet_ids),
            "amount": self.amount,
            "balance_before": self.balance_before,
            "balance_after": self.balance_after,
            "point_transaction_id": self.point_transaction_id,
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> MatchCancellationRefund:
        bet_ids = payload["bet_ids"]
        if not isinstance(bet_ids, list):
            raise ValueError("bet_ids must be a list.")
        return cls(
            persona_id=_payload_string(payload, "persona_id"),
            bet_ids=tuple(_require_positive_int(value, field_name="bet_id") for value in bet_ids),  # type: ignore[arg-type]
            amount=_payload_positive_int(payload, "amount"),
            balance_before=_require_int(payload["balance_before"], field_name="balance_before"),  # type: ignore[arg-type]
            balance_after=_require_int(payload["balance_after"], field_name="balance_after"),  # type: ignore[arg-type]
            point_transaction_id=_payload_positive_int(payload, "point_transaction_id"),
        )


@dataclass(frozen=True, slots=True)
class StoredMatchRefundPublication:
    """Durable refund-completion publication inserted by the cancellation UoW."""

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
    def from_audit_payload(cls, payload: Mapping[str, object]) -> StoredMatchRefundPublication:
        return cls(
            publication_id=_payload_positive_int(payload, "publication_id"),
            event_key=_payload_string(payload, "event_key"),
            payload_fingerprint=_payload_string(payload, "payload_fingerprint"),
            status=PublicationStatus(_payload_string(payload, "status")),
            target_channel_id=_payload_optional_string(payload, "target_channel_id"),
        )


@dataclass(frozen=True, slots=True)
class CancelledMatch:
    """Committed whole-Match cancellation receipt."""

    match_id: int
    match_name: str
    previous_status: MatchStatus
    status: MatchStatus
    reason: str | None
    cancelled_at: datetime
    entry_count: int
    cancelled_bet_count: int
    refund_total: int
    refunds: tuple[MatchCancellationRefund, ...]
    publication: StoredMatchRefundPublication | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "previous_status", MatchStatus(self.previous_status))
        if self.previous_status not in MATCH_CANCELLABLE_STATUSES:
            raise ValueError("previous_status must be a cancellable Match status.")
        object.__setattr__(self, "status", MatchStatus(self.status))
        if self.status != MatchStatus.CANCELLED:
            raise ValueError("A cancellation receipt must have cancelled status.")
        object.__setattr__(
            self,
            "reason",
            _normalized_string(self.reason, field_name="reason", max_length=255, optional=True),
        )
        object.__setattr__(
            self,
            "cancelled_at",
            normalize_utc_datetime(self.cancelled_at, field_name="cancelled_at"),
        )
        _require_non_negative_int(self.entry_count, field_name="entry_count")
        _require_non_negative_int(self.cancelled_bet_count, field_name="cancelled_bet_count")
        _require_non_negative_int(self.refund_total, field_name="refund_total")
        refunds = tuple(self.refunds)
        if any(not isinstance(refund, MatchCancellationRefund) for refund in refunds):
            raise ValueError("refunds must contain MatchCancellationRefund values.")
        if len({refund.persona_id for refund in refunds}) != len(refunds):
            raise ValueError("refunds must have unique Persona IDs.")
        if sum(len(refund.bet_ids) for refund in refunds) != self.cancelled_bet_count:
            raise ValueError("Refund Bet count does not match cancelled_bet_count.")
        if sum(refund.amount for refund in refunds) != self.refund_total:
            raise ValueError("Refund amount does not match refund_total.")
        object.__setattr__(self, "refunds", tuple(sorted(refunds, key=lambda refund: refund.persona_id)))
        if self.publication is not None and not isinstance(self.publication, StoredMatchRefundPublication):
            raise ValueError("publication must be StoredMatchRefundPublication or null.")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_CANCELLATION_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "previous_status": self.previous_status.value,
            "status": self.status.value,
            "reason": self.reason,
            "cancelled_at": self.cancelled_at.isoformat(),
            "entry_count": self.entry_count,
            "cancelled_bet_count": self.cancelled_bet_count,
            "refund_total": self.refund_total,
            "refunds": [refund.to_audit_payload() for refund in self.refunds],
            "publication": self.publication.to_audit_payload() if self.publication is not None else None,
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> CancelledMatch:
        schema_version = payload.get("schema_version")
        if schema_version not in {
            _LEGACY_MATCH_CANCELLATION_AUDIT_SCHEMA_VERSION,
            MATCH_CANCELLATION_AUDIT_SCHEMA_VERSION,
        }:
            raise ValueError("Unsupported Match cancellation audit schema version.")
        refunds = payload["refunds"]
        if not isinstance(refunds, list):
            raise ValueError("refunds must be a list.")
        raw_publication = (
            payload.get("publication") if schema_version == MATCH_CANCELLATION_AUDIT_SCHEMA_VERSION else None
        )
        if raw_publication is not None and not isinstance(raw_publication, Mapping):
            raise ValueError("publication must be an object or null.")
        cancelled = cls(
            match_id=_payload_positive_int(payload, "match_id"),
            match_name=_payload_string(payload, "match_name"),
            previous_status=MatchStatus(_payload_string(payload, "previous_status")),
            status=MatchStatus(_payload_string(payload, "status")),
            reason=(
                _payload_optional_string(payload, "reason")
                if schema_version == MATCH_CANCELLATION_AUDIT_SCHEMA_VERSION
                else _payload_string(payload, "reason")
            ),
            cancelled_at=datetime.fromisoformat(_payload_string(payload, "cancelled_at")),
            entry_count=_payload_non_negative_int(payload, "entry_count"),
            cancelled_bet_count=_payload_non_negative_int(payload, "cancelled_bet_count"),
            refund_total=_payload_non_negative_int(payload, "refund_total"),
            refunds=tuple(MatchCancellationRefund.from_audit_payload(refund) for refund in refunds),
            publication=(
                StoredMatchRefundPublication.from_audit_payload(raw_publication)
                if isinstance(raw_publication, Mapping)
                else None
            ),
        )
        if schema_version == MATCH_CANCELLATION_AUDIT_SCHEMA_VERSION and (
            (cancelled.cancelled_bet_count > 0) != (cancelled.publication is not None)
        ):
            raise ValueError("Current cancellation audit publication coverage is inconsistent.")
        return cancelled


@dataclass(frozen=True, slots=True)
class CancelMatch:
    """Final-confirm command for one native pre-settlement Match."""

    match_id: int
    reason: str | None
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        for field_name, max_length, optional in (
            ("reason", 255, True),
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
                "schema": "match-cancellation-command-v1",
                "match_id": self.match_id,
                "reason": self.reason,
                "guild_id": self.guild_id,
            }
        )


@dataclass(frozen=True, slots=True)
class StoredMatchCancellationOperation:
    """Minimal operation record used to resolve exact retry."""

    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    after_data: Mapping[str, object] | None


class MatchCancellationRepository(Protocol):
    """Persistence operations for one atomic terminal cancellation."""

    def lock_target(self, *, match_id: int, guild_id: str) -> MatchCancellationTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredMatchCancellationOperation | None: ...

    def cancel_and_refund(
        self,
        *,
        command: CancelMatch,
        target: MatchCancellationTarget,
        refund_plans: tuple[MatchCancellationRefundPlan, ...],
        publication_intent: PublicationIntent | None,
        cancelled_at: datetime,
    ) -> CancelledMatch: ...


class MatchCancellationUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only whole-Match cancellation."""

    @property
    def match_cancellation(self) -> MatchCancellationRepository: ...


@dataclass(frozen=True, slots=True)
class MatchCancellationCommands:
    """Application entry point for pre-settlement whole-Match cancellation."""

    command_runner: CommandRunner[MatchCancellationUnitOfWork]
    clock: Callable[[], datetime]

    def cancel_match(self, command: CancelMatch) -> CancelledMatch:
        return self.command_runner.run(lambda unit_of_work: self._cancel(unit_of_work.match_cancellation, command))

    def _cancel(
        self,
        repository: MatchCancellationRepository,
        command: CancelMatch,
    ) -> CancelledMatch:
        try:
            target = repository.lock_target(match_id=command.match_id, guild_id=command.guild_id)
        except MatchCancellationWalletUnavailableError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchCancellationInvalidSourceError("Stored Match cancellation facts are malformed.") from exc
        if target is None:
            raise MatchCancellationUnavailableError("Match does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if (
            target.source_kind != MatchSourceKind.NATIVE_V2
            or target.status not in MATCH_CANCELLABLE_STATUSES
            or target.terminal_reason is not None
        ):
            raise MatchCancellationUnavailableError("Cancellation requires a native pre-settlement Match.")

        refund_plans = self._build_refund_plans(target)
        cancelled_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        publication_intent = (
            build_match_refund_publication_intent(
                target.to_refund_publication_source(
                    refunded_at=cancelled_at,
                    reason=command.reason,
                )
            )
            if refund_plans
            else None
        )
        try:
            cancelled = repository.cancel_and_refund(
                command=command,
                target=target,
                refund_plans=refund_plans,
                publication_intent=publication_intent,
                cancelled_at=cancelled_at,
            )
        except MatchCancellationError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchCancellationInvalidSourceError(
                "Match cancellation did not produce complete canonical evidence."
            ) from exc
        if (
            cancelled.match_id != target.match_id
            or cancelled.previous_status != target.status
            or cancelled.reason != command.reason
            or cancelled.cancelled_bet_count != target.active_bet_count
            or cancelled.refund_total != target.refund_total
            or not self._publication_matches_intent(
                publication=cancelled.publication,
                intent=publication_intent,
            )
        ):
            raise MatchCancellationInvalidSourceError("Stored cancellation receipt does not match locked authority.")
        return cancelled

    @staticmethod
    def _publication_matches_intent(
        *,
        publication: StoredMatchRefundPublication | None,
        intent: PublicationIntent | None,
    ) -> bool:
        if intent is None:
            return publication is None
        return (
            publication is not None
            and publication.event_key == intent.event_key
            and publication.payload_fingerprint == intent.payload_fingerprint
            and publication.status == intent.status
            and publication.target_channel_id == intent.target_channel_id
        )

    @staticmethod
    def _build_refund_plans(target: MatchCancellationTarget) -> tuple[MatchCancellationRefundPlan, ...]:
        bets_by_persona: dict[str, list[MatchCancellationBet]] = {}
        for bet in target.active_bets:
            bets_by_persona.setdefault(bet.persona_id, []).append(bet)
        wallets = {wallet.persona_id: wallet for wallet in target.wallets}
        plans: list[MatchCancellationRefundPlan] = []
        for persona_id in sorted(bets_by_persona):
            bets = bets_by_persona[persona_id]
            wallet = wallets[persona_id]
            amount = sum(calculate_refund(bet.amount) for bet in bets)
            plans.append(
                MatchCancellationRefundPlan(
                    persona_id=persona_id,
                    bet_ids=tuple(bet.id for bet in bets),
                    amount=amount,
                    balance_before=wallet.balance,
                    balance_after=calculate_next_circle_point_balance(wallet.balance, amount),
                )
            )
        return tuple(plans)

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredMatchCancellationOperation,
        command: CancelMatch,
    ) -> CancelledMatch:
        if (
            stored.type != MatchCancellationAuditType.CANCELLED.value
            or stored.request_fingerprint != command.request_fingerprint
            or stored.match_id != command.match_id
        ):
            raise MatchCancellationIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise MatchCancellationAuditError("Exact-retry operation has no stored after snapshot.")
        try:
            cancelled = CancelledMatch.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchCancellationAuditError("Exact-retry operation has malformed stored evidence.") from exc
        if cancelled.match_id != command.match_id or cancelled.reason != command.reason:
            raise MatchCancellationAuditError("Exact-retry snapshot belongs to another cancellation.")
        return cancelled
