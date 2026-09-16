"""Pure domain calculations for betting rules.

This module intentionally owns only deterministic calculation/validation logic and has no
external I/O or persistence dependencies.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from enum import StrEnum
from itertools import combinations
from math import comb
from typing import Final

from .errors import BettingOddsError, BettingSelectionError, BettingStakeError

WEIGHTED_ODDS_RULE_VERSION: Final = "room_match_weighted_odds_v2_field_bonus"
PROVISIONAL_ODDS_QUANTUM: Final = Decimal("0.0001")
APPLIED_ODDS_QUANTUM: Final = Decimal("0.1")
BET_STAKE_UNIT: Final = 10
BET_STAKE_CAP_PERCENT: Final = 10
_ODDS_CALCULATION_PRECISION: Final = 50
_PERCENT_DENOMINATOR: Final = 100


class BetType(StrEnum):
    """Supported bet types for this kernel."""

    WIN = "win"
    QUINELLA = "quinella"
    TRIO = "trio"


class BetStatus(StrEnum):
    """Canonical lifecycle values for a persisted Bet."""

    ACTIVE = "active"
    SETTLED = "settled"
    CANCELLED = "cancelled"


REQUIRED_SELECTION_COUNTS = {
    BetType.WIN: 1,
    BetType.QUINELLA: 2,
    BetType.TRIO: 3,
}


def validate_new_bet_stake(stake: int) -> int:
    """Validate a newly created bet stake as a positive multiple of 10."""

    _validate_stake_value(stake)
    if stake <= 0 or stake % BET_STAKE_UNIT != 0:
        raise BettingStakeError("New bet stake must be a positive multiple of 10.")
    return stake


def calculate_bet_stake_cap(reference_balance: int) -> int:
    """Return the 10-unit-aligned per-operation cap for a locked balance."""

    if not isinstance(reference_balance, int) or isinstance(reference_balance, bool) or reference_balance < 0:
        raise BettingStakeError("Reference balance must be a non-negative integer amount.")
    percentage_cap = reference_balance * BET_STAKE_CAP_PERCENT // _PERCENT_DENOMINATOR
    unit_aligned_cap = percentage_cap // BET_STAKE_UNIT * BET_STAKE_UNIT
    return max(BET_STAKE_UNIT, unit_aligned_cap)


def _validate_stake_value(stake: int) -> int:
    if not isinstance(stake, int) or isinstance(stake, bool):
        raise BettingStakeError("Stake must be an integer amount.")
    return stake


def canonicalize_selections(
    bet_type: BetType,
    selection_ids: Sequence[int],
) -> tuple[int, ...]:
    """Validate selection cardinality/duplication and canonicalize deterministic order."""

    bet_type = BetType(bet_type)
    ids = tuple(selection_ids)
    expected_count = REQUIRED_SELECTION_COUNTS[bet_type]
    if len(ids) != expected_count:
        raise BettingSelectionError(
            f"{bet_type.value} bet must have exactly {expected_count} selections, got {len(ids)}."
        )
    if any(isinstance(entry_id, bool) or not isinstance(entry_id, int) or entry_id <= 0 for entry_id in ids):
        raise BettingSelectionError("Bet selections must be positive integer Entry IDs.")
    if len(set(ids)) != len(ids):
        raise BettingSelectionError("Bet selections must be distinct MatchEntry IDs.")
    if bet_type in (BetType.QUINELLA, BetType.TRIO):
        return tuple(sorted(ids))
    return ids


def _require_positive_decimal(value: Decimal, *, field_name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise BettingOddsError(f"{field_name} must be a positive finite Decimal.")
    return value


def quantize_provisional_odds(raw_odds: Decimal) -> Decimal:
    """Apply the canonical 4-decimal CEILING display boundary."""

    _require_positive_decimal(raw_odds, field_name="raw_odds")
    return raw_odds.quantize(PROVISIONAL_ODDS_QUANTUM, rounding=ROUND_CEILING)


def quantize_applied_odds(provisional_odds: Decimal, *, field_size: int) -> Decimal:
    """Apply the field multiplier and canonical 1-decimal settlement boundary."""

    _require_positive_decimal(provisional_odds, field_name="provisional_odds")
    if isinstance(field_size, bool) or not isinstance(field_size, int) or field_size <= 0:
        raise BettingOddsError("field_size must be a positive integer.")
    multiplier = Decimal("0.5") if field_size < 9 else Decimal(1)
    return (provisional_odds * multiplier).quantize(APPLIED_ODDS_QUANTUM, rounding=ROUND_CEILING)


@dataclass(frozen=True, slots=True)
class BetPoolStake:
    """One active native V2 Bet contribution to the provisional pool."""

    bet_type: BetType
    selection_ids: tuple[int, ...]
    amount: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "bet_type", BetType(self.bet_type))
        object.__setattr__(
            self,
            "selection_ids",
            canonicalize_selections(self.bet_type, self.selection_ids),
        )
        validate_new_bet_stake(self.amount)


@dataclass(frozen=True, slots=True)
class ProvisionalSelectionOdds:
    """One canonical selection and its 4-decimal CEILING provisional odds."""

    bet_type: BetType
    selection_ids: tuple[int, ...]
    odds: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "bet_type", BetType(self.bet_type))
        object.__setattr__(
            self,
            "selection_ids",
            canonicalize_selections(self.bet_type, self.selection_ids),
        )
        _require_positive_decimal(self.odds, field_name="odds")
        if self.odds.as_tuple().exponent < -4:
            raise BettingOddsError("Provisional odds must have at most four decimal places.")


@dataclass(frozen=True, slots=True)
class ZeroPoolMarketOdds:
    """One available market's uniform zero-pool opening projection."""

    bet_type: BetType
    selection_count: int
    odds: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "bet_type", BetType(self.bet_type))
        if (
            isinstance(self.selection_count, bool)
            or not isinstance(self.selection_count, int)
            or self.selection_count <= 0
        ):
            raise BettingOddsError("selection_count must be a positive integer.")
        _require_positive_decimal(self.odds, field_name="odds")
        if self.odds.as_tuple().exponent < -4:
            raise BettingOddsError("Zero-pool odds must have at most four decimal places.")


def calculate_provisional_odds(
    entry_ids: Sequence[int],
    active_bets: Sequence[BetPoolStake],
) -> tuple[ProvisionalSelectionOdds, ...]:
    """Calculate all available Win/Quinella/Trio odds from one current active pool."""

    entries = tuple(sorted(entry_ids))
    if not entries:
        raise BettingOddsError("entry_ids must contain at least one Entry.")
    if len(set(entries)) != len(entries):
        raise BettingOddsError("entry_ids must be unique.")
    if any(isinstance(entry_id, bool) or not isinstance(entry_id, int) or entry_id <= 0 for entry_id in entries):
        raise BettingOddsError("entry_ids must contain positive integers.")

    entry_set = set(entries)
    stakes: dict[BetType, dict[tuple[int, ...], int]] = {bet_type: {} for bet_type in BetType}
    for bet in active_bets:
        if not isinstance(bet, BetPoolStake):
            raise BettingOddsError("active_bets must contain BetPoolStake values.")
        if not set(bet.selection_ids).issubset(entry_set):
            raise BettingOddsError("An active Bet selection is outside the current Entry set.")
        pool = stakes[bet.bet_type]
        pool[bet.selection_ids] = pool.get(bet.selection_ids, 0) + bet.amount

    with localcontext() as context:
        context.prec = _ODDS_CALCULATION_PRECISION
        return _calculate_weighted_odds(entries=entries, stakes=stakes)


def calculate_zero_pool_market_odds(field_size: int) -> tuple[ZeroPoolMarketOdds, ...]:
    """Calculate uniform opening odds without enumerating every valid combination."""

    if isinstance(field_size, bool) or not isinstance(field_size, int) or field_size <= 0:
        raise BettingOddsError("field_size must be a positive integer.")
    configuration = (
        (BetType.WIN, 1, Decimal(9)),
        (BetType.QUINELLA, 2, Decimal(6)),
        (BetType.TRIO, 3, Decimal("4.5")),
    )
    with localcontext() as context:
        context.prec = _ODDS_CALCULATION_PRECISION
        return tuple(
            ZeroPoolMarketOdds(
                bet_type=bet_type,
                selection_count=comb(field_size, cardinality),
                odds=quantize_provisional_odds(
                    Decimal(comb(field_size, cardinality)) * _field_factor(field_size, threshold)
                ),
            )
            for bet_type, cardinality, threshold in configuration
            if field_size >= cardinality
        )


def _calculate_weighted_odds(
    *,
    entries: tuple[int, ...],
    stakes: dict[BetType, dict[tuple[int, ...], int]],
) -> tuple[ProvisionalSelectionOdds, ...]:
    field_size = len(entries)
    win_selections = tuple((entry_id,) for entry_id in entries)
    win_amounts = {selection[0]: stakes[BetType.WIN].get(selection, 0) for selection in win_selections}
    win_effective = {selection: win_amounts[selection[0]] + 1 for selection in win_selections}
    results = _selection_odds(
        bet_type=BetType.WIN,
        effective=win_effective,
        field_factor=_field_factor(field_size, Decimal(9)),
    )

    quinella_effective: dict[tuple[int, ...], int] = {}
    if field_size >= 2:
        # One shared denominator cancels from total / selection weight, so keep the
        # inherited pool exact until the final Decimal ratio and business ceiling.
        quinella_scale = 2 * (field_size - 1)
        for selection in combinations(entries, 2):
            first, second = selection
            quinella_effective[selection] = (
                stakes[BetType.QUINELLA].get(selection, 0) * quinella_scale
                + win_amounts[first]
                + win_amounts[second]
                + quinella_scale
            )
        results += _selection_odds(
            bet_type=BetType.QUINELLA,
            effective=quinella_effective,
            field_factor=_field_factor(field_size, Decimal(6)),
        )

    if field_size >= 3:
        quinella_amounts = {selection: stakes[BetType.QUINELLA].get(selection, 0) for selection in quinella_effective}
        # This is the least shared denominator of the Win and Quinella inheritance
        # terms in the canonical Trio formula.
        trio_scale = 2 * (field_size - 1) * (field_size - 2)
        trio_effective: dict[tuple[int, ...], int] = {}
        for selection in combinations(entries, 3):
            first, second, third = selection
            inherited_win_numerator = 2 * (win_amounts[first] + win_amounts[second] + win_amounts[third])
            inherited_quinella_numerator = (field_size - 1) * (
                quinella_amounts[(first, second)] + quinella_amounts[(first, third)] + quinella_amounts[(second, third)]
            )
            trio_effective[selection] = (
                stakes[BetType.TRIO].get(selection, 0) * trio_scale
                + inherited_win_numerator
                + inherited_quinella_numerator
                + 2 * trio_scale
            )
        results += _selection_odds(
            bet_type=BetType.TRIO,
            effective=trio_effective,
            field_factor=_field_factor(field_size, Decimal("4.5")),
        )
    return results


def _selection_odds(
    *,
    bet_type: BetType,
    effective: dict[tuple[int, ...], int],
    field_factor: Decimal,
) -> tuple[ProvisionalSelectionOdds, ...]:
    total = sum(effective.values())
    return tuple(
        ProvisionalSelectionOdds(
            bet_type=bet_type,
            selection_ids=selection,
            odds=quantize_provisional_odds(Decimal(total) / Decimal(value) * field_factor),
        )
        for selection, value in effective.items()
    )


def _field_factor(field_size: int, threshold: Decimal) -> Decimal:
    ratio = Decimal(field_size) / threshold
    if ratio <= 1:
        return Decimal(1)
    return ratio.sqrt().sqrt()


def calculate_payout(stake: int, confirmed_odds: Decimal) -> int:
    """Calculate integer payout from an already applied 1-decimal odds value."""

    _validate_stake_value(stake)
    if stake <= 0:
        raise BettingStakeError("Payout stake must be positive.")
    _require_positive_decimal(confirmed_odds, field_name="confirmed_odds")
    if confirmed_odds != confirmed_odds.quantize(APPLIED_ODDS_QUANTUM, rounding=ROUND_CEILING):
        raise BettingOddsError("confirmed_odds must already use the 0.1 applied precision.")
    return int((Decimal(stake) * confirmed_odds).to_integral_value(rounding=ROUND_CEILING))


def calculate_refund(stake: int) -> int:
    """Return the exact original stake without mutation."""

    return _validate_stake_value(stake)
