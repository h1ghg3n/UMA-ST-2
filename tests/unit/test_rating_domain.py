from decimal import Decimal

import pytest

from uma_st2.domain.match import MatchGrade
from uma_st2.domain.rating import (
    RatingCalculationError,
    RatingParticipant,
    RatingRule,
    RatingRuleError,
    calculate_rating_transactions,
)


def _participants(*ratings: Decimal) -> tuple[RatingParticipant, ...]:
    return tuple(
        RatingParticipant(
            match_entry_id=index,
            game_account_id=100 + index,
            rank=index,
            rating_before=rating,
        )
        for index, rating in enumerate(ratings, start=1)
    )


def _rules(grade: MatchGrade, *deltas: Decimal) -> tuple[RatingRule, ...]:
    return tuple(
        RatingRule(
            grade=grade,
            participant_count=len(deltas),
            converted_rank=index,
            base_delta=delta,
        )
        for index, delta in enumerate(deltas, start=1)
    )


def test_g1_rating_uses_decimal_adjustment_and_balanced_storage_amount() -> None:
    drafts = calculate_rating_transactions(
        grade=MatchGrade.G1,
        participants=_participants(Decimal(100), Decimal(0)),
        rules=_rules(MatchGrade.G1, Decimal(10), Decimal(20)),
    )

    assert tuple(draft.converted_rank for draft in drafts) == (1, 2)
    assert drafts[0].rating_after == Decimal("104.552941247314494572")
    assert drafts[0].amount == Decimal("4.552941247314494572")
    assert drafts[1].rating_after == Decimal("28.170588129028258141")
    assert drafts[1].amount == Decimal("28.170588129028258141")
    assert all(draft.rating_before + draft.amount == draft.rating_after for draft in drafts)


def test_rating_floor_records_the_actual_balanced_delta() -> None:
    drafts = calculate_rating_transactions(
        grade=MatchGrade.G1,
        participants=_participants(Decimal(0), Decimal(0)),
        rules=_rules(MatchGrade.G1, Decimal(5), Decimal(-10)),
    )

    assert drafts[0].rating_after == Decimal("5.000000000000000000")
    assert drafts[1].base_delta == Decimal("-10.000000000000000000")
    assert drafts[1].rating_after == Decimal("0.000000000000000000")
    assert drafts[1].amount == Decimal("0.000000000000000000")


def test_listed_records_zero_delta_for_every_game_account_without_base_rules() -> None:
    drafts = calculate_rating_transactions(
        grade=MatchGrade.LISTED,
        participants=_participants(Decimal("12.5"), Decimal("4.25")),
        rules=(),
    )

    assert len(drafts) == 2
    assert all(draft.amount == 0 for draft in drafts)
    assert tuple(draft.rating_after for draft in drafts) == (
        Decimal("12.500000000000000000"),
        Decimal("4.250000000000000000"),
    )


def test_op_creates_no_rating_transaction() -> None:
    assert (
        calculate_rating_transactions(
            grade=MatchGrade.OP,
            participants=_participants(Decimal(0)),
            rules=(),
        )
        == ()
    )


def test_rating_rejects_incomplete_rules_and_duplicate_game_account() -> None:
    participants = _participants(Decimal(0), Decimal(0))
    with pytest.raises(RatingRuleError, match="cover converted ranks"):
        calculate_rating_transactions(
            grade=MatchGrade.G2,
            participants=participants,
            rules=(RatingRule(MatchGrade.G2, 2, 1, Decimal(1)),),
        )

    duplicate = (
        participants[0],
        RatingParticipant(
            match_entry_id=2, game_account_id=participants[0].game_account_id, rank=2, rating_before=Decimal(0)
        ),
    )
    with pytest.raises(RatingCalculationError, match="GameAccount"):
        calculate_rating_transactions(
            grade=MatchGrade.G2,
            participants=duplicate,
            rules=_rules(MatchGrade.G2, Decimal(1), Decimal(-1)),
        )


def test_rating_eligible_grade_requires_at_least_two_participants() -> None:
    with pytest.raises(RatingCalculationError, match="at least two"):
        calculate_rating_transactions(
            grade=MatchGrade.G3,
            participants=_participants(Decimal(0)),
            rules=(),
        )
