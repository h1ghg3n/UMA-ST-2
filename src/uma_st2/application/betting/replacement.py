"""Native V2 member Bet replacement command boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.betting import (
    BetStatus,
    BetType,
    calculate_bet_stake_cap,
    calculate_refund,
    canonicalize_selections,
    validate_new_bet_stake,
)
from uma_st2.domain.identity import PersonaStatus
from uma_st2.domain.match import MatchSourceKind, MatchStatus
from uma_st2.domain.point import calculate_next_circle_point_balance
from uma_st2.shared import normalize_utc_datetime

from .placement import (
    BetPlacementEntry,
    BetPlacementPersona,
    BetPlacementTarget,
    BetPlacementWallet,
    PlacedMatchBet,
    fingerprint_bet_selection,
)

BET_REPLACEMENT_AUDIT_SCHEMA_VERSION: Final = 1
BET_REPLACEMENT_REFUND_POINT_ACTION: Final = "match_bet_refund"


class BetReplacementAuditType(StrEnum):
    """Canonical Bet operation type for one accepted replacement."""

    REPLACED = "bet_replaced"


class BetReplacementError(ValueError):
    """Base error for a rejected member Bet replacement."""


class BetReplacementUnavailableError(BetReplacementError):
    """The selected Bet or Match no longer permits replacement."""


class BetReplacementIdentityError(BetReplacementError):
    """The actor has no currently eligible Persona identity."""


class BetReplacementApprovalPendingError(BetReplacementIdentityError):
    """The actor Persona is read-only while approval is pending."""


class BetReplacementWalletUnavailableError(BetReplacementError):
    """The actor Persona has no canonical Circle Point wallet."""


class BetReplacementInsufficientBalanceError(BetReplacementError):
    """The refunded wallet cannot fund the desired new stake."""


class BetReplacementStakeLimitError(BetReplacementError):
    """The desired stake exceeds the post-refund balance-based operation cap."""

    def __init__(self, *, maximum_stake: int) -> None:
        if isinstance(maximum_stake, bool) or not isinstance(maximum_stake, int) or maximum_stake <= 0:
            raise ValueError("maximum_stake must be a positive integer.")
        self.maximum_stake = maximum_stake
        super().__init__(f"Replacement stake exceeds the current {maximum_stake} Circle Point limit.")


class BetReplacementDuplicateError(BetReplacementError):
    """Another active Bet already owns the desired selection."""


class BetReplacementSelectionUnavailableError(BetReplacementError):
    """At least one desired Entry number is not in the current Match."""


class BetReplacementNoChangeError(BetReplacementError):
    """The desired Bet is exactly equal to the selected active Bet."""


class BetReplacementIdempotencyConflictError(BetReplacementError):
    """An idempotency key is bound to another logical operation."""


class BetReplacementInvalidSourceError(BetReplacementError):
    """Persisted Match, Bet, identity, wallet, or audit facts are malformed."""


class BetReplacementAuditError(BetReplacementError):
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


@dataclass(frozen=True, slots=True)
class BetReplacementBet:
    """One immutable old/new Bet snapshot retained by replacement evidence."""

    id: int
    match_id: int
    persona_id: str
    bet_type: BetType
    selections: tuple[BetPlacementEntry, ...]
    selection_fingerprint: str
    amount: int
    status: BetStatus

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        object.__setattr__(self, "bet_type", BetType(self.bet_type))
        selections = tuple(self.selections)
        if not selections or any(not isinstance(entry, BetPlacementEntry) for entry in selections):
            raise ValueError("selections must contain BetPlacementEntry values.")
        canonical_ids = canonicalize_selections(self.bet_type, tuple(entry.id for entry in selections))
        if canonical_ids != tuple(entry.id for entry in selections):
            raise ValueError("Bet selections must use canonical Entry ID order.")
        if len({entry.entry_number for entry in selections}) != len(selections):
            raise ValueError("Bet selections must have unique Entry numbers.")
        object.__setattr__(self, "selections", selections)
        object.__setattr__(
            self,
            "selection_fingerprint",
            _normalized_string(
                self.selection_fingerprint,
                field_name="selection_fingerprint",
                max_length=64,
            ),
        )
        if self.selection_fingerprint != fingerprint_bet_selection(
            bet_type=self.bet_type,
            selection_ids=canonical_ids,
        ):
            raise ValueError("Bet selection fingerprint does not match its selections.")
        validate_new_bet_stake(self.amount)
        object.__setattr__(self, "status", BetStatus(self.status))
        if self.status not in {BetStatus.ACTIVE, BetStatus.CANCELLED}:
            raise ValueError("Replacement evidence supports only active or cancelled Bet status.")

    def with_status(self, status: BetStatus) -> BetReplacementBet:
        """Return the same immutable payload with its lifecycle snapshot changed."""

        return BetReplacementBet(
            id=self.id,
            match_id=self.match_id,
            persona_id=self.persona_id,
            bet_type=self.bet_type,
            selections=self.selections,
            selection_fingerprint=self.selection_fingerprint,
            amount=self.amount,
            status=status,
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "bet_id": self.id,
            "match_id": self.match_id,
            "persona_id": self.persona_id,
            "bet_type": self.bet_type.value,
            "selections": [entry.to_audit_payload() for entry in self.selections],
            "selection_fingerprint": self.selection_fingerprint,
            "amount": self.amount,
            "status": self.status.value,
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> BetReplacementBet:
        selection_payloads = payload["selections"]
        if not isinstance(selection_payloads, Sequence) or isinstance(selection_payloads, (str, bytes)):
            raise ValueError("selections must be a sequence.")
        selections: list[BetPlacementEntry] = []
        for value in selection_payloads:
            if not isinstance(value, Mapping):
                raise ValueError("Each selection must be an object.")
            selections.append(BetPlacementEntry.from_audit_payload(value))
        return cls(
            id=_payload_positive_int(payload, "bet_id"),
            match_id=_payload_positive_int(payload, "match_id"),
            persona_id=_payload_string(payload, "persona_id"),
            bet_type=BetType(_payload_string(payload, "bet_type")),
            selections=tuple(selections),
            selection_fingerprint=_payload_string(payload, "selection_fingerprint"),
            amount=_payload_positive_int(payload, "amount"),
            status=BetStatus(_payload_string(payload, "status")),
        )


@dataclass(frozen=True, slots=True)
class ReplacedMatchBet:
    """Committed old/new Bet and exact Point receipt returned after replacement."""

    old_bet: BetReplacementBet
    new_bet: PlacedMatchBet
    balance_before: int
    balance_after_refund: int
    balance_after: int
    refund_point_transaction_id: int
    stake_point_transaction_id: int
    replaced_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.old_bet, BetReplacementBet) or self.old_bet.status is not BetStatus.CANCELLED:
            raise ValueError("old_bet must be the committed cancelled Bet snapshot.")
        if not isinstance(self.new_bet, PlacedMatchBet):
            raise ValueError("new_bet must be a PlacedMatchBet.")
        if self.old_bet.id == self.new_bet.bet_id:
            raise ValueError("Replacement must create a distinct new Bet row.")
        if self.old_bet.match_id != self.new_bet.match_id or self.old_bet.persona_id != self.new_bet.persona_id:
            raise ValueError("Replacement old/new Bet ownership must match.")
        _require_non_negative_int(self.balance_before, field_name="balance_before")
        _require_non_negative_int(self.balance_after_refund, field_name="balance_after_refund")
        _require_non_negative_int(self.balance_after, field_name="balance_after")
        expected_after_refund = calculate_next_circle_point_balance(self.balance_before, self.old_bet.amount)
        if self.balance_after_refund != expected_after_refund:
            raise ValueError("Replacement refund balance does not match the exact old stake.")
        expected_after = calculate_next_circle_point_balance(self.balance_after_refund, -self.new_bet.amount)
        if self.balance_after != expected_after or self.new_bet.balance_after != self.balance_after:
            raise ValueError("Replacement final balance does not match the new stake debit.")
        _require_positive_int(self.refund_point_transaction_id, field_name="refund_point_transaction_id")
        _require_positive_int(self.stake_point_transaction_id, field_name="stake_point_transaction_id")
        if self.refund_point_transaction_id == self.stake_point_transaction_id:
            raise ValueError("Replacement refund and stake transactions must be distinct.")
        object.__setattr__(
            self,
            "replaced_at",
            normalize_utc_datetime(self.replaced_at, field_name="replaced_at"),
        )
        if self.new_bet.placed_at != self.replaced_at:
            raise ValueError("Replacement and new Bet timestamps must match.")

    @property
    def match_id(self) -> int:
        return self.new_bet.match_id

    @property
    def persona_id(self) -> str:
        return self.new_bet.persona_id

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": BET_REPLACEMENT_AUDIT_SCHEMA_VERSION,
            "old_bet": self.old_bet.to_audit_payload(),
            "new_bet": self.new_bet.to_audit_payload(),
            "balance_before": self.balance_before,
            "balance_after_refund": self.balance_after_refund,
            "balance_after": self.balance_after,
            "refund_point_transaction_id": self.refund_point_transaction_id,
            "stake_point_transaction_id": self.stake_point_transaction_id,
            "replaced_at": self.replaced_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> ReplacedMatchBet:
        if payload.get("schema_version") != BET_REPLACEMENT_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Bet replacement audit schema version.")
        old_payload = payload["old_bet"]
        new_payload = payload["new_bet"]
        if not isinstance(old_payload, Mapping) or not isinstance(new_payload, Mapping):
            raise ValueError("Replacement old/new Bet evidence must be objects.")
        return cls(
            old_bet=BetReplacementBet.from_audit_payload(old_payload),
            new_bet=PlacedMatchBet.from_audit_payload(new_payload),
            balance_before=_payload_non_negative_int(payload, "balance_before"),
            balance_after_refund=_payload_non_negative_int(payload, "balance_after_refund"),
            balance_after=_payload_non_negative_int(payload, "balance_after"),
            refund_point_transaction_id=_payload_positive_int(payload, "refund_point_transaction_id"),
            stake_point_transaction_id=_payload_positive_int(payload, "stake_point_transaction_id"),
            replaced_at=datetime.fromisoformat(_payload_string(payload, "replaced_at")),
        )


@dataclass(frozen=True, slots=True)
class ReplaceMatchBet:
    """One direct final member slash replacement request."""

    bet_id: int
    bet_type: BetType
    entry_numbers: tuple[int, ...]
    amount: int
    actor_discord_user_id: str
    guild_id: str
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.bet_id, field_name="bet_id")
        object.__setattr__(self, "bet_type", BetType(self.bet_type))
        object.__setattr__(
            self,
            "entry_numbers",
            canonicalize_selections(self.bet_type, self.entry_numbers),
        )
        validate_new_bet_stake(self.amount)
        for field_name, max_length, optional in (
            ("actor_discord_user_id", 32, False),
            ("guild_id", 32, False),
            ("idempotency_key", 128, False),
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
                "schema": "match-bet-replacement-command-v1",
                "bet_id": self.bet_id,
                "bet_type": self.bet_type.value,
                "entry_numbers": list(self.entry_numbers),
                "amount": self.amount,
                "actor_discord_user_id": self.actor_discord_user_id,
                "guild_id": self.guild_id,
            }
        )


@dataclass(frozen=True, slots=True)
class StoredBetReplacementOperation:
    """Minimal operation data used to resolve an exact retry."""

    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    bet_id: int | None
    after_data: Mapping[str, object] | None


class BetReplacementRepository(Protocol):
    """Persistence operations for one atomic member Bet replacement."""

    def resolve_candidate_match_id(self, *, bet_id: int) -> int | None: ...

    def lock_target(self, *, match_id: int) -> BetPlacementTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredBetReplacementOperation | None: ...

    def lock_bet(self, *, bet_id: int, match_id: int) -> BetReplacementBet | None: ...

    def lock_actor_persona(self, *, discord_user_id: str) -> BetPlacementPersona | None: ...

    def lock_wallet(self, *, persona_id: str) -> BetPlacementWallet | None: ...

    def find_active_duplicate(
        self,
        *,
        match_id: int,
        persona_id: str,
        bet_type: BetType,
        selection_fingerprint: str,
        excluding_bet_id: int,
    ) -> int | None: ...

    def persist_replacement(
        self,
        *,
        command: ReplaceMatchBet,
        target: BetPlacementTarget,
        persona: BetPlacementPersona,
        wallet: BetPlacementWallet,
        old_bet: BetReplacementBet,
        selections: tuple[BetPlacementEntry, ...],
        selection_fingerprint: str,
        balance_after_refund: int,
        balance_after: int,
        replaced_at: datetime,
    ) -> ReplacedMatchBet: ...


class BetReplacementUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing the member replacement repository."""

    @property
    def bet_replacement(self) -> BetReplacementRepository: ...


@dataclass(frozen=True, slots=True)
class BetReplacementCommands:
    """Application entry point for native member Bet replacement."""

    command_runner: CommandRunner[BetReplacementUnitOfWork]
    clock: Callable[[], datetime]

    def replace_bet(self, command: ReplaceMatchBet) -> ReplacedMatchBet:
        return self.command_runner.run(lambda unit_of_work: self._replace(unit_of_work.bet_replacement, command))

    def _replace(self, repository: BetReplacementRepository, command: ReplaceMatchBet) -> ReplacedMatchBet:
        try:
            match_id = repository.resolve_candidate_match_id(bet_id=command.bet_id)
        except (TypeError, ValueError) as exc:
            raise BetReplacementInvalidSourceError("Stored Bet replacement target is malformed.") from exc
        if match_id is None:
            raise BetReplacementUnavailableError("Bet does not exist.")
        _require_positive_int(match_id, field_name="candidate match ID")

        try:
            target = repository.lock_target(match_id=match_id)
        except (TypeError, ValueError) as exc:
            raise BetReplacementInvalidSourceError("Stored Match replacement facts are malformed.") from exc
        if target is None or target.match_id != match_id:
            raise BetReplacementUnavailableError("Bet Match does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command, match_id=match_id)

        if target.source_kind is not MatchSourceKind.NATIVE_V2 or target.status is not MatchStatus.BETTING_OPEN:
            raise BetReplacementUnavailableError("Bet replacement requires a native betting-open Match.")

        try:
            old_bet = repository.lock_bet(bet_id=command.bet_id, match_id=target.match_id)
        except (TypeError, ValueError) as exc:
            raise BetReplacementInvalidSourceError("Stored selected Bet facts are malformed.") from exc
        if old_bet is None or old_bet.status is not BetStatus.ACTIVE:
            raise BetReplacementUnavailableError("Selected Bet is not active.")
        if old_bet.id != command.bet_id or old_bet.match_id != target.match_id:
            raise BetReplacementInvalidSourceError("Stored selected Bet does not match its locked target.")
        target_entries_by_id = {entry.id: entry for entry in target.entries}
        if any(target_entries_by_id.get(entry.id) != entry for entry in old_bet.selections):
            raise BetReplacementInvalidSourceError("Stored selected Bet references another Match Entry.")

        try:
            persona = repository.lock_actor_persona(discord_user_id=command.actor_discord_user_id)
        except (TypeError, ValueError) as exc:
            raise BetReplacementInvalidSourceError("Stored member identity facts are malformed.") from exc
        if persona is not None and persona.status is PersonaStatus.PENDING_APPROVAL:
            raise BetReplacementApprovalPendingError("Discord actor Persona approval is pending.")
        if persona is None or not persona.is_active or not persona.has_eligible_game_account:
            raise BetReplacementIdentityError(
                "Discord actor requires an active Persona with at least one non-NULL PID GameAccount."
            )
        if old_bet.persona_id != persona.id:
            raise BetReplacementUnavailableError("Selected Bet is not owned by the current actor Persona.")

        try:
            wallet = repository.lock_wallet(persona_id=persona.id)
        except (TypeError, ValueError) as exc:
            raise BetReplacementInvalidSourceError("Stored Circle Point wallet facts are malformed.") from exc
        if wallet is None or wallet.persona_id != persona.id:
            raise BetReplacementWalletUnavailableError("Persona has no canonical Circle Point wallet.")

        entries_by_number = {entry.entry_number: entry for entry in target.entries}
        if any(number not in entries_by_number for number in command.entry_numbers):
            raise BetReplacementSelectionUnavailableError("A desired Entry number is not in the current Match.")
        requested_entries = tuple(entries_by_number[number] for number in command.entry_numbers)
        canonical_ids = canonicalize_selections(command.bet_type, tuple(entry.id for entry in requested_entries))
        entries_by_id = {entry.id: entry for entry in requested_entries}
        selections = tuple(entries_by_id[entry_id] for entry_id in canonical_ids)
        selection_fingerprint = fingerprint_bet_selection(
            bet_type=command.bet_type,
            selection_ids=canonical_ids,
        )

        if (
            old_bet.bet_type is command.bet_type
            and old_bet.selection_fingerprint == selection_fingerprint
            and old_bet.amount == command.amount
        ):
            raise BetReplacementNoChangeError("Desired Bet is exactly equal to the selected active Bet.")

        try:
            duplicate_id = repository.find_active_duplicate(
                match_id=target.match_id,
                persona_id=persona.id,
                bet_type=command.bet_type,
                selection_fingerprint=selection_fingerprint,
                excluding_bet_id=old_bet.id,
            )
        except (TypeError, ValueError) as exc:
            raise BetReplacementInvalidSourceError("Stored active Bet facts are malformed.") from exc
        if duplicate_id is not None:
            _require_positive_int(duplicate_id, field_name="duplicate Bet ID")
            raise BetReplacementDuplicateError("Another matching active Bet already exists.")

        refund_amount = calculate_refund(old_bet.amount)
        balance_after_refund = calculate_next_circle_point_balance(wallet.balance, refund_amount)
        maximum_stake = calculate_bet_stake_cap(balance_after_refund)
        if command.amount > maximum_stake:
            raise BetReplacementStakeLimitError(maximum_stake=maximum_stake)
        balance_after = calculate_next_circle_point_balance(balance_after_refund, -command.amount)
        if balance_after < 0:
            raise BetReplacementInsufficientBalanceError("Circle Point balance is insufficient after refund.")
        replaced_at = normalize_utc_datetime(self.clock(), field_name="clock result")

        try:
            replaced = repository.persist_replacement(
                command=command,
                target=target,
                persona=persona,
                wallet=wallet,
                old_bet=old_bet,
                selections=selections,
                selection_fingerprint=selection_fingerprint,
                balance_after_refund=balance_after_refund,
                balance_after=balance_after,
                replaced_at=replaced_at,
            )
        except BetReplacementError:
            raise
        except (TypeError, ValueError) as exc:
            raise BetReplacementInvalidSourceError(
                "Bet replacement did not produce complete canonical evidence."
            ) from exc
        if (
            replaced.old_bet != old_bet.with_status(BetStatus.CANCELLED)
            or replaced.new_bet.match_id != target.match_id
            or replaced.new_bet.persona_id != persona.id
            or replaced.new_bet.bet_type is not command.bet_type
            or replaced.new_bet.selections != selections
            or replaced.new_bet.selection_fingerprint != selection_fingerprint
            or replaced.new_bet.amount != command.amount
            or replaced.balance_before != wallet.balance
            or replaced.balance_after_refund != balance_after_refund
            or replaced.balance_after != balance_after
            or replaced.replaced_at != replaced_at
        ):
            raise BetReplacementInvalidSourceError("Persisted Bet replacement receipt does not match the command.")
        return replaced

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredBetReplacementOperation,
        command: ReplaceMatchBet,
        match_id: int,
    ) -> ReplacedMatchBet:
        if (
            stored.type != BetReplacementAuditType.REPLACED.value
            or stored.request_fingerprint != command.request_fingerprint
            or stored.match_id != match_id
        ):
            raise BetReplacementIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise BetReplacementAuditError("Exact-retry operation has no stored after snapshot.")
        try:
            replaced = ReplacedMatchBet.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise BetReplacementAuditError("Exact-retry operation has malformed stored evidence.") from exc
        if (
            replaced.match_id != match_id
            or replaced.old_bet.id != command.bet_id
            or replaced.new_bet.bet_id != stored.bet_id
            or replaced.new_bet.bet_type is not command.bet_type
            or tuple(entry.entry_number for entry in replaced.new_bet.selections) != command.entry_numbers
            or replaced.new_bet.amount != command.amount
        ):
            raise BetReplacementAuditError("Exact-retry evidence does not match the replacement command.")
        return replaced
