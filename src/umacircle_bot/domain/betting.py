from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from umacircle_bot.domain.errors import BettingRuleError

MAX_POINT_AMOUNT = 9_223_372_036_854_775_807
MAX_CIRCLE_POINT_BALANCE = 9_223_372_036_854_775_807
MIN_CIRCLE_POINT_BALANCE = -1_000
MAX_PAYOUT_RATE = Decimal("99999999.99")
FULL_PAYOUT_MIN_PARTICIPANT_COUNT = 9
REDUCED_PAYOUT_MULTIPLIER = Decimal("0.5")


class MatchBetType(StrEnum):
    WIN = "win"
    QUINELLA = "quinella"
    TRIO = "trio"


SELECTION_COUNTS = {
    MatchBetType.WIN: 1,
    MatchBetType.QUINELLA: 2,
    MatchBetType.TRIO: 3,
}


@dataclass(frozen=True)
class MatchPayout:
    payout_amount: int
    payout_rate: Decimal | None = None

    def __post_init__(self) -> None:
        validate_nonnegative_point_amount(self.payout_amount, field_name="payout amount")
        if self.payout_rate is None:
            return
        if not isinstance(self.payout_rate, Decimal):
            raise BettingRuleError("payout rate must be a Decimal")
        if not self.payout_rate.is_finite() or not 0 <= self.payout_rate <= MAX_PAYOUT_RATE:
            raise BettingRuleError("payout rate is outside the supported range")
        if self.payout_rate.quantize(Decimal("0.01")) != self.payout_rate:
            raise BettingRuleError("payout rate supports at most two decimal places")


def match_payout_multiplier(participant_count: int) -> Decimal:
    if not _is_positive_int(participant_count):
        raise BettingRuleError("participant count must be a positive integer")
    if participant_count < FULL_PAYOUT_MIN_PARTICIPANT_COUNT:
        return REDUCED_PAYOUT_MULTIPLIER
    return Decimal(1)


def calculate_match_payout_amount(
    stake_amount: int,
    payout_rate: Decimal,
    participant_count: int,
) -> int:
    validate_positive_point_amount(stake_amount, field_name="stake amount")
    normalized_rate = _normalize_calculation_payout_rate(payout_rate)
    payout = Decimal(stake_amount) * normalized_rate * match_payout_multiplier(participant_count)
    return int(payout.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_match_payout_amount_from_effective_rate(
    stake_amount: int,
    effective_payout_rate: Decimal,
) -> int:
    """Calculate a payout using a rate already frozen in an odds snapshot."""
    validate_positive_point_amount(stake_amount, field_name="stake amount")
    normalized_rate = _normalize_calculation_payout_rate(effective_payout_rate)
    payout = Decimal(stake_amount) * normalized_rate
    return int(payout.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def adjust_match_payout_rate(payout_rate: Decimal, participant_count: int) -> Decimal:
    return _normalize_calculation_payout_rate(payout_rate) * match_payout_multiplier(participant_count)


def normalize_match_bet_type(value: str) -> MatchBetType:
    if not isinstance(value, str):
        raise BettingRuleError("bet type must be text")
    try:
        return MatchBetType(value.strip().lower())
    except ValueError as exc:
        raise BettingRuleError("unsupported room-match bet type") from exc


def normalize_match_bet_numbers(bet_type: str, numbers: Sequence[int]) -> list[int]:
    normalized_type = normalize_match_bet_type(bet_type)
    if isinstance(numbers, (str, bytes)) or not isinstance(numbers, Sequence):
        raise BettingRuleError("bet numbers must be a sequence of integers")

    expected_count = SELECTION_COUNTS[normalized_type]
    if len(numbers) != expected_count:
        raise BettingRuleError(f"{normalized_type.value} requires exactly {expected_count} selection(s)")
    normalized_numbers = list(numbers)
    if any(not _is_positive_int(number) for number in normalized_numbers):
        raise BettingRuleError("bet numbers must be positive integers")
    if len(set(normalized_numbers)) != len(normalized_numbers):
        raise BettingRuleError("bet numbers must be distinct")

    if normalized_type in {MatchBetType.QUINELLA, MatchBetType.TRIO}:
        normalized_numbers.sort()
    return normalized_numbers


def is_match_bet_hit(
    bet_type: str,
    numbers: Sequence[int],
    finishers_by_rank: Mapping[int, int],
) -> bool:
    normalized_type = normalize_match_bet_type(bet_type)
    normalized_numbers = normalize_match_bet_numbers(normalized_type.value, numbers)
    if not isinstance(finishers_by_rank, Mapping):
        raise BettingRuleError("race finishers must be a rank mapping")

    required_count = SELECTION_COUNTS[normalized_type]
    winning_numbers: list[int] = []
    for rank in range(1, required_count + 1):
        entry_number = finishers_by_rank.get(rank)
        if not _is_positive_int(entry_number):
            raise BettingRuleError("confirmed race results do not contain all required ranks")
        winning_numbers.append(entry_number)

    if len(set(winning_numbers)) != len(winning_numbers):
        raise BettingRuleError("confirmed race results contain duplicate finishers")
    return normalized_numbers == normalize_match_bet_numbers(normalized_type.value, winning_numbers)


def validate_positive_point_amount(value: int, *, field_name: str = "point amount") -> None:
    if not _is_positive_int(value) or value > MAX_POINT_AMOUNT:
        raise BettingRuleError(f"{field_name} must be an integer between 1 and {MAX_POINT_AMOUNT}")


def validate_nonnegative_point_amount(value: int, *, field_name: str = "point amount") -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_POINT_AMOUNT:
        raise BettingRuleError(f"{field_name} must be an integer between 0 and {MAX_POINT_AMOUNT}")


def validate_circle_point_balance(value: int, *, field_name: str = "room point balance") -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not MIN_CIRCLE_POINT_BALANCE <= value <= MAX_CIRCLE_POINT_BALANCE
    ):
        raise BettingRuleError(
            f"{field_name} must be an integer between {MIN_CIRCLE_POINT_BALANCE} and {MAX_CIRCLE_POINT_BALANCE}"
        )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _normalize_calculation_payout_rate(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise BettingRuleError("payout rate must be a Decimal")
    if not value.is_finite() or not 0 <= value <= MAX_PAYOUT_RATE:
        raise BettingRuleError("payout rate is outside the supported range")
    return value
