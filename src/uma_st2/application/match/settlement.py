"""Atomic native V2 Match settlement boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.application.publication import PublicationIntent
from uma_st2.application.publication.match import (
    MatchResultPublicationSource,
    build_match_result_publication_intent,
)
from uma_st2.domain.betting import (
    APPLIED_ODDS_QUANTUM,
    PROVISIONAL_ODDS_QUANTUM,
    WEIGHTED_ODDS_RULE_VERSION,
    BetPoolStake,
    BetType,
    calculate_payout,
    calculate_provisional_odds,
    quantize_applied_odds,
)
from uma_st2.domain.match import (
    PLACEMENT_REWARD_RULE_VERSION,
    MatchGrade,
    MatchRatingDisposition,
    MatchSourceKind,
    MatchStatus,
    calculate_match_placement_reward,
)
from uma_st2.domain.point import calculate_next_circle_point_balance
from uma_st2.domain.rating import (
    RATING_FORMULA_VERSION,
    RatingCalculationError,
    RatingParticipant,
    RatingRule,
    RatingRuleError,
    calculate_rating_transactions,
    normalize_rating_storage,
)
from uma_st2.shared import normalize_utc_datetime

MATCH_SETTLEMENT_AUDIT_SCHEMA_VERSION: Final = 2
MATCH_BET_PAYOUT_POINT_ACTION: Final = "match_bet_payout"
MATCH_PLACEMENT_REWARD_POINT_ACTION: Final = "match_placement_reward"


class MatchSettlementAuditType(StrEnum):
    """Canonical operation type for a completed native settlement."""

    SETTLED = "match_settled"


class MatchSettlementError(ValueError):
    """Base error for rejected native Match settlement."""


class MatchSettlementUnavailableError(MatchSettlementError):
    """The selected Match is absent or no longer settlement-eligible."""


class MatchSettlementStaleError(MatchSettlementError):
    """The confirmed result, pool, Rating, or rule version changed after Preview."""


class MatchSettlementRuleUnavailableError(MatchSettlementError):
    """No complete reviewed Rating rule version can settle this Match."""


class MatchSettlementRatingSelectionError(MatchSettlementError):
    """The explicit Rating participation selection is incomplete or invalid."""


class MatchSettlementWalletUnavailableError(MatchSettlementError):
    """A Bet or placement-reward Persona has no canonical wallet."""


class MatchSettlementInvalidSourceError(MatchSettlementError):
    """Persisted Match settlement authority is malformed."""


class MatchSettlementIdempotencyConflictError(MatchSettlementError):
    """An idempotency key is bound to another logical settlement."""


class MatchSettlementAuditError(MatchSettlementError):
    """Stored exact-retry evidence is absent or malformed."""


def _require_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _require_non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


def _require_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer.")
    return value


def _normalized_string(
    value: object,
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


def _sha256_hex(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a SHA-256 hex string.")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{field_name} must be a SHA-256 hex string.")
    return normalized


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _normalize_excluded_rating_entry_ids(values: tuple[int, ...]) -> tuple[int, ...]:
    if not isinstance(values, tuple):
        raise ValueError("excluded_rating_entry_ids must be a tuple.")
    normalized = tuple(sorted(_require_positive_int(value, field_name="excluded_rating_entry_id") for value in values))
    if len(set(normalized)) != len(normalized):
        raise ValueError("excluded_rating_entry_ids must be unique.")
    return normalized


def _winning_market_identities(
    ranked_entries: tuple[tuple[int, int], ...],
) -> tuple[tuple[BetType, tuple[int, ...], tuple[int, ...]], ...]:
    """Return canonical winning Entry IDs/numbers for each available market."""

    return tuple(
        (
            bet_type,
            tuple(sorted(entry_id for entry_id, _ in ranked_entries[:selection_count])),
            tuple(sorted(entry_number for _, entry_number in ranked_entries[:selection_count])),
        )
        for selection_count, bet_type in enumerate(BetType, start=1)
        if len(ranked_entries) >= selection_count
    )


def _payload_mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload[key]
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object.")
    return value


def _payload_list(payload: Mapping[str, object], key: str) -> list[object]:
    value = payload[key]
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list.")
    return value


def _payload_mapping_list(payload: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    values = _payload_list(payload, key)
    if any(not isinstance(value, Mapping) for value in values):
        raise ValueError(f"{key} must contain only objects.")
    return tuple(value for value in values if isinstance(value, Mapping))


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = _normalized_string(payload[key], field_name=key, max_length=255)
    assert value is not None
    return value


def _payload_optional_string(payload: Mapping[str, object], key: str, *, max_length: int) -> str | None:
    return _normalized_string(payload.get(key), field_name=key, max_length=max_length, optional=True)


def _payload_decimal(payload: Mapping[str, object], key: str) -> Decimal:
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a decimal string.")
    try:
        converted = Decimal(value)
    except ArithmeticError as exc:
        raise ValueError(f"{key} must be a decimal string.") from exc
    if not converted.is_finite():
        raise ValueError(f"{key} must be finite.")
    return converted


@dataclass(frozen=True, slots=True)
class MatchSettlementLock:
    """Minimal Match-first lock result used before exact-retry resolution."""

    match_id: int
    match_name: str
    source_kind: MatchSourceKind
    status: MatchStatus

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchStatus(self.status))


@dataclass(frozen=True, slots=True)
class MatchSettlementResultAuthority:
    """Current confirmed ResultSubmission identity consumed by settlement."""

    submission_id: int
    revision_number: int
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        _require_positive_int(self.submission_id, field_name="submission_id")
        _require_positive_int(self.revision_number, field_name="revision_number")
        object.__setattr__(
            self,
            "candidate_fingerprint",
            _sha256_hex(self.candidate_fingerprint, field_name="candidate_fingerprint"),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "submission_id": self.submission_id,
            "revision_number": self.revision_number,
            "candidate_fingerprint": self.candidate_fingerprint,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> MatchSettlementResultAuthority:
        return cls(
            submission_id=_require_positive_int(payload["submission_id"], field_name="submission_id"),
            revision_number=_require_positive_int(payload["revision_number"], field_name="revision_number"),
            candidate_fingerprint=_sha256_hex(
                payload["candidate_fingerprint"],
                field_name="candidate_fingerprint",
            ),
        )


@dataclass(frozen=True, slots=True)
class MatchSettlementRuleReference:
    """Immutable Rating rule version selected by settlement."""

    version_id: int
    version_number: int
    rule_set_checksum: str

    def __post_init__(self) -> None:
        _require_positive_int(self.version_id, field_name="version_id")
        _require_positive_int(self.version_number, field_name="version_number")
        object.__setattr__(
            self,
            "rule_set_checksum",
            _sha256_hex(self.rule_set_checksum, field_name="rule_set_checksum"),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "version_number": self.version_number,
            "rule_set_checksum": self.rule_set_checksum,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> MatchSettlementRuleReference:
        return cls(
            version_id=_require_positive_int(payload["version_id"], field_name="version_id"),
            version_number=_require_positive_int(payload["version_number"], field_name="version_number"),
            rule_set_checksum=_sha256_hex(payload["rule_set_checksum"], field_name="rule_set_checksum"),
        )


@dataclass(frozen=True, slots=True)
class MatchSettlementEntry:
    """One confirmed Entry with settlement-time display and Rating authority."""

    match_entry_id: int
    entry_number: int
    game_account_id: int
    owner_at_event_persona_id: str
    game_account_name: str
    horse_name: str
    affiliation_at_event: str | None
    rank: int
    rating_before: Decimal

    def __post_init__(self) -> None:
        for field_name in ("match_entry_id", "entry_number", "game_account_id", "rank"):
            _require_positive_int(getattr(self, field_name), field_name=field_name)
        object.__setattr__(
            self,
            "owner_at_event_persona_id",
            _normalized_string(
                self.owner_at_event_persona_id,
                field_name="owner_at_event_persona_id",
                max_length=36,
            ),
        )
        object.__setattr__(
            self,
            "game_account_name",
            _normalized_string(self.game_account_name, field_name="game_account_name", max_length=100),
        )
        object.__setattr__(
            self,
            "horse_name",
            _normalized_string(self.horse_name, field_name="horse_name", max_length=100),
        )
        object.__setattr__(
            self,
            "affiliation_at_event",
            _normalized_string(
                self.affiliation_at_event,
                field_name="affiliation_at_event",
                max_length=100,
                optional=True,
            ),
        )
        rating_before = normalize_rating_storage(self.rating_before)
        if rating_before < 0:
            raise ValueError("rating_before cannot be negative.")
        object.__setattr__(self, "rating_before", rating_before)

    def to_fingerprint_payload(self) -> dict[str, object]:
        return {
            "match_entry_id": self.match_entry_id,
            "entry_number": self.entry_number,
            "game_account_id": self.game_account_id,
            "owner_at_event_persona_id": self.owner_at_event_persona_id,
            "rank": self.rank,
            "rating_before": _decimal_text(self.rating_before),
        }


@dataclass(frozen=True, slots=True)
class MatchSettlementBet:
    """One current active Bet consumed by settlement."""

    bet_id: int
    persona_id: str
    bet_type: BetType
    selection_entry_ids: tuple[int, ...]
    amount: int

    def __post_init__(self) -> None:
        _require_positive_int(self.bet_id, field_name="bet_id")
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        pool_stake = BetPoolStake(
            bet_type=BetType(self.bet_type),
            selection_ids=tuple(self.selection_entry_ids),
            amount=self.amount,
        )
        object.__setattr__(self, "bet_type", pool_stake.bet_type)
        object.__setattr__(self, "selection_entry_ids", pool_stake.selection_ids)

    def to_pool_stake(self) -> BetPoolStake:
        return BetPoolStake(self.bet_type, self.selection_entry_ids, self.amount)

    def to_fingerprint_payload(self) -> dict[str, object]:
        return {
            "bet_id": self.bet_id,
            "persona_id": self.persona_id,
            "bet_type": self.bet_type.value,
            "selection_entry_ids": list(self.selection_entry_ids),
            "amount": self.amount,
        }


@dataclass(frozen=True, slots=True)
class MatchSettlementWallet:
    """One deterministic Circle Point lock target."""

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
class MatchSettlementTarget:
    """Complete closed/locked authority used to build one deterministic plan."""

    match_id: int
    match_name: str
    source_kind: MatchSourceKind
    status: MatchStatus
    grade: MatchGrade
    scheduled_at: datetime
    result: MatchSettlementResultAuthority
    entries: tuple[MatchSettlementEntry, ...]
    active_bets: tuple[MatchSettlementBet, ...]
    wallets: tuple[MatchSettlementWallet, ...]
    rating_rule_version: MatchSettlementRuleReference | None
    rating_rules: tuple[RatingRule, ...] = field(default_factory=tuple)

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
        if not isinstance(self.result, MatchSettlementResultAuthority):
            raise ValueError("result must be MatchSettlementResultAuthority.")

        entries = tuple(sorted(self.entries, key=lambda entry: (entry.rank, entry.match_entry_id)))
        if not entries or any(not isinstance(entry, MatchSettlementEntry) for entry in entries):
            raise ValueError("entries must contain a complete Match settlement board.")
        if tuple(entry.rank for entry in entries) != tuple(range(1, len(entries) + 1)):
            raise ValueError("Settlement Entry ranks must be contiguous from 1.")
        for key_name, values in (
            ("Match Entry IDs", (entry.match_entry_id for entry in entries)),
            ("Entry numbers", (entry.entry_number for entry in entries)),
        ):
            collected = tuple(values)
            if len(set(collected)) != len(collected):
                raise ValueError(f"{key_name} must be unique.")
        object.__setattr__(self, "entries", entries)

        bets = tuple(sorted(self.active_bets, key=lambda bet: bet.bet_id))
        if any(not isinstance(bet, MatchSettlementBet) for bet in bets):
            raise ValueError("active_bets must contain MatchSettlementBet values.")
        if len({bet.bet_id for bet in bets}) != len(bets):
            raise ValueError("active_bets must have unique IDs.")
        entry_ids = {entry.match_entry_id for entry in entries}
        if any(not set(bet.selection_entry_ids).issubset(entry_ids) for bet in bets):
            raise ValueError("An active Bet selection is outside the confirmed Entry set.")
        object.__setattr__(self, "active_bets", bets)

        wallets = tuple(sorted(self.wallets, key=lambda wallet: wallet.persona_id))
        if any(not isinstance(wallet, MatchSettlementWallet) for wallet in wallets):
            raise ValueError("wallets must contain MatchSettlementWallet values.")
        if len({wallet.persona_id for wallet in wallets}) != len(wallets):
            raise ValueError("wallets must have unique Persona IDs.")
        required_personas = {bet.persona_id for bet in bets}
        if self.grade is not MatchGrade.OP:
            required_personas.update(entry.owner_at_event_persona_id for entry in entries)
        if {wallet.persona_id for wallet in wallets} != required_personas:
            raise MatchSettlementWalletUnavailableError(
                "Every active-Bet or placement-reward Persona must retain a canonical wallet."
            )
        object.__setattr__(self, "wallets", wallets)

        if self.grade is MatchGrade.OP:
            if self.rating_rule_version is not None or self.rating_rules:
                raise ValueError("OP settlement cannot select Rating rules.")
        elif not isinstance(self.rating_rule_version, MatchSettlementRuleReference):
            raise MatchSettlementRuleUnavailableError("A non-OP settlement requires a reviewed Rating rule version.")
        rules = tuple(self.rating_rules)
        if any(not isinstance(rule, RatingRule) for rule in rules):
            raise ValueError("rating_rules must contain RatingRule values.")
        object.__setattr__(self, "rating_rules", rules)

    @property
    def active_stake_total(self) -> int:
        return sum(bet.amount for bet in self.active_bets)

    def settlement_fingerprint(self, *, excluded_rating_entry_ids: tuple[int, ...] = ()) -> str:
        excluded = _normalize_excluded_rating_entry_ids(excluded_rating_entry_ids)
        return _fingerprint(
            {
                "schema": "match-settlement-preview-v2",
                "match_id": self.match_id,
                "source_kind": self.source_kind.value,
                "status": self.status.value,
                "grade": self.grade.value,
                "result": self.result.to_payload(),
                "entries": [entry.to_fingerprint_payload() for entry in self.entries],
                "active_bets": [bet.to_fingerprint_payload() for bet in self.active_bets],
                "excluded_rating_entry_ids": list(excluded),
                "rating_rule_version": (
                    self.rating_rule_version.to_payload() if self.rating_rule_version is not None else None
                ),
                "odds_rule_version": WEIGHTED_ODDS_RULE_VERSION,
                "rating_formula_version": RATING_FORMULA_VERSION,
                "placement_reward_rule_version": PLACEMENT_REWARD_RULE_VERSION,
            }
        )

    def to_audit_payload(self, *, excluded_rating_entry_ids: tuple[int, ...]) -> dict[str, object]:
        excluded = _normalize_excluded_rating_entry_ids(excluded_rating_entry_ids)
        return {
            "schema_version": MATCH_SETTLEMENT_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "source_kind": self.source_kind.value,
            "status": self.status.value,
            "grade": self.grade.value,
            "scheduled_at": self.scheduled_at.isoformat(),
            "result": self.result.to_payload(),
            "settlement_fingerprint": self.settlement_fingerprint(
                excluded_rating_entry_ids=excluded,
            ),
            "entries": [entry.to_fingerprint_payload() for entry in self.entries],
            "active_bets": [bet.to_fingerprint_payload() for bet in self.active_bets],
            "excluded_rating_entry_ids": list(excluded),
            "wallets": [{"persona_id": wallet.persona_id, "balance": wallet.balance} for wallet in self.wallets],
            "rating_rule_version": (
                self.rating_rule_version.to_payload() if self.rating_rule_version is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class MatchSettlementAppliedOdds:
    """Winning selection and immutable applied odds for one available market."""

    bet_type: BetType
    selection_entry_ids: tuple[int, ...]
    selection_entry_numbers: tuple[int, ...]
    provisional_odds: Decimal
    confirmed_odds: Decimal

    def __post_init__(self) -> None:
        pool = BetPoolStake(BetType(self.bet_type), tuple(self.selection_entry_ids), 10)
        object.__setattr__(self, "bet_type", pool.bet_type)
        object.__setattr__(self, "selection_entry_ids", pool.selection_ids)
        numbers = tuple(self.selection_entry_numbers)
        if len(numbers) != len(pool.selection_ids) or any(
            _require_positive_int(number, field_name="selection_entry_number") != number for number in numbers
        ):
            raise ValueError("selection_entry_numbers must match the selection cardinality.")
        object.__setattr__(self, "selection_entry_numbers", tuple(sorted(numbers)))
        if (
            not isinstance(self.provisional_odds, Decimal)
            or self.provisional_odds <= 0
            or self.provisional_odds.as_tuple().exponent != -4
        ):
            raise ValueError("provisional_odds must be a positive four-decimal value.")
        if (
            not isinstance(self.confirmed_odds, Decimal)
            or self.confirmed_odds <= 0
            or self.confirmed_odds.as_tuple().exponent != -1
        ):
            raise ValueError("confirmed_odds must be a positive one-decimal value.")

    def to_payload(self) -> dict[str, object]:
        return {
            "bet_type": self.bet_type.value,
            "selection_entry_ids": list(self.selection_entry_ids),
            "selection_entry_numbers": list(self.selection_entry_numbers),
            "provisional_odds": format(self.provisional_odds, ".4f"),
            "confirmed_odds": format(self.confirmed_odds, ".1f"),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> MatchSettlementAppliedOdds:
        return cls(
            bet_type=BetType(_payload_string(payload, "bet_type")),
            selection_entry_ids=tuple(
                _require_positive_int(value, field_name="selection_entry_id")
                for value in _payload_list(payload, "selection_entry_ids")
            ),
            selection_entry_numbers=tuple(
                _require_positive_int(value, field_name="selection_entry_number")
                for value in _payload_list(payload, "selection_entry_numbers")
            ),
            provisional_odds=_payload_decimal(payload, "provisional_odds"),
            confirmed_odds=_payload_decimal(payload, "confirmed_odds"),
        )


@dataclass(frozen=True, slots=True)
class MatchSettlementPayoutPlan:
    persona_id: str
    bet_ids: tuple[int, ...]
    amount: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        bet_ids = tuple(sorted(self.bet_ids))
        if not bet_ids or len(set(bet_ids)) != len(bet_ids):
            raise ValueError("Payout bet_ids must be non-empty and unique.")
        for bet_id in bet_ids:
            _require_positive_int(bet_id, field_name="bet_id")
        object.__setattr__(self, "bet_ids", bet_ids)
        _require_positive_int(self.amount, field_name="amount")


@dataclass(frozen=True, slots=True)
class MatchSettlementRewardPlan:
    persona_id: str
    selected_match_entry_id: int
    selected_game_account_id: int
    selected_rank: int
    suppressed_match_entry_ids: tuple[int, ...]
    amount: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        for field_name in ("selected_match_entry_id", "selected_game_account_id", "selected_rank", "amount"):
            _require_positive_int(getattr(self, field_name), field_name=field_name)
        suppressed = tuple(sorted(self.suppressed_match_entry_ids))
        if len(set(suppressed)) != len(suppressed) or self.selected_match_entry_id in suppressed:
            raise ValueError("suppressed_match_entry_ids must be distinct from the selected Entry.")
        for entry_id in suppressed:
            _require_positive_int(entry_id, field_name="suppressed_match_entry_id")
        object.__setattr__(self, "suppressed_match_entry_ids", suppressed)


@dataclass(frozen=True, slots=True)
class MatchSettlementRatingPlan:
    match_entry_id: int
    entry_number: int
    game_account_id: int
    game_account_name: str
    horse_name: str
    affiliation_at_event: str | None
    rank: int
    rating_disposition: MatchRatingDisposition
    rating_rank: int | None
    rating_before: Decimal
    base_delta: Decimal
    adjustment_delta: Decimal
    amount: Decimal
    rating_after: Decimal
    transaction_required: bool

    def __post_init__(self) -> None:
        for field_name in ("match_entry_id", "entry_number", "game_account_id", "rank"):
            _require_positive_int(getattr(self, field_name), field_name=field_name)
        object.__setattr__(self, "rating_disposition", MatchRatingDisposition(self.rating_disposition))
        if self.rating_rank is not None:
            _require_positive_int(self.rating_rank, field_name="rating_rank")
        object.__setattr__(
            self,
            "game_account_name",
            _normalized_string(self.game_account_name, field_name="game_account_name", max_length=100),
        )
        object.__setattr__(
            self,
            "horse_name",
            _normalized_string(self.horse_name, field_name="horse_name", max_length=100),
        )
        object.__setattr__(
            self,
            "affiliation_at_event",
            _normalized_string(
                self.affiliation_at_event,
                field_name="affiliation_at_event",
                max_length=100,
                optional=True,
            ),
        )
        for field_name in ("rating_before", "base_delta", "adjustment_delta", "amount", "rating_after"):
            value = getattr(self, field_name)
            if value != normalize_rating_storage(value):
                raise ValueError(f"{field_name} must use Rating storage precision.")
        if self.rating_before + self.amount != self.rating_after:
            raise ValueError("Rating plan must balance.")
        if not isinstance(self.transaction_required, bool):
            raise ValueError("transaction_required must be a boolean.")
        if self.rating_disposition is MatchRatingDisposition.RATED:
            if self.rating_rank is None or not self.transaction_required:
                raise ValueError("A rated Entry requires a derived rank and Rating transaction.")
        elif self.rating_rank is not None or self.transaction_required:
            raise ValueError("A non-rated Entry cannot have a derived rank or Rating transaction.")
        if not self.transaction_required and any(
            value != Decimal() for value in (self.base_delta, self.adjustment_delta, self.amount)
        ):
            raise ValueError("A no-transaction Rating snapshot cannot change Rating.")


@dataclass(frozen=True, slots=True)
class MatchSettlementWalletDelta:
    persona_id: str
    balance_before: int
    payout_amount: int
    reward_amount: int
    balance_after: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        _require_int(self.balance_before, field_name="balance_before")
        _require_non_negative_int(self.payout_amount, field_name="payout_amount")
        _require_non_negative_int(self.reward_amount, field_name="reward_amount")
        if self.payout_amount + self.reward_amount <= 0:
            raise ValueError("A wallet delta must contain a positive settlement credit.")
        if (
            calculate_next_circle_point_balance(
                self.balance_before,
                self.payout_amount + self.reward_amount,
            )
            != self.balance_after
        ):
            raise ValueError("balance_after does not match the settlement credit.")


@dataclass(frozen=True, slots=True)
class MatchSettlementPlan:
    """Deterministic Preview/final plan built only from current canonical authority."""

    target: MatchSettlementTarget
    settlement_fingerprint: str
    excluded_rating_entry_ids: tuple[int, ...]
    applied_odds: tuple[MatchSettlementAppliedOdds, ...]
    payouts: tuple[MatchSettlementPayoutPlan, ...]
    rewards: tuple[MatchSettlementRewardPlan, ...]
    ratings: tuple[MatchSettlementRatingPlan, ...]
    wallet_deltas: tuple[MatchSettlementWalletDelta, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.target, MatchSettlementTarget):
            raise ValueError("target must be MatchSettlementTarget.")
        object.__setattr__(
            self,
            "settlement_fingerprint",
            _sha256_hex(self.settlement_fingerprint, field_name="settlement_fingerprint"),
        )
        excluded = _normalize_excluded_rating_entry_ids(self.excluded_rating_entry_ids)
        target_entry_ids = {entry.match_entry_id for entry in self.target.entries}
        if not set(excluded).issubset(target_entry_ids):
            raise ValueError("excluded_rating_entry_ids must reference the complete official Entry board.")
        object.__setattr__(self, "excluded_rating_entry_ids", excluded)
        if self.settlement_fingerprint != self.target.settlement_fingerprint(
            excluded_rating_entry_ids=excluded,
        ):
            raise ValueError("settlement_fingerprint does not match target authority.")
        odds = tuple(sorted(self.applied_odds, key=lambda item: tuple(BetType).index(item.bet_type)))
        if len({item.bet_type for item in odds}) != len(odds):
            raise ValueError("applied_odds must contain each available Bet type once.")
        expected_market_types = tuple(BetType)[: min(len(self.target.entries), len(BetType))]
        if tuple(item.bet_type for item in odds) != expected_market_types:
            raise ValueError("applied_odds must contain every available winning market.")
        expected_market_identities = _winning_market_identities(
            tuple((entry.match_entry_id, entry.entry_number) for entry in self.target.entries)
        )
        if (
            tuple((item.bet_type, item.selection_entry_ids, item.selection_entry_numbers) for item in odds)
            != expected_market_identities
        ):
            raise ValueError("applied_odds must identify the confirmed winning Entry set.")
        object.__setattr__(self, "applied_odds", odds)
        payouts = tuple(sorted(self.payouts, key=lambda item: item.persona_id))
        if len({item.persona_id for item in payouts}) != len(payouts):
            raise ValueError("payouts must contain at most one aggregate per Persona.")
        payout_bet_ids = tuple(bet_id for payout in payouts for bet_id in payout.bet_ids)
        if len(set(payout_bet_ids)) != len(payout_bet_ids) or not set(payout_bet_ids).issubset(
            {bet.bet_id for bet in self.target.active_bets}
        ):
            raise ValueError("payouts must reference distinct current active Bets.")
        object.__setattr__(self, "payouts", payouts)
        rewards = tuple(sorted(self.rewards, key=lambda item: item.persona_id))
        if len({item.persona_id for item in rewards}) != len(rewards):
            raise ValueError("rewards must contain at most one aggregate per Persona.")
        object.__setattr__(self, "rewards", rewards)
        ratings = tuple(sorted(self.ratings, key=lambda item: (item.rank, item.match_entry_id)))
        if (
            tuple(item.rank for item in ratings) != tuple(range(1, len(self.target.entries) + 1))
            or {item.match_entry_id for item in ratings} != {entry.match_entry_id for entry in self.target.entries}
            or tuple(item.game_account_id for item in ratings)
            != tuple(entry.game_account_id for entry in self.target.entries)
        ):
            raise ValueError("ratings must preserve the complete settlement Entry board.")
        excluded_from_ratings = tuple(
            item.match_entry_id for item in ratings if item.rating_disposition is MatchRatingDisposition.EXCLUDED
        )
        if tuple(sorted(excluded_from_ratings)) != excluded:
            raise ValueError("Rating dispositions must match excluded_rating_entry_ids.")
        rated = tuple(item for item in ratings if item.rating_disposition is MatchRatingDisposition.RATED)
        if len(rated) == 1:
            raise ValueError("A Rating participant board cannot contain exactly one Entry.")
        if len({item.game_account_id for item in rated}) != len(rated):
            raise ValueError("A GameAccount may have at most one rated Entry per Match.")
        if tuple(item.rating_rank for item in rated) != tuple(range(1, len(rated) + 1)):
            raise ValueError("Rated Entries must use contiguous derived Rating ranks.")
        if self.target.grade is MatchGrade.OP:
            if excluded or any(
                item.rating_disposition is not MatchRatingDisposition.NOT_APPLICABLE for item in ratings
            ):
                raise ValueError("OP Entries must be Rating-not-applicable without exclusions.")
        elif any(item.rating_disposition is MatchRatingDisposition.NOT_APPLICABLE for item in ratings):
            raise ValueError("Non-OP Entries cannot be Rating-not-applicable.")
        object.__setattr__(self, "ratings", ratings)
        wallet_deltas = tuple(sorted(self.wallet_deltas, key=lambda item: item.persona_id))
        if len({item.persona_id for item in wallet_deltas}) != len(wallet_deltas):
            raise ValueError("wallet_deltas must contain at most one transition per Persona.")
        credited_personas = {item.persona_id for item in payouts} | {item.persona_id for item in rewards}
        if {item.persona_id for item in wallet_deltas} != credited_personas:
            raise ValueError("wallet_deltas must cover every credited Persona exactly once.")
        object.__setattr__(
            self,
            "wallet_deltas",
            wallet_deltas,
        )

    @property
    def payout_total(self) -> int:
        return sum(payout.amount for payout in self.payouts)

    @property
    def reward_total(self) -> int:
        return sum(reward.amount for reward in self.rewards)


def build_match_settlement_plan(
    target: MatchSettlementTarget,
    *,
    excluded_rating_entry_ids: tuple[int, ...] = (),
) -> MatchSettlementPlan:
    """Calculate immutable winning odds, payouts, rewards, and Rating transitions."""

    if target.source_kind is not MatchSourceKind.NATIVE_V2 or target.status is not MatchStatus.RESULT_CONFIRMED:
        raise MatchSettlementUnavailableError("Settlement requires a native result-confirmed Match.")
    try:
        excluded_rating_entry_ids = _normalize_excluded_rating_entry_ids(excluded_rating_entry_ids)
    except ValueError as exc:
        raise MatchSettlementRatingSelectionError(str(exc)) from exc
    entry_ids = tuple(entry.match_entry_id for entry in target.entries)
    if not set(excluded_rating_entry_ids).issubset(entry_ids):
        raise MatchSettlementRatingSelectionError(
            "Every excluded Rating Entry must belong to the current official board."
        )
    if target.grade is MatchGrade.OP and excluded_rating_entry_ids:
        raise MatchSettlementRatingSelectionError("OP Entries are already Rating-not-applicable.")
    odds_projection = calculate_provisional_odds(
        entry_ids,
        tuple(bet.to_pool_stake() for bet in target.active_bets),
    )
    odds_by_key = {(odds.bet_type, odds.selection_ids): odds.odds for odds in odds_projection}
    entry_by_rank = {entry.rank: entry for entry in target.entries}
    winning_selections: list[tuple[BetType, tuple[int, ...]]] = [(BetType.WIN, (entry_by_rank[1].match_entry_id,))]
    if len(target.entries) >= 2:
        winning_selections.append(
            (
                BetType.QUINELLA,
                tuple(sorted((entry_by_rank[1].match_entry_id, entry_by_rank[2].match_entry_id))),
            )
        )
    if len(target.entries) >= 3:
        winning_selections.append(
            (
                BetType.TRIO,
                tuple(
                    sorted(
                        (
                            entry_by_rank[1].match_entry_id,
                            entry_by_rank[2].match_entry_id,
                            entry_by_rank[3].match_entry_id,
                        )
                    )
                ),
            )
        )
    number_by_id = {entry.match_entry_id: entry.entry_number for entry in target.entries}
    applied_odds = tuple(
        MatchSettlementAppliedOdds(
            bet_type=bet_type,
            selection_entry_ids=selection,
            selection_entry_numbers=tuple(sorted(number_by_id[entry_id] for entry_id in selection)),
            provisional_odds=odds_by_key[(bet_type, selection)],
            confirmed_odds=quantize_applied_odds(
                odds_by_key[(bet_type, selection)],
                field_size=len(target.entries),
            ),
        )
        for bet_type, selection in winning_selections
    )
    winning_odds = {(item.bet_type, item.selection_entry_ids): item.confirmed_odds for item in applied_odds}
    payout_bets: dict[str, list[tuple[int, int]]] = {}
    for bet in target.active_bets:
        confirmed_odds = winning_odds.get((bet.bet_type, bet.selection_entry_ids))
        if confirmed_odds is None:
            continue
        payout_bets.setdefault(bet.persona_id, []).append((bet.bet_id, calculate_payout(bet.amount, confirmed_odds)))
    payouts = tuple(
        MatchSettlementPayoutPlan(
            persona_id=persona_id,
            bet_ids=tuple(bet_id for bet_id, _ in values),
            amount=sum(amount for _, amount in values),
        )
        for persona_id, values in sorted(payout_bets.items())
    )

    rewards: tuple[MatchSettlementRewardPlan, ...]
    if target.grade is MatchGrade.OP:
        rewards = ()
    else:
        entries_by_persona: dict[str, list[MatchSettlementEntry]] = {}
        for entry in target.entries:
            entries_by_persona.setdefault(entry.owner_at_event_persona_id, []).append(entry)
        reward_plans: list[MatchSettlementRewardPlan] = []
        for persona_id, entries in sorted(entries_by_persona.items()):
            ordered = sorted(entries, key=lambda entry: (entry.rank, entry.match_entry_id))
            selected = ordered[0]
            reward_plans.append(
                MatchSettlementRewardPlan(
                    persona_id=persona_id,
                    selected_match_entry_id=selected.match_entry_id,
                    selected_game_account_id=selected.game_account_id,
                    selected_rank=selected.rank,
                    suppressed_match_entry_ids=tuple(entry.match_entry_id for entry in ordered[1:]),
                    amount=calculate_match_placement_reward(
                        grade=target.grade,
                        rank=selected.rank,
                        field_size=len(target.entries),
                    ),
                )
            )
        rewards = tuple(reward_plans)

    excluded_entry_ids = set(excluded_rating_entry_ids)
    eligible_entries = tuple(entry for entry in target.entries if entry.match_entry_id not in excluded_entry_ids)
    if target.grade is not MatchGrade.OP:
        if len(eligible_entries) == 1:
            raise MatchSettlementRatingSelectionError(
                "Rating participation must retain either zero or at least two Entries."
            )
        eligible_game_account_ids = tuple(entry.game_account_id for entry in eligible_entries)
        if len(set(eligible_game_account_ids)) != len(eligible_game_account_ids):
            raise MatchSettlementRatingSelectionError(
                "Select Rating exclusions so each GameAccount has at most one rated Entry."
            )

    if target.grade is MatchGrade.OP:
        zero = normalize_rating_storage(Decimal())
        ratings = tuple(
            MatchSettlementRatingPlan(
                match_entry_id=entry.match_entry_id,
                entry_number=entry.entry_number,
                game_account_id=entry.game_account_id,
                game_account_name=entry.game_account_name,
                horse_name=entry.horse_name,
                affiliation_at_event=entry.affiliation_at_event,
                rank=entry.rank,
                rating_disposition=MatchRatingDisposition.NOT_APPLICABLE,
                rating_rank=None,
                rating_before=entry.rating_before,
                base_delta=zero,
                adjustment_delta=zero,
                amount=zero,
                rating_after=entry.rating_before,
                transaction_required=False,
            )
            for entry in target.entries
        )
    elif not eligible_entries:
        zero = normalize_rating_storage(Decimal())
        ratings = tuple(
            MatchSettlementRatingPlan(
                match_entry_id=entry.match_entry_id,
                entry_number=entry.entry_number,
                game_account_id=entry.game_account_id,
                game_account_name=entry.game_account_name,
                horse_name=entry.horse_name,
                affiliation_at_event=entry.affiliation_at_event,
                rank=entry.rank,
                rating_disposition=MatchRatingDisposition.EXCLUDED,
                rating_rank=None,
                rating_before=entry.rating_before,
                base_delta=zero,
                adjustment_delta=zero,
                amount=zero,
                rating_after=entry.rating_before,
                transaction_required=False,
            )
            for entry in target.entries
        )
    else:
        try:
            drafts = calculate_rating_transactions(
                grade=target.grade,
                participants=tuple(
                    RatingParticipant(
                        match_entry_id=entry.match_entry_id,
                        game_account_id=entry.game_account_id,
                        rank=rating_rank,
                        rating_before=entry.rating_before,
                    )
                    for rating_rank, entry in enumerate(eligible_entries, start=1)
                ),
                rules=target.rating_rules,
            )
        except RatingRuleError as exc:
            raise MatchSettlementRuleUnavailableError(str(exc)) from exc
        except RatingCalculationError as exc:
            raise MatchSettlementInvalidSourceError(str(exc)) from exc
        draft_by_entry_id = {draft.match_entry_id: draft for draft in drafts}
        zero = normalize_rating_storage(Decimal())
        ratings = tuple(
            MatchSettlementRatingPlan(
                match_entry_id=entry.match_entry_id,
                entry_number=entry.entry_number,
                game_account_id=entry.game_account_id,
                game_account_name=entry.game_account_name,
                horse_name=entry.horse_name,
                affiliation_at_event=entry.affiliation_at_event,
                rank=entry.rank,
                rating_disposition=(
                    MatchRatingDisposition.EXCLUDED
                    if entry.match_entry_id in excluded_entry_ids
                    else MatchRatingDisposition.RATED
                ),
                rating_rank=(
                    None if entry.match_entry_id in excluded_entry_ids else draft_by_entry_id[entry.match_entry_id].rank
                ),
                rating_before=entry.rating_before,
                base_delta=(
                    zero
                    if entry.match_entry_id in excluded_entry_ids
                    else draft_by_entry_id[entry.match_entry_id].base_delta
                ),
                adjustment_delta=(
                    zero
                    if entry.match_entry_id in excluded_entry_ids
                    else draft_by_entry_id[entry.match_entry_id].adjustment_delta
                ),
                amount=(
                    zero
                    if entry.match_entry_id in excluded_entry_ids
                    else draft_by_entry_id[entry.match_entry_id].amount
                ),
                rating_after=(
                    entry.rating_before
                    if entry.match_entry_id in excluded_entry_ids
                    else draft_by_entry_id[entry.match_entry_id].rating_after
                ),
                transaction_required=entry.match_entry_id not in excluded_entry_ids,
            )
            for entry in target.entries
        )

    payout_by_persona = {payout.persona_id: payout.amount for payout in payouts}
    reward_by_persona = {reward.persona_id: reward.amount for reward in rewards}
    wallets = {wallet.persona_id: wallet for wallet in target.wallets}
    credited_personas = sorted(set(payout_by_persona) | set(reward_by_persona))
    wallet_deltas = tuple(
        MatchSettlementWalletDelta(
            persona_id=persona_id,
            balance_before=wallets[persona_id].balance,
            payout_amount=payout_by_persona.get(persona_id, 0),
            reward_amount=reward_by_persona.get(persona_id, 0),
            balance_after=calculate_next_circle_point_balance(
                wallets[persona_id].balance,
                payout_by_persona.get(persona_id, 0) + reward_by_persona.get(persona_id, 0),
            ),
        )
        for persona_id in credited_personas
    )
    return MatchSettlementPlan(
        target=target,
        settlement_fingerprint=target.settlement_fingerprint(
            excluded_rating_entry_ids=excluded_rating_entry_ids,
        ),
        excluded_rating_entry_ids=excluded_rating_entry_ids,
        applied_odds=applied_odds,
        payouts=payouts,
        rewards=rewards,
        ratings=ratings,
        wallet_deltas=wallet_deltas,
    )


@dataclass(frozen=True, slots=True)
class MatchSettlementPayout:
    persona_id: str
    bet_ids: tuple[int, ...]
    amount: int
    point_transaction_id: int

    def __post_init__(self) -> None:
        MatchSettlementPayoutPlan(self.persona_id, self.bet_ids, self.amount)
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        object.__setattr__(self, "bet_ids", tuple(sorted(self.bet_ids)))
        _require_positive_int(self.point_transaction_id, field_name="point_transaction_id")

    def to_payload(self) -> dict[str, object]:
        return {
            "persona_id": self.persona_id,
            "bet_ids": list(self.bet_ids),
            "amount": self.amount,
            "point_transaction_id": self.point_transaction_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> MatchSettlementPayout:
        return cls(
            persona_id=_payload_string(payload, "persona_id"),
            bet_ids=tuple(
                _require_positive_int(value, field_name="bet_id") for value in _payload_list(payload, "bet_ids")
            ),
            amount=_require_positive_int(payload["amount"], field_name="amount"),
            point_transaction_id=_require_positive_int(
                payload["point_transaction_id"],
                field_name="point_transaction_id",
            ),
        )


@dataclass(frozen=True, slots=True)
class MatchSettlementReward:
    persona_id: str
    selected_match_entry_id: int
    selected_game_account_id: int
    selected_rank: int
    suppressed_match_entry_ids: tuple[int, ...]
    amount: int
    point_transaction_id: int

    def __post_init__(self) -> None:
        MatchSettlementRewardPlan(
            self.persona_id,
            self.selected_match_entry_id,
            self.selected_game_account_id,
            self.selected_rank,
            self.suppressed_match_entry_ids,
            self.amount,
        )
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        object.__setattr__(self, "suppressed_match_entry_ids", tuple(sorted(self.suppressed_match_entry_ids)))
        _require_positive_int(self.point_transaction_id, field_name="point_transaction_id")

    def to_payload(self) -> dict[str, object]:
        return {
            "persona_id": self.persona_id,
            "selected_match_entry_id": self.selected_match_entry_id,
            "selected_game_account_id": self.selected_game_account_id,
            "selected_rank": self.selected_rank,
            "suppressed_match_entry_ids": list(self.suppressed_match_entry_ids),
            "amount": self.amount,
            "point_transaction_id": self.point_transaction_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> MatchSettlementReward:
        return cls(
            persona_id=_payload_string(payload, "persona_id"),
            selected_match_entry_id=_require_positive_int(
                payload["selected_match_entry_id"], field_name="selected_match_entry_id"
            ),
            selected_game_account_id=_require_positive_int(
                payload["selected_game_account_id"], field_name="selected_game_account_id"
            ),
            selected_rank=_require_positive_int(payload["selected_rank"], field_name="selected_rank"),
            suppressed_match_entry_ids=tuple(
                _require_positive_int(value, field_name="suppressed_match_entry_id")
                for value in _payload_list(payload, "suppressed_match_entry_ids")
            ),
            amount=_require_positive_int(payload["amount"], field_name="amount"),
            point_transaction_id=_require_positive_int(
                payload["point_transaction_id"], field_name="point_transaction_id"
            ),
        )


@dataclass(frozen=True, slots=True)
class MatchSettlementRating:
    match_entry_id: int
    entry_number: int
    game_account_id: int
    game_account_name: str
    horse_name: str
    affiliation_at_event: str | None
    rank: int
    rating_before: Decimal
    base_delta: Decimal
    adjustment_delta: Decimal
    amount: Decimal
    rating_after: Decimal
    rating_transaction_id: int | None
    rating_disposition: MatchRatingDisposition | None = None
    rating_rank: int | None = None

    def __post_init__(self) -> None:
        disposition = self.rating_disposition
        if disposition is None:
            disposition = (
                MatchRatingDisposition.RATED
                if self.rating_transaction_id is not None
                else MatchRatingDisposition.NOT_APPLICABLE
            )
        disposition = MatchRatingDisposition(disposition)
        rating_rank = self.rating_rank
        if rating_rank is None and disposition is MatchRatingDisposition.RATED:
            rating_rank = self.rank
        object.__setattr__(self, "rating_disposition", disposition)
        object.__setattr__(self, "rating_rank", rating_rank)
        MatchSettlementRatingPlan(
            match_entry_id=self.match_entry_id,
            entry_number=self.entry_number,
            game_account_id=self.game_account_id,
            game_account_name=self.game_account_name,
            horse_name=self.horse_name,
            affiliation_at_event=self.affiliation_at_event,
            rank=self.rank,
            rating_disposition=self.rating_disposition,
            rating_rank=self.rating_rank,
            rating_before=self.rating_before,
            base_delta=self.base_delta,
            adjustment_delta=self.adjustment_delta,
            amount=self.amount,
            rating_after=self.rating_after,
            transaction_required=self.rating_transaction_id is not None,
        )
        if self.rating_transaction_id is not None:
            _require_positive_int(self.rating_transaction_id, field_name="rating_transaction_id")

    def to_payload(self) -> dict[str, object]:
        return {
            "match_entry_id": self.match_entry_id,
            "entry_number": self.entry_number,
            "game_account_id": self.game_account_id,
            "game_account_name": self.game_account_name,
            "horse_name": self.horse_name,
            "affiliation_at_event": self.affiliation_at_event,
            "rank": self.rank,
            "rating_disposition": self.rating_disposition.value,
            "rating_rank": self.rating_rank,
            "rating_before": _decimal_text(self.rating_before),
            "base_delta": _decimal_text(self.base_delta),
            "adjustment_delta": _decimal_text(self.adjustment_delta),
            "amount": _decimal_text(self.amount),
            "rating_after": _decimal_text(self.rating_after),
            "rating_transaction_id": self.rating_transaction_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> MatchSettlementRating:
        transaction_id = payload.get("rating_transaction_id")
        raw_disposition = payload.get("rating_disposition")
        if raw_disposition is None:
            disposition = (
                MatchRatingDisposition.RATED if transaction_id is not None else MatchRatingDisposition.NOT_APPLICABLE
            )
        else:
            disposition = MatchRatingDisposition(_payload_string(payload, "rating_disposition"))
        raw_rating_rank = payload.get("rating_rank")
        if raw_disposition is None and disposition is MatchRatingDisposition.RATED:
            raw_rating_rank = payload["rank"]
        return cls(
            match_entry_id=_require_positive_int(payload["match_entry_id"], field_name="match_entry_id"),
            entry_number=_require_positive_int(payload["entry_number"], field_name="entry_number"),
            game_account_id=_require_positive_int(payload["game_account_id"], field_name="game_account_id"),
            game_account_name=_payload_string(payload, "game_account_name"),
            horse_name=_payload_string(payload, "horse_name"),
            affiliation_at_event=_payload_optional_string(payload, "affiliation_at_event", max_length=100),
            rank=_require_positive_int(payload["rank"], field_name="rank"),
            rating_disposition=disposition,
            rating_rank=(
                _require_positive_int(raw_rating_rank, field_name="rating_rank")
                if raw_rating_rank is not None
                else None
            ),
            rating_before=_payload_decimal(payload, "rating_before"),
            base_delta=_payload_decimal(payload, "base_delta"),
            adjustment_delta=_payload_decimal(payload, "adjustment_delta"),
            amount=_payload_decimal(payload, "amount"),
            rating_after=_payload_decimal(payload, "rating_after"),
            rating_transaction_id=(
                _require_positive_int(transaction_id, field_name="rating_transaction_id")
                if transaction_id is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class SettledMatch:
    """Committed settlement receipt and complete rollback/publication evidence."""

    match_id: int
    match_name: str
    previous_status: MatchStatus
    status: MatchStatus
    grade: MatchGrade
    settled_at: datetime
    result: MatchSettlementResultAuthority
    settlement_fingerprint: str
    active_bet_ids: tuple[int, ...]
    active_stake_total: int
    applied_odds: tuple[MatchSettlementAppliedOdds, ...]
    payouts: tuple[MatchSettlementPayout, ...]
    rewards: tuple[MatchSettlementReward, ...]
    rating_rule_version: MatchSettlementRuleReference | None
    ratings: tuple[MatchSettlementRating, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "previous_status", MatchStatus(self.previous_status))
        if self.previous_status is not MatchStatus.RESULT_CONFIRMED:
            raise ValueError("Settlement previous_status must be result_confirmed.")
        object.__setattr__(self, "status", MatchStatus(self.status))
        if self.status is not MatchStatus.SETTLED:
            raise ValueError("A settlement receipt must have settled status.")
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(self, "settled_at", normalize_utc_datetime(self.settled_at, field_name="settled_at"))
        if not isinstance(self.result, MatchSettlementResultAuthority):
            raise ValueError("result must be MatchSettlementResultAuthority.")
        object.__setattr__(
            self,
            "settlement_fingerprint",
            _sha256_hex(self.settlement_fingerprint, field_name="settlement_fingerprint"),
        )
        active_bet_ids = tuple(sorted(self.active_bet_ids))
        if len(set(active_bet_ids)) != len(active_bet_ids):
            raise ValueError("active_bet_ids must be unique.")
        for bet_id in active_bet_ids:
            _require_positive_int(bet_id, field_name="active_bet_id")
        object.__setattr__(self, "active_bet_ids", active_bet_ids)
        _require_non_negative_int(self.active_stake_total, field_name="active_stake_total")
        object.__setattr__(
            self,
            "applied_odds",
            tuple(sorted(self.applied_odds, key=lambda item: tuple(BetType).index(item.bet_type))),
        )
        if len({item.bet_type for item in self.applied_odds}) != len(self.applied_odds):
            raise ValueError("Settlement odds markets must be unique.")
        payouts = tuple(sorted(self.payouts, key=lambda item: item.persona_id))
        if len({item.persona_id for item in payouts}) != len(payouts):
            raise ValueError("Settlement payouts must be unique per Persona.")
        payout_bet_ids = tuple(bet_id for payout in payouts for bet_id in payout.bet_ids)
        if len(set(payout_bet_ids)) != len(payout_bet_ids) or not set(payout_bet_ids).issubset(active_bet_ids):
            raise ValueError("Settlement payouts must reference distinct active Bets.")
        object.__setattr__(self, "payouts", payouts)
        rewards = tuple(sorted(self.rewards, key=lambda item: item.persona_id))
        if len({item.persona_id for item in rewards}) != len(rewards):
            raise ValueError("Settlement rewards must be unique per Persona.")
        object.__setattr__(self, "rewards", rewards)
        ratings = tuple(sorted(self.ratings, key=lambda item: (item.rank, item.match_entry_id)))
        if not ratings or tuple(item.rank for item in ratings) != tuple(range(1, len(ratings) + 1)):
            raise ValueError("Settlement ratings must preserve one complete contiguous board.")
        if len({item.match_entry_id for item in ratings}) != len(ratings):
            raise ValueError("Settlement Rating Entry IDs must be unique.")
        rated = tuple(item for item in ratings if item.rating_disposition is MatchRatingDisposition.RATED)
        if len(rated) == 1 or len({item.game_account_id for item in rated}) != len(rated):
            raise ValueError("Settlement rated Entries must contain zero or distinct GameAccounts of at least two.")
        if tuple(item.rating_rank for item in rated) != tuple(range(1, len(rated) + 1)):
            raise ValueError("Settlement rated Entries must preserve contiguous derived ranks.")
        object.__setattr__(self, "ratings", ratings)
        expected_market_types = tuple(BetType)[: min(len(ratings), len(BetType))]
        if tuple(item.bet_type for item in self.applied_odds) != expected_market_types:
            raise ValueError("Settlement must preserve every available winning odds market.")
        expected_market_identities = _winning_market_identities(
            tuple((rating.match_entry_id, rating.entry_number) for rating in ratings)
        )
        if (
            tuple((item.bet_type, item.selection_entry_ids, item.selection_entry_numbers) for item in self.applied_odds)
            != expected_market_identities
        ):
            raise ValueError("Settlement odds must identify the confirmed winning Entry set.")
        point_transaction_ids = tuple(
            [payout.point_transaction_id for payout in payouts] + [reward.point_transaction_id for reward in rewards]
        )
        if len(set(point_transaction_ids)) != len(point_transaction_ids):
            raise ValueError("Settlement Point transaction IDs must be unique.")
        rating_transaction_ids = tuple(
            rating.rating_transaction_id for rating in ratings if rating.rating_transaction_id is not None
        )
        if len(set(rating_transaction_ids)) != len(rating_transaction_ids):
            raise ValueError("Settlement Rating transaction IDs must be unique.")
        if self.grade is MatchGrade.OP:
            if (
                rewards
                or self.rating_rule_version is not None
                or any(
                    rating.rating_disposition is not MatchRatingDisposition.NOT_APPLICABLE
                    or rating.rating_transaction_id is not None
                    for rating in self.ratings
                )
            ):
                raise ValueError("OP settlement must preserve empty reward and Rating transaction sets.")
        elif self.rating_rule_version is None or any(
            (rating.rating_disposition is MatchRatingDisposition.RATED) != (rating.rating_transaction_id is not None)
            or rating.rating_disposition is MatchRatingDisposition.NOT_APPLICABLE
            for rating in self.ratings
        ):
            raise ValueError("Non-OP settlement requires exact rated/excluded transaction provenance.")

    @property
    def payout_total(self) -> int:
        return sum(payout.amount for payout in self.payouts)

    @property
    def reward_total(self) -> int:
        return sum(reward.amount for reward in self.rewards)

    @property
    def point_transaction_ids(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                [payout.point_transaction_id for payout in self.payouts]
                + [reward.point_transaction_id for reward in self.rewards]
            )
        )

    @property
    def rating_transaction_ids(self) -> tuple[int, ...]:
        return tuple(
            rating.rating_transaction_id for rating in self.ratings if rating.rating_transaction_id is not None
        )

    @property
    def excluded_rating_entry_ids(self) -> tuple[int, ...]:
        return tuple(
            rating.match_entry_id
            for rating in self.ratings
            if rating.rating_disposition is MatchRatingDisposition.EXCLUDED
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_SETTLEMENT_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "previous_status": self.previous_status.value,
            "status": self.status.value,
            "grade": self.grade.value,
            "settled_at": self.settled_at.isoformat(),
            "result": self.result.to_payload(),
            "settlement_fingerprint": self.settlement_fingerprint,
            "odds": {
                "rule_version": WEIGHTED_ODDS_RULE_VERSION,
                "provisional_precision": format(PROVISIONAL_ODDS_QUANTUM, ".4f"),
                "applied_precision": format(APPLIED_ODDS_QUANTUM, ".1f"),
                "field_multiplier": "0.5" if len(self.ratings) < 9 else "1",
                "markets": [item.to_payload() for item in self.applied_odds],
            },
            "active_bet_ids": list(self.active_bet_ids),
            "active_stake_total": self.active_stake_total,
            "payouts": [payout.to_payload() for payout in self.payouts],
            "payout_point_transaction_ids": [payout.point_transaction_id for payout in self.payouts],
            "rewards": [reward.to_payload() for reward in self.rewards],
            "reward_point_transaction_ids": [reward.point_transaction_id for reward in self.rewards],
            "placement_reward_rule_version": PLACEMENT_REWARD_RULE_VERSION,
            "rating_formula_version": RATING_FORMULA_VERSION,
            "rating_rule_version": (
                self.rating_rule_version.to_payload() if self.rating_rule_version is not None else None
            ),
            "excluded_rating_entry_ids": list(self.excluded_rating_entry_ids),
            "ratings": [rating.to_payload() for rating in self.ratings],
            "rating_transaction_ids": list(self.rating_transaction_ids),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> SettledMatch:
        schema_version = payload.get("schema_version")
        if schema_version not in (1, MATCH_SETTLEMENT_AUDIT_SCHEMA_VERSION):
            raise ValueError("Unsupported Match settlement audit schema version.")
        odds = _payload_mapping(payload, "odds")
        if (
            odds.get("rule_version") != WEIGHTED_ODDS_RULE_VERSION
            or odds.get("provisional_precision") != format(PROVISIONAL_ODDS_QUANTUM, ".4f")
            or odds.get("applied_precision") != format(APPLIED_ODDS_QUANTUM, ".1f")
            or payload.get("placement_reward_rule_version") != PLACEMENT_REWARD_RULE_VERSION
            or payload.get("rating_formula_version") != RATING_FORMULA_VERSION
        ):
            raise ValueError("Stored Match settlement policy version is unsupported.")
        raw_rule = payload.get("rating_rule_version")
        if raw_rule is not None and not isinstance(raw_rule, Mapping):
            raise ValueError("rating_rule_version must be an object or null.")
        result = cls(
            match_id=_require_positive_int(payload["match_id"], field_name="match_id"),
            match_name=_payload_string(payload, "match_name"),
            previous_status=MatchStatus(_payload_string(payload, "previous_status")),
            status=MatchStatus(_payload_string(payload, "status")),
            grade=MatchGrade(_payload_string(payload, "grade")),
            settled_at=datetime.fromisoformat(_payload_string(payload, "settled_at")),
            result=MatchSettlementResultAuthority.from_payload(_payload_mapping(payload, "result")),
            settlement_fingerprint=_sha256_hex(payload["settlement_fingerprint"], field_name="settlement_fingerprint"),
            active_bet_ids=tuple(
                _require_positive_int(value, field_name="active_bet_id")
                for value in _payload_list(payload, "active_bet_ids")
            ),
            active_stake_total=_require_non_negative_int(
                payload["active_stake_total"], field_name="active_stake_total"
            ),
            applied_odds=tuple(
                MatchSettlementAppliedOdds.from_payload(value) for value in _payload_mapping_list(odds, "markets")
            ),
            payouts=tuple(
                MatchSettlementPayout.from_payload(value) for value in _payload_mapping_list(payload, "payouts")
            ),
            rewards=tuple(
                MatchSettlementReward.from_payload(value) for value in _payload_mapping_list(payload, "rewards")
            ),
            rating_rule_version=(MatchSettlementRuleReference.from_payload(raw_rule) if raw_rule is not None else None),
            ratings=tuple(
                MatchSettlementRating.from_payload(value) for value in _payload_mapping_list(payload, "ratings")
            ),
        )
        expected_payout_ids = [payout.point_transaction_id for payout in result.payouts]
        expected_reward_ids = [reward.point_transaction_id for reward in result.rewards]
        if payload.get("payout_point_transaction_ids") != expected_payout_ids:
            raise ValueError("Stored payout Point transaction IDs are incomplete.")
        if payload.get("reward_point_transaction_ids") != expected_reward_ids:
            raise ValueError("Stored reward Point transaction IDs are incomplete.")
        if payload.get("rating_transaction_ids") != list(result.rating_transaction_ids):
            raise ValueError("Stored Rating transaction IDs are incomplete.")
        if schema_version == MATCH_SETTLEMENT_AUDIT_SCHEMA_VERSION and payload.get("excluded_rating_entry_ids") != list(
            result.excluded_rating_entry_ids
        ):
            raise ValueError("Stored excluded Rating Entry IDs are incomplete.")
        expected_multiplier = "0.5" if len(result.ratings) < 9 else "1"
        if odds.get("field_multiplier") != expected_multiplier:
            raise ValueError("Stored field multiplier does not match the settled field size.")
        return result


@dataclass(frozen=True, slots=True)
class SettleMatch:
    """Final-confirm command bound to one settlement Preview fingerprint."""

    match_id: int
    expected_settlement_fingerprint: str
    excluded_rating_entry_ids: tuple[int, ...]
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str
    reason: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "expected_settlement_fingerprint",
            _sha256_hex(
                self.expected_settlement_fingerprint,
                field_name="expected_settlement_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "excluded_rating_entry_ids",
            _normalize_excluded_rating_entry_ids(self.excluded_rating_entry_ids),
        )
        for field_name, max_length, optional in (
            ("idempotency_key", 128, False),
            ("actor_discord_user_id", 32, False),
            ("guild_id", 32, False),
            ("reason", 255, True),
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
                "schema": "match-settlement-command-v2",
                "match_id": self.match_id,
                "expected_settlement_fingerprint": self.expected_settlement_fingerprint,
                "excluded_rating_entry_ids": list(self.excluded_rating_entry_ids),
                "guild_id": self.guild_id,
                "reason": self.reason,
            }
        )


@dataclass(frozen=True, slots=True)
class StoredMatchSettlementOperation:
    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    after_data: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class MatchSettlementTargetChoice:
    match_id: int
    match_name: str
    entry_count: int
    active_bet_count: int

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        _require_positive_int(self.entry_count, field_name="entry_count")
        _require_non_negative_int(self.active_bet_count, field_name="active_bet_count")


@dataclass(frozen=True, slots=True)
class MatchSettlementRatingSelectionEntry:
    """One official Entry exposed to the private Rating-selection step."""

    match_entry_id: int
    entry_number: int
    game_account_id: int
    game_account_name: str
    horse_name: str
    rank: int

    def __post_init__(self) -> None:
        for field_name in ("match_entry_id", "entry_number", "game_account_id", "rank"):
            _require_positive_int(getattr(self, field_name), field_name=field_name)
        object.__setattr__(
            self,
            "game_account_name",
            _normalized_string(self.game_account_name, field_name="game_account_name", max_length=100),
        )
        object.__setattr__(
            self,
            "horse_name",
            _normalized_string(self.horse_name, field_name="horse_name", max_length=100),
        )


@dataclass(frozen=True, slots=True)
class MatchSettlementRatingSelectionTarget:
    """Detached complete official board used only to author a selection draft."""

    match_id: int
    match_name: str
    grade: MatchGrade
    result: MatchSettlementResultAuthority
    entries: tuple[MatchSettlementRatingSelectionEntry, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        if not isinstance(self.result, MatchSettlementResultAuthority):
            raise ValueError("result must be MatchSettlementResultAuthority.")
        entries = tuple(sorted(self.entries, key=lambda item: (item.rank, item.match_entry_id)))
        if not entries or tuple(entry.rank for entry in entries) != tuple(range(1, len(entries) + 1)):
            raise ValueError("Rating selection requires one complete official board.")
        if len({entry.match_entry_id for entry in entries}) != len(entries):
            raise ValueError("Rating selection Entry IDs must be unique.")
        object.__setattr__(self, "entries", entries)

    @classmethod
    def from_settlement_target(cls, target: MatchSettlementTarget) -> MatchSettlementRatingSelectionTarget:
        return cls(
            match_id=target.match_id,
            match_name=target.match_name,
            grade=target.grade,
            result=target.result,
            entries=tuple(
                MatchSettlementRatingSelectionEntry(
                    match_entry_id=entry.match_entry_id,
                    entry_number=entry.entry_number,
                    game_account_id=entry.game_account_id,
                    game_account_name=entry.game_account_name,
                    horse_name=entry.horse_name,
                    rank=entry.rank,
                )
                for entry in target.entries
            ),
        )


class MatchSettlementRepository(Protocol):
    def lock_match(self, *, match_id: int) -> MatchSettlementLock | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredMatchSettlementOperation | None: ...

    def load_target(self, *, match_id: int, lock: bool) -> MatchSettlementTarget | None: ...

    def persist_settlement(
        self,
        *,
        command: SettleMatch,
        plan: MatchSettlementPlan,
        settled_at: datetime,
    ) -> SettledMatch: ...

    def load_result_publication_source(
        self,
        *,
        match_id: int,
        guild_id: str,
    ) -> MatchResultPublicationSource | None: ...

    def add_result_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> None: ...


class MatchSettlementQueryRepository(Protocol):
    def search_targets(self, *, search: str, limit: int) -> tuple[MatchSettlementTargetChoice, ...]: ...

    def load_target(self, *, match_id: int) -> MatchSettlementTarget | None: ...


class MatchSettlementUnitOfWork(UnitOfWork, Protocol):
    @property
    def match_settlement(self) -> MatchSettlementRepository: ...


class MatchSettlementQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def match_settlement_queries(self) -> MatchSettlementQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchSettlementQueries:
    query_runner: QueryRunner[MatchSettlementQueryUnitOfWork]

    def search_targets(self, *, search: str, limit: int = 25) -> tuple[MatchSettlementTargetChoice, ...]:
        if not isinstance(search, str):
            raise ValueError("search must be a string.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25.")
        normalized = search.strip()
        return self.query_runner.run(
            lambda unit_of_work: unit_of_work.match_settlement_queries.search_targets(
                search=normalized,
                limit=limit,
            )
        )

    def get_preview(
        self,
        *,
        match_id: int,
        excluded_rating_entry_ids: tuple[int, ...] = (),
    ) -> MatchSettlementPlan:
        _require_positive_int(match_id, field_name="match_id")
        excluded = _normalize_excluded_rating_entry_ids(excluded_rating_entry_ids)

        def query(unit_of_work: MatchSettlementQueryUnitOfWork) -> MatchSettlementPlan:
            try:
                target = unit_of_work.match_settlement_queries.load_target(match_id=match_id)
                if target is None:
                    raise MatchSettlementUnavailableError("Match settlement target does not exist.")
                return build_match_settlement_plan(
                    target,
                    excluded_rating_entry_ids=excluded,
                )
            except MatchSettlementError:
                raise
            except (TypeError, ValueError) as exc:
                raise MatchSettlementInvalidSourceError(
                    "Stored Match settlement Preview authority is malformed."
                ) from exc

        return self.query_runner.run(query)

    def get_rating_selection(self, *, match_id: int) -> MatchSettlementRatingSelectionTarget:
        _require_positive_int(match_id, field_name="match_id")

        def query(unit_of_work: MatchSettlementQueryUnitOfWork) -> MatchSettlementRatingSelectionTarget:
            try:
                target = unit_of_work.match_settlement_queries.load_target(match_id=match_id)
                if target is None:
                    raise MatchSettlementUnavailableError("Match settlement target does not exist.")
                if (
                    target.source_kind is not MatchSourceKind.NATIVE_V2
                    or target.status is not MatchStatus.RESULT_CONFIRMED
                ):
                    raise MatchSettlementUnavailableError("Settlement requires a native result-confirmed Match.")
                return MatchSettlementRatingSelectionTarget.from_settlement_target(target)
            except MatchSettlementError:
                raise
            except (TypeError, ValueError) as exc:
                raise MatchSettlementInvalidSourceError(
                    "Stored Match Rating-selection authority is malformed."
                ) from exc

        return self.query_runner.run(query)


@dataclass(frozen=True, slots=True)
class MatchSettlementCommands:
    command_runner: CommandRunner[MatchSettlementUnitOfWork]
    clock: Callable[[], datetime]

    def settle_match(self, command: SettleMatch) -> SettledMatch:
        return self.command_runner.run(lambda unit_of_work: self._settle(unit_of_work.match_settlement, command))

    def _settle(self, repository: MatchSettlementRepository, command: SettleMatch) -> SettledMatch:
        try:
            locked = repository.lock_match(match_id=command.match_id)
        except (TypeError, ValueError) as exc:
            raise MatchSettlementInvalidSourceError("Stored Match lock authority is malformed.") from exc
        if locked is None:
            raise MatchSettlementUnavailableError("Match does not exist.")
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)
        if locked.source_kind is not MatchSourceKind.NATIVE_V2 or locked.status is not MatchStatus.RESULT_CONFIRMED:
            raise MatchSettlementUnavailableError("Settlement requires a native result-confirmed Match.")
        try:
            target = repository.load_target(match_id=command.match_id, lock=True)
            if target is None:
                raise MatchSettlementUnavailableError("Match settlement target is unavailable.")
            plan = build_match_settlement_plan(
                target,
                excluded_rating_entry_ids=command.excluded_rating_entry_ids,
            )
        except MatchSettlementError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchSettlementInvalidSourceError("Stored Match settlement authority is malformed.") from exc
        if plan.settlement_fingerprint != command.expected_settlement_fingerprint:
            raise MatchSettlementStaleError(
                "Confirmed result, Bet pool, Rating, or Rating rule version changed after Preview."
            )
        settled_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        try:
            settled = repository.persist_settlement(command=command, plan=plan, settled_at=settled_at)
        except MatchSettlementError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchSettlementInvalidSourceError(
                "Match settlement did not produce complete canonical evidence."
            ) from exc
        if (
            settled.match_id != plan.target.match_id
            or settled.match_name != plan.target.match_name
            or settled.grade is not plan.target.grade
            or settled.result != plan.target.result
            or settled.settlement_fingerprint != plan.settlement_fingerprint
            or settled.active_bet_ids != tuple(bet.bet_id for bet in plan.target.active_bets)
            or settled.active_stake_total != plan.target.active_stake_total
            or settled.applied_odds != plan.applied_odds
            or settled.payout_total != plan.payout_total
            or settled.reward_total != plan.reward_total
            or tuple((item.persona_id, item.bet_ids, item.amount) for item in settled.payouts)
            != tuple((item.persona_id, item.bet_ids, item.amount) for item in plan.payouts)
            or tuple(
                (
                    item.persona_id,
                    item.selected_match_entry_id,
                    item.selected_game_account_id,
                    item.selected_rank,
                    item.suppressed_match_entry_ids,
                    item.amount,
                )
                for item in settled.rewards
            )
            != tuple(
                (
                    item.persona_id,
                    item.selected_match_entry_id,
                    item.selected_game_account_id,
                    item.selected_rank,
                    item.suppressed_match_entry_ids,
                    item.amount,
                )
                for item in plan.rewards
            )
            or tuple(
                (
                    item.match_entry_id,
                    item.entry_number,
                    item.game_account_id,
                    item.rank,
                    item.rating_disposition,
                    item.rating_rank,
                    item.rating_before,
                    item.base_delta,
                    item.adjustment_delta,
                    item.amount,
                    item.rating_after,
                )
                for item in settled.ratings
            )
            != tuple(
                (
                    item.match_entry_id,
                    item.entry_number,
                    item.game_account_id,
                    item.rank,
                    item.rating_disposition,
                    item.rating_rank,
                    item.rating_before,
                    item.base_delta,
                    item.adjustment_delta,
                    item.amount,
                    item.rating_after,
                )
                for item in plan.ratings
            )
        ):
            raise MatchSettlementInvalidSourceError("Stored settlement receipt does not match locked authority.")
        try:
            publication_source = repository.load_result_publication_source(
                match_id=settled.match_id,
                guild_id=command.guild_id,
            )
            if publication_source is None:
                raise ValueError("Settled Match result publication source is unavailable.")
            publication_intent = build_match_result_publication_intent(publication_source)
            repository.add_result_publication(intent=publication_intent, created_at=settled_at)
        except MatchSettlementError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchSettlementInvalidSourceError(
                "Match settlement did not produce complete result publication evidence."
            ) from exc
        return settled

    @staticmethod
    def _resolve_exact_retry(*, stored: StoredMatchSettlementOperation, command: SettleMatch) -> SettledMatch:
        if (
            stored.type != MatchSettlementAuditType.SETTLED.value
            or stored.request_fingerprint != command.request_fingerprint
            or stored.match_id != command.match_id
        ):
            raise MatchSettlementIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise MatchSettlementAuditError("Exact-retry settlement has no stored evidence bundle.")
        try:
            settled = SettledMatch.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchSettlementAuditError("Exact-retry settlement evidence is malformed.") from exc
        if (
            settled.match_id != command.match_id
            or settled.settlement_fingerprint != command.expected_settlement_fingerprint
        ):
            raise MatchSettlementAuditError("Exact-retry evidence belongs to another settlement.")
        return settled
