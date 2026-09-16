"""Pure native V2 Room Match Rating calculations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext
from typing import Final

from uma_st2.domain.match import MatchGrade

from .errors import RatingCalculationError, RatingRuleError

RATING_FORMULA_VERSION: Final = "room_match_rating_v2_decimal"
RATING_STORAGE_QUANTUM: Final = Decimal("0.000000000000000001")
RATING_RULE_GRADES: Final = frozenset((MatchGrade.G1, MatchGrade.G2, MatchGrade.G3))
_GRADE_FACTORS: Final = {
    MatchGrade.G1: Decimal(7),
    MatchGrade.G2: Decimal(5),
    MatchGrade.G3: Decimal(3),
}
_CALCULATION_PRECISION: Final = 60


def _positive_integer(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RatingCalculationError(f"{field_name} must be a positive integer.")
    return value


def _finite_decimal(value: object, *, field_name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise RatingCalculationError(f"{field_name} must be a finite Decimal.")
    return value


def normalize_rating_storage(value: Decimal) -> Decimal:
    """Normalize one Rating value only at the NUMERIC(30,18) storage boundary."""

    normalized = _finite_decimal(value, field_name="rating").quantize(
        RATING_STORAGE_QUANTUM,
        rounding=ROUND_HALF_EVEN,
    )
    if normalized.adjusted() > 11:
        raise RatingCalculationError("Rating value exceeds NUMERIC(30,18).")
    return normalized


@dataclass(frozen=True, slots=True)
class RatingRule:
    """One immutable base-delta cell from a versioned Rating workbook."""

    grade: MatchGrade
    participant_count: int
    converted_rank: int
    base_delta: Decimal

    def __post_init__(self) -> None:
        grade = MatchGrade(self.grade)
        if grade not in RATING_RULE_GRADES:
            raise RatingRuleError("RatingRule rows are supported only for G1, G2, and G3.")
        try:
            participant_count = _positive_integer(self.participant_count, field_name="participant_count")
            converted_rank = _positive_integer(self.converted_rank, field_name="converted_rank")
        except RatingCalculationError as error:
            raise RatingRuleError(str(error)) from error
        if converted_rank > participant_count:
            raise RatingRuleError("RatingRule converted_rank cannot exceed participant_count.")
        try:
            base_delta = normalize_rating_storage(self.base_delta)
        except RatingCalculationError as error:
            raise RatingRuleError(str(error)) from error
        object.__setattr__(self, "grade", grade)
        object.__setattr__(self, "participant_count", participant_count)
        object.__setattr__(self, "converted_rank", converted_rank)
        object.__setattr__(self, "base_delta", base_delta)


@dataclass(frozen=True, slots=True)
class RatingParticipant:
    """One confirmed native Match Entry at the settlement boundary."""

    match_entry_id: int
    game_account_id: int
    rank: int
    rating_before: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "match_entry_id",
            _positive_integer(self.match_entry_id, field_name="match_entry_id"),
        )
        object.__setattr__(
            self,
            "game_account_id",
            _positive_integer(self.game_account_id, field_name="game_account_id"),
        )
        object.__setattr__(self, "rank", _positive_integer(self.rank, field_name="rank"))
        normalized = normalize_rating_storage(self.rating_before)
        if normalized < 0:
            raise RatingCalculationError("rating_before cannot be negative.")
        object.__setattr__(self, "rating_before", normalized)


@dataclass(frozen=True, slots=True)
class RatingTransactionDraft:
    """Exact Rating transition to persist for one Match Entry."""

    match_entry_id: int
    game_account_id: int
    rank: int
    converted_rank: int
    rating_before: Decimal
    base_delta: Decimal
    adjustment_delta: Decimal
    amount: Decimal
    rating_after: Decimal

    def __post_init__(self) -> None:
        for field_name in ("rating_before", "base_delta", "adjustment_delta", "amount", "rating_after"):
            value = getattr(self, field_name)
            if value != normalize_rating_storage(value):
                raise RatingCalculationError(f"{field_name} must use NUMERIC(30,18) precision.")
        if self.rating_before + self.amount != self.rating_after:
            raise RatingCalculationError("Rating transaction does not balance.")
        if self.rating_after < 0:
            raise RatingCalculationError("rating_after cannot be negative.")


def calculate_rating_transactions(
    *,
    grade: MatchGrade,
    participants: tuple[RatingParticipant, ...],
    rules: tuple[RatingRule, ...],
) -> tuple[RatingTransactionDraft, ...]:
    """Calculate every GameAccount Rating transition from one complete confirmed board."""

    canonical_grade = MatchGrade(grade)
    ordered = tuple(sorted(participants, key=lambda participant: (participant.rank, participant.match_entry_id)))
    if canonical_grade is MatchGrade.OP:
        return ()
    if len(ordered) < 2:
        raise RatingCalculationError("A Rating-eligible Match requires at least two participants.")
    expected_ranks = tuple(range(1, len(ordered) + 1))
    if tuple(participant.rank for participant in ordered) != expected_ranks:
        raise RatingCalculationError("Rating participants require complete contiguous ranks.")
    if len({participant.match_entry_id for participant in ordered}) != len(ordered):
        raise RatingCalculationError("Rating participants contain duplicate Match Entries.")
    if len({participant.game_account_id for participant in ordered}) != len(ordered):
        raise RatingCalculationError("One GameAccount cannot receive two Rating transitions for one Match.")

    field_size = len(ordered)
    converted_ranks = _converted_ranks(field_size)
    rule_by_rank = _rules_for_board(grade=canonical_grade, field_size=field_size, rules=rules)
    average_before = sum((participant.rating_before for participant in ordered), start=Decimal()) / Decimal(field_size)

    drafts: list[RatingTransactionDraft] = []
    for participant, converted_rank in zip(ordered, converted_ranks, strict=True):
        if canonical_grade is MatchGrade.LISTED:
            base_delta = Decimal()
            adjustment_delta = Decimal()
        else:
            base_delta = rule_by_rank[converted_rank]
            adjustment_delta = _adjustment_delta(
                average_rating_before=average_before,
                rating_before=participant.rating_before,
                grade=canonical_grade,
            )
        rating_after = normalize_rating_storage(
            max(participant.rating_before + base_delta + adjustment_delta, Decimal())
        )
        amount = normalize_rating_storage(rating_after - participant.rating_before)
        drafts.append(
            RatingTransactionDraft(
                match_entry_id=participant.match_entry_id,
                game_account_id=participant.game_account_id,
                rank=participant.rank,
                converted_rank=converted_rank,
                rating_before=participant.rating_before,
                base_delta=normalize_rating_storage(base_delta),
                adjustment_delta=normalize_rating_storage(adjustment_delta),
                amount=amount,
                rating_after=rating_after,
            )
        )
    return tuple(drafts)


def _converted_ranks(field_size: int) -> tuple[int, ...]:
    return tuple(
        int(
            ((Decimal(field_size - 1) * Decimal(ordinal)) / Decimal(field_size - 1) + Decimal(1)).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        for ordinal in range(field_size)
    )


def _rules_for_board(
    *,
    grade: MatchGrade,
    field_size: int,
    rules: tuple[RatingRule, ...],
) -> dict[int, Decimal]:
    if grade is MatchGrade.LISTED:
        return {}
    matching = tuple(rule for rule in rules if rule.grade is grade and rule.participant_count == field_size)
    by_rank = {rule.converted_rank: rule.base_delta for rule in matching}
    if len(by_rank) != len(matching):
        raise RatingRuleError("RatingRule rows contain duplicate grade/participant/rank keys.")
    required = set(range(1, field_size + 1))
    missing = sorted(required - set(by_rank))
    if missing:
        raise RatingRuleError(f"RatingRule version does not cover converted ranks: {missing}.")
    return by_rank


def _adjustment_delta(
    *,
    average_rating_before: Decimal,
    rating_before: Decimal,
    grade: MatchGrade,
) -> Decimal:
    difference = average_rating_before - rating_before
    with localcontext() as context:
        context.prec = _CALCULATION_PRECISION
        if difference >= 0:
            curve = Decimal("1.5") * ((difference + Decimal(10)).log10() - Decimal(1))
        else:
            curve = -((-difference + Decimal(10)).log10()) + Decimal(1)
        return curve * _GRADE_FACTORS[grade]
