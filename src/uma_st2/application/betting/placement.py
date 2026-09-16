"""Native V2 member Bet placement command boundary."""

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
    canonicalize_selections,
    validate_new_bet_stake,
)
from uma_st2.domain.identity import PersonaStatus, allows_member_mutation
from uma_st2.domain.match import MatchSourceKind, MatchStatus
from uma_st2.domain.point import calculate_next_circle_point_balance
from uma_st2.shared import normalize_utc_datetime

BET_PLACEMENT_AUDIT_SCHEMA_VERSION: Final = 1
BET_STAKE_POINT_ACTION: Final = "match_bet_stake"


class BetPlacementAuditType(StrEnum):
    """Canonical Bet operation type for one accepted placement."""

    PLACED = "bet_placed"


class BetPlacementError(ValueError):
    """Base error for a rejected member Bet placement."""


class BetPlacementUnavailableError(BetPlacementError):
    """The selected Match is absent or no longer accepts Bets."""


class BetPlacementIdentityError(BetPlacementError):
    """The actor has no currently eligible Persona identity."""


class BetPlacementApprovalPendingError(BetPlacementIdentityError):
    """The actor Persona is read-only while approval is pending."""


class BetPlacementWalletUnavailableError(BetPlacementError):
    """The actor Persona has no canonical Circle Point wallet."""


class BetPlacementInsufficientBalanceError(BetPlacementError):
    """The actor Persona wallet cannot fund the requested stake."""


class BetPlacementStakeLimitError(BetPlacementError):
    """The requested stake exceeds the current balance-based operation cap."""

    def __init__(self, *, maximum_stake: int) -> None:
        if isinstance(maximum_stake, bool) or not isinstance(maximum_stake, int) or maximum_stake <= 0:
            raise ValueError("maximum_stake must be a positive integer.")
        self.maximum_stake = maximum_stake
        super().__init__(f"Bet stake exceeds the current {maximum_stake} Circle Point limit.")


class BetPlacementDuplicateError(BetPlacementError):
    """The same Persona already owns the same active selection."""


class BetPlacementSelectionUnavailableError(BetPlacementError):
    """At least one requested Entry number is not in the current Match."""


class BetPlacementIdempotencyConflictError(BetPlacementError):
    """An idempotency key is bound to another logical placement."""


class BetPlacementInvalidSourceError(BetPlacementError):
    """Persisted Match, identity, wallet, Bet, or audit facts are malformed."""


class BetPlacementAuditError(BetPlacementError):
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


def fingerprint_bet_selection(*, bet_type: BetType, selection_ids: Sequence[int]) -> str:
    """Return the stable fingerprint used by the active duplicate guard."""

    canonical = canonicalize_selections(BetType(bet_type), selection_ids)
    return _fingerprint(
        {
            "schema": "match-bet-selection-v1",
            "bet_type": BetType(bet_type).value,
            "selection_ids": list(canonical),
        }
    )


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
class BetPlacementEntry:
    """One immutable Match Entry identity available for selection."""

    id: int
    entry_number: int

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.entry_number, field_name="entry_number")

    def to_audit_payload(self) -> dict[str, int]:
        return {"entry_id": self.id, "entry_number": self.entry_number}

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> BetPlacementEntry:
        return cls(
            id=_payload_positive_int(payload, "entry_id"),
            entry_number=_payload_positive_int(payload, "entry_number"),
        )


@dataclass(frozen=True, slots=True)
class BetPlacementTarget:
    """Locked Match authority and immutable Entry identities for placement."""

    match_id: int
    match_name: str
    source_kind: MatchSourceKind
    status: MatchStatus
    entries: tuple[BetPlacementEntry, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchStatus(self.status))
        entries = tuple(self.entries)
        if not entries or any(not isinstance(entry, BetPlacementEntry) for entry in entries):
            raise ValueError("entries must contain at least one BetPlacementEntry.")
        if len({entry.id for entry in entries}) != len(entries):
            raise ValueError("Bet placement entries must have unique IDs.")
        if len({entry.entry_number for entry in entries}) != len(entries):
            raise ValueError("Bet placement entries must have unique Entry numbers.")
        object.__setattr__(self, "entries", tuple(sorted(entries, key=lambda entry: entry.entry_number)))


@dataclass(frozen=True, slots=True)
class BetPlacementPersona:
    """Current actor Persona and required game-identity gate."""

    id: str
    status: PersonaStatus
    has_eligible_game_account: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _normalized_string(self.id, field_name="id", max_length=36))
        object.__setattr__(self, "status", PersonaStatus(self.status))
        if not isinstance(self.has_eligible_game_account, bool):
            raise ValueError("has_eligible_game_account must be a boolean.")

    @property
    def is_active(self) -> bool:
        return allows_member_mutation(self.status)


@dataclass(frozen=True, slots=True)
class BetPlacementWallet:
    """Locked current Circle Point wallet snapshot."""

    persona_id: str
    balance: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        _require_non_negative_int(self.balance, field_name="balance")


@dataclass(frozen=True, slots=True)
class PlacedMatchBet:
    """Committed placement receipt returned after save or exact retry."""

    bet_id: int
    match_id: int
    match_name: str
    persona_id: str
    bet_type: BetType
    selections: tuple[BetPlacementEntry, ...]
    selection_fingerprint: str
    amount: int
    status: BetStatus
    balance_after: int
    placed_at: datetime

    def __post_init__(self) -> None:
        _require_positive_int(self.bet_id, field_name="bet_id")
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        object.__setattr__(self, "bet_type", BetType(self.bet_type))
        selections = tuple(self.selections)
        canonical_ids = canonicalize_selections(self.bet_type, tuple(entry.id for entry in selections))
        if canonical_ids != tuple(entry.id for entry in selections):
            raise ValueError("Receipt selections must use canonical Entry ID order.")
        if len({entry.entry_number for entry in selections}) != len(selections):
            raise ValueError("Receipt selections must have unique Entry numbers.")
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
            selection_ids=tuple(entry.id for entry in selections),
        ):
            raise ValueError("Receipt selection fingerprint does not match its selections.")
        validate_new_bet_stake(self.amount)
        object.__setattr__(self, "status", BetStatus(self.status))
        if self.status != BetStatus.ACTIVE:
            raise ValueError("A placement receipt must contain an active Bet.")
        _require_non_negative_int(self.balance_after, field_name="balance_after")
        object.__setattr__(
            self,
            "placed_at",
            normalize_utc_datetime(self.placed_at, field_name="placed_at"),
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": BET_PLACEMENT_AUDIT_SCHEMA_VERSION,
            "bet_id": self.bet_id,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "persona_id": self.persona_id,
            "bet_type": self.bet_type.value,
            "selections": [selection.to_audit_payload() for selection in self.selections],
            "selection_fingerprint": self.selection_fingerprint,
            "amount": self.amount,
            "status": self.status.value,
            "balance_after": self.balance_after,
            "placed_at": self.placed_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> PlacedMatchBet:
        if payload.get("schema_version") != BET_PLACEMENT_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Bet placement audit schema version.")
        selection_payloads = payload["selections"]
        if not isinstance(selection_payloads, Sequence) or isinstance(selection_payloads, (str, bytes)):
            raise ValueError("selections must be a sequence.")
        selections: list[BetPlacementEntry] = []
        for value in selection_payloads:
            if not isinstance(value, Mapping):
                raise ValueError("Each selection must be an object.")
            selections.append(BetPlacementEntry.from_audit_payload(value))
        return cls(
            bet_id=_payload_positive_int(payload, "bet_id"),
            match_id=_payload_positive_int(payload, "match_id"),
            match_name=_payload_string(payload, "match_name"),
            persona_id=_payload_string(payload, "persona_id"),
            bet_type=BetType(_payload_string(payload, "bet_type")),
            selections=tuple(selections),
            selection_fingerprint=_payload_string(payload, "selection_fingerprint"),
            amount=_payload_positive_int(payload, "amount"),
            status=BetStatus(_payload_string(payload, "status")),
            balance_after=_payload_non_negative_int(payload, "balance_after"),
            placed_at=datetime.fromisoformat(_payload_string(payload, "placed_at")),
        )


@dataclass(frozen=True, slots=True)
class PlaceMatchBet:
    """One direct final member slash placement request."""

    match_id: int
    bet_type: BetType
    entry_numbers: tuple[int, ...]
    amount: int
    actor_discord_user_id: str
    guild_id: str
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
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
                "schema": "match-bet-placement-command-v1",
                "match_id": self.match_id,
                "bet_type": self.bet_type.value,
                "entry_numbers": list(self.entry_numbers),
                "amount": self.amount,
                "actor_discord_user_id": self.actor_discord_user_id,
                "guild_id": self.guild_id,
            }
        )


@dataclass(frozen=True, slots=True)
class StoredBetPlacementOperation:
    """Minimal operation data used to resolve an exact retry."""

    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    bet_id: int | None
    after_data: Mapping[str, object] | None


class BetPlacementRepository(Protocol):
    """Persistence operations for one atomic member placement."""

    def lock_target(self, *, match_id: int) -> BetPlacementTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredBetPlacementOperation | None: ...

    def lock_actor_persona(self, *, discord_user_id: str) -> BetPlacementPersona | None: ...

    def lock_wallet(self, *, persona_id: str) -> BetPlacementWallet | None: ...

    def find_active_duplicate(
        self,
        *,
        match_id: int,
        persona_id: str,
        bet_type: BetType,
        selection_fingerprint: str,
    ) -> int | None: ...

    def persist_placement(
        self,
        *,
        command: PlaceMatchBet,
        target: BetPlacementTarget,
        persona: BetPlacementPersona,
        wallet: BetPlacementWallet,
        selections: tuple[BetPlacementEntry, ...],
        selection_fingerprint: str,
        balance_after: int,
        placed_at: datetime,
    ) -> PlacedMatchBet: ...


class BetPlacementUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing the member placement repository."""

    @property
    def bet_placement(self) -> BetPlacementRepository: ...


@dataclass(frozen=True, slots=True)
class BetPlacementCommands:
    """Application entry point for native member Bet placement."""

    command_runner: CommandRunner[BetPlacementUnitOfWork]
    clock: Callable[[], datetime]

    def place_bet(self, command: PlaceMatchBet) -> PlacedMatchBet:
        return self.command_runner.run(lambda unit_of_work: self._place(unit_of_work.bet_placement, command))

    def _place(self, repository: BetPlacementRepository, command: PlaceMatchBet) -> PlacedMatchBet:
        try:
            target = repository.lock_target(match_id=command.match_id)
        except (TypeError, ValueError) as exc:
            raise BetPlacementInvalidSourceError("Stored Match placement facts are malformed.") from exc
        if target is None:
            raise BetPlacementUnavailableError("Match does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if target.source_kind != MatchSourceKind.NATIVE_V2 or target.status != MatchStatus.BETTING_OPEN:
            raise BetPlacementUnavailableError("Bet placement requires a native betting-open Match.")

        try:
            persona = repository.lock_actor_persona(discord_user_id=command.actor_discord_user_id)
        except (TypeError, ValueError) as exc:
            raise BetPlacementInvalidSourceError("Stored member identity facts are malformed.") from exc
        if persona is not None and persona.status is PersonaStatus.PENDING_APPROVAL:
            raise BetPlacementApprovalPendingError("Discord actor Persona approval is pending.")
        if persona is None or not persona.is_active or not persona.has_eligible_game_account:
            raise BetPlacementIdentityError(
                "Discord actor requires an active Persona with at least one non-NULL PID GameAccount."
            )

        try:
            wallet = repository.lock_wallet(persona_id=persona.id)
        except (TypeError, ValueError) as exc:
            raise BetPlacementInvalidSourceError("Stored Circle Point wallet facts are malformed.") from exc
        if wallet is None or wallet.persona_id != persona.id:
            raise BetPlacementWalletUnavailableError("Persona has no canonical Circle Point wallet.")

        entries_by_number = {entry.entry_number: entry for entry in target.entries}
        if any(number not in entries_by_number for number in command.entry_numbers):
            raise BetPlacementSelectionUnavailableError("A selected Entry number is not in the current Match.")
        requested_entries = tuple(entries_by_number[number] for number in command.entry_numbers)
        canonical_ids = canonicalize_selections(command.bet_type, tuple(entry.id for entry in requested_entries))
        entries_by_id = {entry.id: entry for entry in requested_entries}
        selections = tuple(entries_by_id[entry_id] for entry_id in canonical_ids)
        selection_fingerprint = fingerprint_bet_selection(
            bet_type=command.bet_type,
            selection_ids=canonical_ids,
        )

        try:
            duplicate_id = repository.find_active_duplicate(
                match_id=target.match_id,
                persona_id=persona.id,
                bet_type=command.bet_type,
                selection_fingerprint=selection_fingerprint,
            )
        except (TypeError, ValueError) as exc:
            raise BetPlacementInvalidSourceError("Stored active Bet facts are malformed.") from exc
        if duplicate_id is not None:
            _require_positive_int(duplicate_id, field_name="duplicate Bet ID")
            raise BetPlacementDuplicateError("Matching active Bet already exists.")

        maximum_stake = calculate_bet_stake_cap(wallet.balance)
        if command.amount > maximum_stake:
            raise BetPlacementStakeLimitError(maximum_stake=maximum_stake)
        if wallet.balance < command.amount:
            raise BetPlacementInsufficientBalanceError("Circle Point balance is insufficient.")
        balance_after = calculate_next_circle_point_balance(wallet.balance, -command.amount)
        if balance_after < 0:
            raise BetPlacementInsufficientBalanceError("Circle Point balance is insufficient.")
        placed_at = normalize_utc_datetime(self.clock(), field_name="clock result")

        try:
            placed = repository.persist_placement(
                command=command,
                target=target,
                persona=persona,
                wallet=wallet,
                selections=selections,
                selection_fingerprint=selection_fingerprint,
                balance_after=balance_after,
                placed_at=placed_at,
            )
        except BetPlacementError:
            raise
        except (TypeError, ValueError) as exc:
            raise BetPlacementInvalidSourceError("Bet placement did not produce complete canonical evidence.") from exc
        if (
            placed.match_id != target.match_id
            or placed.persona_id != persona.id
            or placed.bet_type != command.bet_type
            or placed.selections != selections
            or placed.selection_fingerprint != selection_fingerprint
            or placed.amount != command.amount
            or placed.balance_after != balance_after
            or placed.placed_at != placed_at
        ):
            raise BetPlacementInvalidSourceError("Persisted Bet placement receipt does not match the command.")
        return placed

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredBetPlacementOperation,
        command: PlaceMatchBet,
    ) -> PlacedMatchBet:
        if (
            stored.type != BetPlacementAuditType.PLACED.value
            or stored.request_fingerprint != command.request_fingerprint
            or stored.match_id != command.match_id
        ):
            raise BetPlacementIdempotencyConflictError("Idempotency key is already bound to another logical operation.")
        if stored.after_data is None:
            raise BetPlacementAuditError("Exact-retry operation has no stored after snapshot.")
        try:
            placed = PlacedMatchBet.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise BetPlacementAuditError("Exact-retry operation has malformed stored evidence.") from exc
        if (
            placed.match_id != command.match_id
            or placed.bet_id != stored.bet_id
            or placed.bet_type != command.bet_type
            or tuple(selection.entry_number for selection in placed.selections) != command.entry_numbers
            or placed.amount != command.amount
        ):
            raise BetPlacementAuditError("Exact-retry snapshot does not match its Bet operation.")
        return placed
