"""Terminal native V2 Match settlement rollback boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.application.publication import (
    MatchSettlementVoidedPublicationSource,
    PublicationIntent,
    build_match_settlement_voided_publication_intent,
)
from uma_st2.domain.match import MatchSourceKind, MatchStatus
from uma_st2.domain.point import calculate_next_circle_point_balance
from uma_st2.shared import normalize_utc_datetime

from .cancellation import MATCH_BET_REFUND_POINT_ACTION
from .settlement import (
    MATCH_BET_PAYOUT_POINT_ACTION,
    MATCH_PLACEMENT_REWARD_POINT_ACTION,
    SettledMatch,
)

MATCH_SETTLEMENT_ROLLBACK_AUDIT_SCHEMA_VERSION: Final = 1
MATCH_BET_PAYOUT_REVERSAL_POINT_ACTION: Final = "match_bet_payout_reversal"
MATCH_PLACEMENT_REWARD_REVERSAL_POINT_ACTION: Final = "match_placement_reward_reversal"
_MAX_CIRCLE_POINT_BALANCE: Final = (1 << 63) - 1


class MatchSettlementRollbackAuditType(StrEnum):
    """Canonical audit type for one terminal settlement compensation."""

    ROLLED_BACK = "match_settlement_rolled_back"


class MatchSettlementRollbackError(ValueError):
    """Base error for rejected terminal settlement rollback."""


class MatchSettlementRollbackUnavailableError(MatchSettlementRollbackError):
    """The selected Match is absent or no longer rollback-eligible."""


class MatchSettlementRollbackEvidenceExpiredError(MatchSettlementRollbackError):
    """The exact retained settlement evidence can no longer be compensated."""


class MatchSettlementRollbackStaleError(MatchSettlementRollbackError):
    """The retained evidence or wallet authority changed after Preview."""


class MatchSettlementRollbackBalanceError(MatchSettlementRollbackError):
    """The atomic compensation would leave an unsupported wallet balance."""


class MatchSettlementRollbackInvalidSourceError(MatchSettlementRollbackError):
    """Persisted rollback authority or a repository result is malformed."""


class MatchSettlementRollbackIdempotencyConflictError(MatchSettlementRollbackError):
    """An idempotency key is bound to another logical operation."""


class MatchSettlementRollbackAuditError(MatchSettlementRollbackError):
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


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("Decimal audit values must be finite.")
    return format(value, "f")


def _payload_string(payload: Mapping[str, object], key: str, *, max_length: int = 255) -> str:
    value = payload[key]
    normalized = _normalized_string(value, field_name=key, max_length=max_length)
    assert normalized is not None
    return normalized


def _payload_positive_int(payload: Mapping[str, object], key: str) -> int:
    return _require_positive_int(payload[key], field_name=key)  # type: ignore[arg-type]


def _payload_non_negative_int(payload: Mapping[str, object], key: str) -> int:
    return _require_non_negative_int(payload[key], field_name=key)  # type: ignore[arg-type]


def _payload_int(payload: Mapping[str, object], key: str) -> int:
    return _require_int(payload[key], field_name=key)  # type: ignore[arg-type]


def _payload_decimal(payload: Mapping[str, object], key: str) -> Decimal:
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a decimal string.")
    converted = Decimal(value)
    if not converted.is_finite():
        raise ValueError(f"{key} must be finite.")
    return converted


def _payload_list(payload: Mapping[str, object], key: str) -> list[object]:
    value = payload[key]
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list.")
    return value


def _payload_mapping_list(payload: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    values = _payload_list(payload, key)
    if any(not isinstance(value, Mapping) for value in values):
        raise ValueError(f"{key} must contain objects.")
    return tuple(values)  # type: ignore[return-value]


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _sha256_hex(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest.")
    return value


@dataclass(frozen=True, slots=True)
class MatchSettlementRollbackLock:
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
class MatchSettlementRollbackBet:
    bet_id: int
    persona_id: str
    amount: int

    def __post_init__(self) -> None:
        _require_positive_int(self.bet_id, field_name="bet_id")
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        _require_positive_int(self.amount, field_name="amount")


@dataclass(frozen=True, slots=True)
class MatchSettlementRollbackWallet:
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
class MatchSettlementOriginalPointTransaction:
    transaction_id: int
    operation_id: int | None
    persona_id: str
    action: str
    amount: int

    def __post_init__(self) -> None:
        _require_positive_int(self.transaction_id, field_name="transaction_id")
        if self.operation_id is not None:
            _require_positive_int(self.operation_id, field_name="operation_id")
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        object.__setattr__(
            self,
            "action",
            _normalized_string(self.action, field_name="action", max_length=32),
        )
        _require_positive_int(self.amount, field_name="amount")


@dataclass(frozen=True, slots=True)
class MatchSettlementOriginalRatingTransaction:
    transaction_id: int
    operation_id: int | None
    rating_rule_version_id: int
    match_entry_id: int
    game_account_id: int
    rating_before: Decimal
    amount: Decimal
    rating_after: Decimal
    current_rating: Decimal | None
    latest_transaction_id: int | None

    def __post_init__(self) -> None:
        for field_name in (
            "transaction_id",
            "rating_rule_version_id",
            "match_entry_id",
            "game_account_id",
        ):
            _require_positive_int(getattr(self, field_name), field_name=field_name)
        if self.operation_id is not None:
            _require_positive_int(self.operation_id, field_name="operation_id")
        if self.latest_transaction_id is not None:
            _require_positive_int(self.latest_transaction_id, field_name="latest_transaction_id")
        for field_name in ("rating_before", "amount", "rating_after"):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{field_name} must be a finite Decimal.")
        if self.current_rating is not None and (
            not isinstance(self.current_rating, Decimal) or not self.current_rating.is_finite()
        ):
            raise ValueError("current_rating must be a finite Decimal or None.")
        if self.rating_after != self.rating_before + self.amount:
            raise ValueError("Original Rating transaction is not balanced.")


@dataclass(frozen=True, slots=True)
class MatchSettlementRollbackTarget:
    match_id: int
    match_name: str
    source_kind: MatchSourceKind
    status: MatchStatus
    settlement_operation_id: int
    settlement: SettledMatch
    bets: tuple[MatchSettlementRollbackBet, ...]
    wallets: tuple[MatchSettlementRollbackWallet, ...]
    point_transactions: tuple[MatchSettlementOriginalPointTransaction, ...]
    rating_transactions: tuple[MatchSettlementOriginalRatingTransaction, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchStatus(self.status))
        _require_positive_int(self.settlement_operation_id, field_name="settlement_operation_id")
        if not isinstance(self.settlement, SettledMatch):
            raise ValueError("settlement must be SettledMatch.")
        bets = tuple(sorted(self.bets, key=lambda item: item.bet_id))
        if len({item.bet_id for item in bets}) != len(bets):
            raise ValueError("Rollback Bets must be unique.")
        object.__setattr__(self, "bets", bets)
        wallets = tuple(sorted(self.wallets, key=lambda item: item.persona_id))
        if len({item.persona_id for item in wallets}) != len(wallets):
            raise ValueError("Rollback wallets must be unique.")
        object.__setattr__(self, "wallets", wallets)
        point_transactions = tuple(sorted(self.point_transactions, key=lambda item: item.transaction_id))
        if len({item.transaction_id for item in point_transactions}) != len(point_transactions):
            raise ValueError("Original Point transaction IDs must be unique.")
        object.__setattr__(self, "point_transactions", point_transactions)
        rating_transactions = tuple(sorted(self.rating_transactions, key=lambda item: item.transaction_id))
        if len({item.transaction_id for item in rating_transactions}) != len(rating_transactions):
            raise ValueError("Original Rating transaction IDs must be unique.")
        if len({item.game_account_id for item in rating_transactions}) != len(rating_transactions):
            raise ValueError("Original Rating transactions must be unique per GameAccount.")
        object.__setattr__(self, "rating_transactions", rating_transactions)


@dataclass(frozen=True, slots=True)
class MatchSettlementRollbackWalletPlan:
    persona_id: str
    balance_before: int
    stake_refund: int
    payout_reversal: int
    reward_reversal: int
    balance_after: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        for field_name in (
            "balance_before",
            "stake_refund",
            "payout_reversal",
            "reward_reversal",
            "balance_after",
        ):
            _require_non_negative_int(getattr(self, field_name), field_name=field_name)
        expected = self.balance_before + self.stake_refund - self.payout_reversal - self.reward_reversal
        if self.balance_after != expected:
            raise ValueError("Rollback wallet balance does not match its complete compensation.")

    @property
    def net_delta(self) -> int:
        return self.stake_refund - self.payout_reversal - self.reward_reversal

    def to_payload(self) -> dict[str, object]:
        return {
            "persona_id": self.persona_id,
            "balance_before": self.balance_before,
            "stake_refund": self.stake_refund,
            "payout_reversal": self.payout_reversal,
            "reward_reversal": self.reward_reversal,
            "net_delta": self.net_delta,
            "balance_after": self.balance_after,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> MatchSettlementRollbackWalletPlan:
        result = cls(
            persona_id=_payload_string(payload, "persona_id", max_length=36),
            balance_before=_payload_non_negative_int(payload, "balance_before"),
            stake_refund=_payload_non_negative_int(payload, "stake_refund"),
            payout_reversal=_payload_non_negative_int(payload, "payout_reversal"),
            reward_reversal=_payload_non_negative_int(payload, "reward_reversal"),
            balance_after=_payload_non_negative_int(payload, "balance_after"),
        )
        if _payload_int(payload, "net_delta") != result.net_delta:
            raise ValueError("Stored rollback wallet net delta is inconsistent.")
        return result


@dataclass(frozen=True, slots=True)
class MatchSettlementRollbackRatingPlan:
    original_transaction_id: int
    rating_rule_version_id: int
    match_entry_id: int
    game_account_id: int
    rating_before: Decimal
    amount: Decimal
    rating_after: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "original_transaction_id",
            "rating_rule_version_id",
            "match_entry_id",
            "game_account_id",
        ):
            _require_positive_int(getattr(self, field_name), field_name=field_name)
        for field_name in ("rating_before", "amount", "rating_after"):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{field_name} must be a finite Decimal.")
        if self.rating_after != self.rating_before + self.amount:
            raise ValueError("Rollback Rating compensation is not balanced.")

    @property
    def compensation_before(self) -> Decimal:
        return self.rating_after

    @property
    def compensation_amount(self) -> Decimal:
        return -self.amount

    @property
    def compensation_after(self) -> Decimal:
        return self.rating_before

    def to_payload(self) -> dict[str, object]:
        return {
            "original_transaction_id": self.original_transaction_id,
            "rating_rule_version_id": self.rating_rule_version_id,
            "match_entry_id": self.match_entry_id,
            "game_account_id": self.game_account_id,
            "rating_before": _decimal_text(self.rating_before),
            "amount": _decimal_text(self.amount),
            "rating_after": _decimal_text(self.rating_after),
            "compensation_before": _decimal_text(self.compensation_before),
            "compensation_amount": _decimal_text(self.compensation_amount),
            "compensation_after": _decimal_text(self.compensation_after),
        }


@dataclass(frozen=True, slots=True)
class MatchSettlementRollbackPlan:
    target: MatchSettlementRollbackTarget
    rollback_fingerprint: str
    wallets: tuple[MatchSettlementRollbackWalletPlan, ...]
    ratings: tuple[MatchSettlementRollbackRatingPlan, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.target, MatchSettlementRollbackTarget):
            raise ValueError("target must be MatchSettlementRollbackTarget.")
        object.__setattr__(
            self,
            "rollback_fingerprint",
            _sha256_hex(self.rollback_fingerprint, field_name="rollback_fingerprint"),
        )
        wallets = tuple(sorted(self.wallets, key=lambda item: item.persona_id))
        if len({item.persona_id for item in wallets}) != len(wallets):
            raise ValueError("Rollback wallet plans must be unique per Persona.")
        object.__setattr__(self, "wallets", wallets)
        ratings = tuple(sorted(self.ratings, key=lambda item: item.original_transaction_id))
        if len({item.original_transaction_id for item in ratings}) != len(ratings):
            raise ValueError("Rollback Rating plans must be unique.")
        object.__setattr__(self, "ratings", ratings)

    @property
    def stake_refund_total(self) -> int:
        return sum(item.stake_refund for item in self.wallets)

    @property
    def payout_reversal_total(self) -> int:
        return sum(item.payout_reversal for item in self.wallets)

    @property
    def reward_reversal_total(self) -> int:
        return sum(item.reward_reversal for item in self.wallets)


def build_match_settlement_rollback_plan(
    target: MatchSettlementRollbackTarget,
) -> MatchSettlementRollbackPlan:
    """Validate one retained settlement bundle and build exact compensation."""

    if target.source_kind is not MatchSourceKind.NATIVE_V2 or target.status is not MatchStatus.SETTLED:
        raise MatchSettlementRollbackUnavailableError("Rollback requires one native settled Match.")
    settlement = target.settlement
    if (
        settlement.match_id != target.match_id
        or settlement.match_name != target.match_name
        or settlement.status is not MatchStatus.SETTLED
    ):
        raise MatchSettlementRollbackEvidenceExpiredError("operation evidence expired: settlement identity mismatch")

    expected_bet_ids = settlement.active_bet_ids
    if tuple(item.bet_id for item in target.bets) != expected_bet_ids:
        raise MatchSettlementRollbackEvidenceExpiredError("operation evidence expired: Bet coverage mismatch")
    if sum(item.amount for item in target.bets) != settlement.active_stake_total:
        raise MatchSettlementRollbackEvidenceExpiredError("operation evidence expired: Bet stake mismatch")

    expected_points = {
        **{
            item.point_transaction_id: (
                item.persona_id,
                MATCH_BET_PAYOUT_POINT_ACTION,
                item.amount,
            )
            for item in settlement.payouts
        },
        **{
            item.point_transaction_id: (
                item.persona_id,
                MATCH_PLACEMENT_REWARD_POINT_ACTION,
                item.amount,
            )
            for item in settlement.rewards
        },
    }
    if tuple(item.transaction_id for item in target.point_transactions) != tuple(sorted(expected_points)):
        raise MatchSettlementRollbackEvidenceExpiredError("operation evidence expired: Point coverage mismatch")
    for transaction in target.point_transactions:
        if (
            transaction.operation_id != target.settlement_operation_id
            or (
                transaction.persona_id,
                transaction.action,
                transaction.amount,
            )
            != expected_points[transaction.transaction_id]
        ):
            raise MatchSettlementRollbackEvidenceExpiredError(
                "operation evidence expired: Point transaction provenance mismatch"
            )

    expected_ratings = {
        item.rating_transaction_id: item for item in settlement.ratings if item.rating_transaction_id is not None
    }
    if tuple(item.transaction_id for item in target.rating_transactions) != tuple(sorted(expected_ratings)):
        raise MatchSettlementRollbackEvidenceExpiredError("operation evidence expired: Rating coverage mismatch")
    rule_version_id = settlement.rating_rule_version.version_id if settlement.rating_rule_version is not None else None
    rating_plans: list[MatchSettlementRollbackRatingPlan] = []
    for transaction in target.rating_transactions:
        expected = expected_ratings[transaction.transaction_id]
        if (
            rule_version_id is None
            or transaction.operation_id != target.settlement_operation_id
            or transaction.rating_rule_version_id != rule_version_id
            or transaction.match_entry_id != expected.match_entry_id
            or transaction.game_account_id != expected.game_account_id
            or transaction.rating_before != expected.rating_before
            or transaction.amount != expected.amount
            or transaction.rating_after != expected.rating_after
            or transaction.current_rating != expected.rating_after
            or transaction.latest_transaction_id != transaction.transaction_id
        ):
            raise MatchSettlementRollbackEvidenceExpiredError(
                "operation evidence expired: Rating transaction is missing, changed, or no longer latest"
            )
        rating_plans.append(
            MatchSettlementRollbackRatingPlan(
                original_transaction_id=transaction.transaction_id,
                rating_rule_version_id=transaction.rating_rule_version_id,
                match_entry_id=transaction.match_entry_id,
                game_account_id=transaction.game_account_id,
                rating_before=transaction.rating_before,
                amount=transaction.amount,
                rating_after=transaction.rating_after,
            )
        )

    bet_persona = {item.bet_id: item.persona_id for item in target.bets}
    for payout in settlement.payouts:
        if any(bet_persona.get(bet_id) != payout.persona_id for bet_id in payout.bet_ids):
            raise MatchSettlementRollbackEvidenceExpiredError(
                "operation evidence expired: payout Bet ownership mismatch"
            )

    affected_persona_ids = sorted(
        {item.persona_id for item in target.bets}
        | {item.persona_id for item in settlement.payouts}
        | {item.persona_id for item in settlement.rewards}
    )
    wallet_by_persona = {item.persona_id: item for item in target.wallets}
    if tuple(wallet_by_persona) != tuple(affected_persona_ids):
        raise MatchSettlementRollbackEvidenceExpiredError("operation evidence expired: wallet coverage mismatch")

    refunds: dict[str, int] = dict.fromkeys(affected_persona_ids, 0)
    payout_reversals: dict[str, int] = dict.fromkeys(affected_persona_ids, 0)
    reward_reversals: dict[str, int] = dict.fromkeys(affected_persona_ids, 0)
    for bet in target.bets:
        refunds[bet.persona_id] += bet.amount
    for payout in settlement.payouts:
        payout_reversals[payout.persona_id] += payout.amount
    for reward in settlement.rewards:
        reward_reversals[reward.persona_id] += reward.amount

    wallet_plans: list[MatchSettlementRollbackWalletPlan] = []
    for persona_id in affected_persona_ids:
        wallet = wallet_by_persona[persona_id]
        delta = refunds[persona_id] - payout_reversals[persona_id] - reward_reversals[persona_id]
        balance_after = calculate_next_circle_point_balance(wallet.balance, delta)
        if not 0 <= balance_after <= _MAX_CIRCLE_POINT_BALANCE:
            raise MatchSettlementRollbackBalanceError(
                "Settlement rollback would exceed the supported Circle Point balance range."
            )
        wallet_plans.append(
            MatchSettlementRollbackWalletPlan(
                persona_id=persona_id,
                balance_before=wallet.balance,
                stake_refund=refunds[persona_id],
                payout_reversal=payout_reversals[persona_id],
                reward_reversal=reward_reversals[persona_id],
                balance_after=balance_after,
            )
        )

    fingerprint = _fingerprint(
        {
            "schema": "match-settlement-rollback-preview-v1",
            "match_id": target.match_id,
            "settlement_operation_id": target.settlement_operation_id,
            "settlement_fingerprint": settlement.settlement_fingerprint,
            "bets": [
                {"bet_id": item.bet_id, "persona_id": item.persona_id, "amount": item.amount} for item in target.bets
            ],
            "original_point_transaction_ids": [item.transaction_id for item in target.point_transactions],
            "wallets": [item.to_payload() for item in wallet_plans],
            "ratings": [item.to_payload() for item in rating_plans],
        }
    )
    return MatchSettlementRollbackPlan(
        target=target,
        rollback_fingerprint=fingerprint,
        wallets=tuple(wallet_plans),
        ratings=tuple(rating_plans),
    )


@dataclass(frozen=True, slots=True)
class MatchSettlementPointCompensation:
    persona_id: str
    action: str
    amount: int
    point_transaction_id: int
    original_point_transaction_id: int | None = None
    refunded_bet_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "persona_id",
            _normalized_string(self.persona_id, field_name="persona_id", max_length=36),
        )
        object.__setattr__(
            self,
            "action",
            _normalized_string(self.action, field_name="action", max_length=32),
        )
        _require_int(self.amount, field_name="amount")
        if self.amount == 0:
            raise ValueError("Point compensation amount cannot be zero.")
        _require_positive_int(self.point_transaction_id, field_name="point_transaction_id")
        if self.original_point_transaction_id is not None:
            _require_positive_int(
                self.original_point_transaction_id,
                field_name="original_point_transaction_id",
            )
        refunded_bet_ids = tuple(sorted(self.refunded_bet_ids))
        if len(set(refunded_bet_ids)) != len(refunded_bet_ids):
            raise ValueError("refunded_bet_ids must be unique.")
        for bet_id in refunded_bet_ids:
            _require_positive_int(bet_id, field_name="refunded_bet_id")
        object.__setattr__(self, "refunded_bet_ids", refunded_bet_ids)
        if self.action == MATCH_BET_REFUND_POINT_ACTION:
            if self.amount <= 0 or self.original_point_transaction_id is not None or not refunded_bet_ids:
                raise ValueError("Stake refund compensation provenance is incomplete.")
        elif self.action in (
            MATCH_BET_PAYOUT_REVERSAL_POINT_ACTION,
            MATCH_PLACEMENT_REWARD_REVERSAL_POINT_ACTION,
        ):
            if self.amount >= 0 or self.original_point_transaction_id is None or refunded_bet_ids:
                raise ValueError("Point reversal compensation provenance is incomplete.")
        else:
            raise ValueError("Unsupported settlement rollback Point action.")

    def to_payload(self) -> dict[str, object]:
        return {
            "persona_id": self.persona_id,
            "action": self.action,
            "amount": self.amount,
            "point_transaction_id": self.point_transaction_id,
            "original_point_transaction_id": self.original_point_transaction_id,
            "refunded_bet_ids": list(self.refunded_bet_ids),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> MatchSettlementPointCompensation:
        original_id = payload.get("original_point_transaction_id")
        return cls(
            persona_id=_payload_string(payload, "persona_id", max_length=36),
            action=_payload_string(payload, "action", max_length=32),
            amount=_payload_int(payload, "amount"),
            point_transaction_id=_payload_positive_int(payload, "point_transaction_id"),
            original_point_transaction_id=(
                _require_positive_int(original_id, field_name="original_point_transaction_id")
                if original_id is not None
                else None
            ),
            refunded_bet_ids=tuple(
                _require_positive_int(value, field_name="refunded_bet_id")
                for value in _payload_list(payload, "refunded_bet_ids")
            ),
        )


@dataclass(frozen=True, slots=True)
class MatchSettlementRatingCompensation:
    original_rating_transaction_id: int
    compensation_rating_transaction_id: int
    rating_rule_version_id: int
    match_entry_id: int
    game_account_id: int
    rating_before: Decimal
    amount: Decimal
    rating_after: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "original_rating_transaction_id",
            "compensation_rating_transaction_id",
            "rating_rule_version_id",
            "match_entry_id",
            "game_account_id",
        ):
            _require_positive_int(getattr(self, field_name), field_name=field_name)
        for field_name in ("rating_before", "amount", "rating_after"):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{field_name} must be a finite Decimal.")
        if self.rating_after != self.rating_before + self.amount:
            raise ValueError("Rating compensation is not balanced.")

    def to_payload(self) -> dict[str, object]:
        return {
            "original_rating_transaction_id": self.original_rating_transaction_id,
            "compensation_rating_transaction_id": self.compensation_rating_transaction_id,
            "rating_rule_version_id": self.rating_rule_version_id,
            "match_entry_id": self.match_entry_id,
            "game_account_id": self.game_account_id,
            "rating_before": _decimal_text(self.rating_before),
            "amount": _decimal_text(self.amount),
            "rating_after": _decimal_text(self.rating_after),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> MatchSettlementRatingCompensation:
        return cls(
            original_rating_transaction_id=_payload_positive_int(payload, "original_rating_transaction_id"),
            compensation_rating_transaction_id=_payload_positive_int(
                payload,
                "compensation_rating_transaction_id",
            ),
            rating_rule_version_id=_payload_positive_int(payload, "rating_rule_version_id"),
            match_entry_id=_payload_positive_int(payload, "match_entry_id"),
            game_account_id=_payload_positive_int(payload, "game_account_id"),
            rating_before=_payload_decimal(payload, "rating_before"),
            amount=_payload_decimal(payload, "amount"),
            rating_after=_payload_decimal(payload, "rating_after"),
        )


@dataclass(frozen=True, slots=True)
class RolledBackMatchSettlement:
    match_id: int
    match_name: str
    previous_status: MatchStatus
    status: MatchStatus
    reason: str
    rolled_back_at: datetime
    settlement_operation_id: int
    settlement_fingerprint: str
    cancelled_bet_ids: tuple[int, ...]
    wallets: tuple[MatchSettlementRollbackWalletPlan, ...]
    point_compensations: tuple[MatchSettlementPointCompensation, ...]
    rating_compensations: tuple[MatchSettlementRatingCompensation, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "previous_status", MatchStatus(self.previous_status))
        if self.previous_status is not MatchStatus.SETTLED:
            raise ValueError("Settlement rollback must start from settled.")
        object.__setattr__(self, "status", MatchStatus(self.status))
        if self.status is not MatchStatus.VOIDED:
            raise ValueError("Settlement rollback must produce terminal voided status.")
        object.__setattr__(
            self,
            "reason",
            _normalized_string(self.reason, field_name="reason", max_length=255),
        )
        object.__setattr__(
            self,
            "rolled_back_at",
            normalize_utc_datetime(self.rolled_back_at, field_name="rolled_back_at"),
        )
        _require_positive_int(self.settlement_operation_id, field_name="settlement_operation_id")
        object.__setattr__(
            self,
            "settlement_fingerprint",
            _sha256_hex(self.settlement_fingerprint, field_name="settlement_fingerprint"),
        )
        cancelled_bet_ids = tuple(sorted(self.cancelled_bet_ids))
        if len(set(cancelled_bet_ids)) != len(cancelled_bet_ids):
            raise ValueError("cancelled_bet_ids must be unique.")
        for bet_id in cancelled_bet_ids:
            _require_positive_int(bet_id, field_name="cancelled_bet_id")
        object.__setattr__(self, "cancelled_bet_ids", cancelled_bet_ids)
        wallets = tuple(sorted(self.wallets, key=lambda item: item.persona_id))
        if len({item.persona_id for item in wallets}) != len(wallets):
            raise ValueError("Rollback receipt wallets must be unique.")
        object.__setattr__(self, "wallets", wallets)
        point_compensations = tuple(sorted(self.point_compensations, key=lambda item: item.point_transaction_id))
        if len({item.point_transaction_id for item in point_compensations}) != len(point_compensations):
            raise ValueError("Point compensation IDs must be unique.")
        original_point_ids = tuple(
            item.original_point_transaction_id
            for item in point_compensations
            if item.original_point_transaction_id is not None
        )
        if len(set(original_point_ids)) != len(original_point_ids):
            raise ValueError("Original Point compensation references must be unique.")
        refunded_bet_ids = tuple(bet_id for item in point_compensations for bet_id in item.refunded_bet_ids)
        if tuple(sorted(refunded_bet_ids)) != cancelled_bet_ids:
            raise ValueError("Stake refund compensation must cover every cancelled Bet exactly once.")
        object.__setattr__(self, "point_compensations", point_compensations)
        rating_compensations = tuple(
            sorted(self.rating_compensations, key=lambda item: item.original_rating_transaction_id)
        )
        if len({item.original_rating_transaction_id for item in rating_compensations}) != len(
            rating_compensations
        ) or len({item.compensation_rating_transaction_id for item in rating_compensations}) != len(
            rating_compensations
        ):
            raise ValueError("Rating compensation transaction IDs must be unique.")
        object.__setattr__(self, "rating_compensations", rating_compensations)

    @property
    def point_transaction_ids(self) -> tuple[int, ...]:
        return tuple(item.point_transaction_id for item in self.point_compensations)

    @property
    def rating_transaction_ids(self) -> tuple[int, ...]:
        return tuple(item.compensation_rating_transaction_id for item in self.rating_compensations)

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_SETTLEMENT_ROLLBACK_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "previous_status": self.previous_status.value,
            "status": self.status.value,
            "reason": self.reason,
            "rolled_back_at": self.rolled_back_at.isoformat(),
            "settlement_operation_id": self.settlement_operation_id,
            "settlement_fingerprint": self.settlement_fingerprint,
            "cancelled_bet_ids": list(self.cancelled_bet_ids),
            "wallets": [item.to_payload() for item in self.wallets],
            "point_compensations": [item.to_payload() for item in self.point_compensations],
            "point_compensation_transaction_ids": list(self.point_transaction_ids),
            "rating_compensations": [item.to_payload() for item in self.rating_compensations],
            "rating_compensation_transaction_ids": list(self.rating_transaction_ids),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> RolledBackMatchSettlement:
        if payload.get("schema_version") != MATCH_SETTLEMENT_ROLLBACK_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Match settlement rollback audit schema version.")
        result = cls(
            match_id=_payload_positive_int(payload, "match_id"),
            match_name=_payload_string(payload, "match_name", max_length=200),
            previous_status=MatchStatus(_payload_string(payload, "previous_status", max_length=32)),
            status=MatchStatus(_payload_string(payload, "status", max_length=32)),
            reason=_payload_string(payload, "reason", max_length=255),
            rolled_back_at=datetime.fromisoformat(_payload_string(payload, "rolled_back_at", max_length=64)),
            settlement_operation_id=_payload_positive_int(payload, "settlement_operation_id"),
            settlement_fingerprint=_sha256_hex(
                payload["settlement_fingerprint"],
                field_name="settlement_fingerprint",
            ),
            cancelled_bet_ids=tuple(
                _require_positive_int(value, field_name="cancelled_bet_id")
                for value in _payload_list(payload, "cancelled_bet_ids")
            ),
            wallets=tuple(
                MatchSettlementRollbackWalletPlan.from_payload(item)
                for item in _payload_mapping_list(payload, "wallets")
            ),
            point_compensations=tuple(
                MatchSettlementPointCompensation.from_payload(item)
                for item in _payload_mapping_list(payload, "point_compensations")
            ),
            rating_compensations=tuple(
                MatchSettlementRatingCompensation.from_payload(item)
                for item in _payload_mapping_list(payload, "rating_compensations")
            ),
        )
        if payload.get("point_compensation_transaction_ids") != list(result.point_transaction_ids):
            raise ValueError("Stored Point compensation transaction coverage is incomplete.")
        if payload.get("rating_compensation_transaction_ids") != list(result.rating_transaction_ids):
            raise ValueError("Stored Rating compensation transaction coverage is incomplete.")
        return result


@dataclass(frozen=True, slots=True)
class RollbackMatchSettlement:
    match_id: int
    expected_rollback_fingerprint: str
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str
    reason: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "expected_rollback_fingerprint",
            _sha256_hex(
                self.expected_rollback_fingerprint,
                field_name="expected_rollback_fingerprint",
            ),
        )
        for field_name, max_length, optional in (
            ("idempotency_key", 128, False),
            ("actor_discord_user_id", 32, False),
            ("guild_id", 32, False),
            ("reason", 255, False),
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
                "schema": "match-settlement-rollback-command-v1",
                "match_id": self.match_id,
                "expected_rollback_fingerprint": self.expected_rollback_fingerprint,
                "guild_id": self.guild_id,
                "reason": self.reason,
            }
        )


@dataclass(frozen=True, slots=True)
class StoredMatchSettlementRollbackOperation:
    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    after_data: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class MatchSettlementRollbackTargetChoice:
    match_id: int
    match_name: str
    settled_at: datetime
    settled_bet_count: int

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(
            self,
            "settled_at",
            normalize_utc_datetime(self.settled_at, field_name="settled_at"),
        )
        _require_non_negative_int(self.settled_bet_count, field_name="settled_bet_count")


class MatchSettlementRollbackRepository(Protocol):
    def lock_match(self, *, match_id: int) -> MatchSettlementRollbackLock | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredMatchSettlementRollbackOperation | None: ...

    def load_target(self, *, match_id: int, lock: bool) -> MatchSettlementRollbackTarget | None: ...

    def persist_rollback(
        self,
        *,
        command: RollbackMatchSettlement,
        plan: MatchSettlementRollbackPlan,
        rolled_back_at: datetime,
    ) -> RolledBackMatchSettlement: ...

    def load_rollback_publication_source(
        self,
        *,
        match_id: int,
        guild_id: str,
    ) -> MatchSettlementVoidedPublicationSource | None: ...

    def add_rollback_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> None: ...


class MatchSettlementRollbackQueryRepository(Protocol):
    def search_targets(self, *, search: str, limit: int) -> tuple[MatchSettlementRollbackTargetChoice, ...]: ...

    def load_target(self, *, match_id: int) -> MatchSettlementRollbackTarget | None: ...


class MatchSettlementRollbackUnitOfWork(UnitOfWork, Protocol):
    @property
    def match_settlement_rollback(self) -> MatchSettlementRollbackRepository: ...


class MatchSettlementRollbackQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def match_settlement_rollback_queries(self) -> MatchSettlementRollbackQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchSettlementRollbackQueries:
    query_runner: QueryRunner[MatchSettlementRollbackQueryUnitOfWork]

    def search_targets(self, *, search: str, limit: int = 25) -> tuple[MatchSettlementRollbackTargetChoice, ...]:
        if not isinstance(search, str):
            raise ValueError("search must be a string.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25.")
        normalized = search.strip()
        return self.query_runner.run(
            lambda unit_of_work: unit_of_work.match_settlement_rollback_queries.search_targets(
                search=normalized,
                limit=limit,
            )
        )

    def get_preview(self, *, match_id: int) -> MatchSettlementRollbackPlan:
        _require_positive_int(match_id, field_name="match_id")

        def query(unit_of_work: MatchSettlementRollbackQueryUnitOfWork) -> MatchSettlementRollbackPlan:
            try:
                target = unit_of_work.match_settlement_rollback_queries.load_target(match_id=match_id)
                if target is None:
                    raise MatchSettlementRollbackUnavailableError("Settlement rollback target does not exist.")
                return build_match_settlement_rollback_plan(target)
            except MatchSettlementRollbackError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise MatchSettlementRollbackInvalidSourceError(
                    "Stored Match settlement rollback Preview authority is malformed."
                ) from exc

        return self.query_runner.run(query)


@dataclass(frozen=True, slots=True)
class MatchSettlementRollbackCommands:
    command_runner: CommandRunner[MatchSettlementRollbackUnitOfWork]
    clock: Callable[[], datetime]

    def rollback_settlement(self, command: RollbackMatchSettlement) -> RolledBackMatchSettlement:
        return self.command_runner.run(
            lambda unit_of_work: self._rollback(
                unit_of_work.match_settlement_rollback,
                command,
            )
        )

    def _rollback(
        self,
        repository: MatchSettlementRollbackRepository,
        command: RollbackMatchSettlement,
    ) -> RolledBackMatchSettlement:
        try:
            locked = repository.lock_match(match_id=command.match_id)
        except (TypeError, ValueError) as exc:
            raise MatchSettlementRollbackInvalidSourceError("Stored Match lock authority is malformed.") from exc
        if locked is None:
            raise MatchSettlementRollbackUnavailableError("Match does not exist.")
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)
        if locked.source_kind is not MatchSourceKind.NATIVE_V2 or locked.status is not MatchStatus.SETTLED:
            raise MatchSettlementRollbackUnavailableError("Rollback requires one native settled Match.")
        try:
            target = repository.load_target(match_id=command.match_id, lock=True)
            if target is None:
                raise MatchSettlementRollbackUnavailableError("Settlement rollback target is unavailable.")
            plan = build_match_settlement_rollback_plan(target)
        except MatchSettlementRollbackError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchSettlementRollbackInvalidSourceError(
                "Stored Match settlement rollback authority is malformed."
            ) from exc
        if plan.rollback_fingerprint != command.expected_rollback_fingerprint:
            raise MatchSettlementRollbackStaleError(
                "Settlement evidence, wallet, or Rating authority changed after Preview."
            )
        rolled_back_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        try:
            result = repository.persist_rollback(
                command=command,
                plan=plan,
                rolled_back_at=rolled_back_at,
            )
        except MatchSettlementRollbackError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchSettlementRollbackInvalidSourceError(
                "Settlement rollback did not produce complete canonical evidence."
            ) from exc
        if (
            result.match_id != plan.target.match_id
            or result.match_name != plan.target.match_name
            or result.previous_status is not MatchStatus.SETTLED
            or result.status is not MatchStatus.VOIDED
            or result.reason != command.reason
            or result.settlement_operation_id != plan.target.settlement_operation_id
            or result.settlement_fingerprint != plan.target.settlement.settlement_fingerprint
            or result.cancelled_bet_ids != tuple(item.bet_id for item in plan.target.bets)
            or result.wallets != plan.wallets
            or tuple(item.original_rating_transaction_id for item in result.rating_compensations)
            != tuple(item.original_transaction_id for item in plan.ratings)
        ):
            raise MatchSettlementRollbackInvalidSourceError(
                "Stored settlement rollback receipt does not match locked authority."
            )
        try:
            publication_source = repository.load_rollback_publication_source(
                match_id=result.match_id,
                guild_id=command.guild_id,
            )
            if publication_source is None:
                raise ValueError("Voided Match rollback publication source is unavailable.")
            publication_intent = build_match_settlement_voided_publication_intent(publication_source)
            repository.add_rollback_publication(intent=publication_intent, created_at=rolled_back_at)
        except MatchSettlementRollbackError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchSettlementRollbackInvalidSourceError(
                "Settlement rollback did not produce complete correction publication evidence."
            ) from exc
        return result

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredMatchSettlementRollbackOperation,
        command: RollbackMatchSettlement,
    ) -> RolledBackMatchSettlement:
        if (
            stored.type != MatchSettlementRollbackAuditType.ROLLED_BACK.value
            or stored.request_fingerprint != command.request_fingerprint
            or stored.match_id != command.match_id
        ):
            raise MatchSettlementRollbackIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise MatchSettlementRollbackAuditError("Exact-retry rollback has no stored evidence bundle.")
        try:
            result = RolledBackMatchSettlement.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchSettlementRollbackAuditError("Exact-retry rollback evidence is malformed.") from exc
        if result.match_id != command.match_id or result.reason != command.reason:
            raise MatchSettlementRollbackAuditError("Exact-retry evidence belongs to another rollback.")
        return result
