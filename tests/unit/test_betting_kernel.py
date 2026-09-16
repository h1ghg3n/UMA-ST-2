from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from uma_st2.domain.betting import (
    BetPoolStake,
    BettingOddsError,
    BettingSelectionError,
    BettingStakeError,
    BetType,
    calculate_bet_stake_cap,
    calculate_payout,
    calculate_provisional_odds,
    calculate_refund,
    calculate_zero_pool_market_odds,
    canonicalize_selections,
    quantize_applied_odds,
    quantize_provisional_odds,
    validate_new_bet_stake,
)


def test_win_with_one_entry_succeeds() -> None:
    assert canonicalize_selections(BetType.WIN, (3,)) == (3,)


def test_win_with_zero_or_two_entries_fails() -> None:
    with pytest.raises(BettingSelectionError):
        canonicalize_selections(BetType.WIN, ())

    with pytest.raises(BettingSelectionError):
        canonicalize_selections(BetType.WIN, (1, 2))


def test_quinella_with_two_distinct_entries_succeeds_and_canonicalizes() -> None:
    assert canonicalize_selections(BetType.QUINELLA, (7, 3)) == (3, 7)


def test_trio_with_three_distinct_entries_succeeds_and_canonicalizes() -> None:
    assert canonicalize_selections(BetType.TRIO, (9, 1, 5)) == (1, 5, 9)


def test_duplicate_entry_selection_fails() -> None:
    with pytest.raises(BettingSelectionError):
        canonicalize_selections(BetType.QUINELLA, (11, 11))

    with pytest.raises(BettingSelectionError):
        canonicalize_selections(BetType.TRIO, (1, 2, 2))


def test_new_bet_stake_ten_and_twenty_succeeds() -> None:
    validate_new_bet_stake(10)
    validate_new_bet_stake(20)


def test_new_bet_stake_zero_negative_or_non_multiple_fails() -> None:
    for stake in (0, -10, 11):
        with pytest.raises(BettingStakeError):
            validate_new_bet_stake(stake)


@pytest.mark.parametrize(
    ("balance", "expected_cap"),
    (
        (0, 10),
        (50, 10),
        (99, 10),
        (100, 10),
        (199, 10),
        (200, 20),
        (999, 90),
        (1000, 100),
    ),
)
def test_bet_stake_cap_is_ten_percent_with_ten_point_minimum_and_unit_alignment(
    balance: int,
    expected_cap: int,
) -> None:
    assert calculate_bet_stake_cap(balance) == expected_cap


@pytest.mark.parametrize("balance", (-1, True, 1.5))
def test_bet_stake_cap_rejects_invalid_reference_balance(balance: object) -> None:
    with pytest.raises(BettingStakeError):
        calculate_bet_stake_cap(balance)  # type: ignore[arg-type]


def test_existing_non_tenth_stake_preserves_payout_and_refund() -> None:
    assert calculate_payout(13, Decimal("1.3")) == 17
    assert calculate_refund(13) == 13


def test_provisional_odds_keeps_1_23450_as_12345() -> None:
    assert quantize_provisional_odds(Decimal("1.23450")) == Decimal("1.2345")


def test_provisional_odds_ceils_1_23451_to_1_2346() -> None:
    assert quantize_provisional_odds(Decimal("1.23451")) == Decimal("1.2346")


def test_applied_odds_use_field_multiplier_and_one_decimal_ceiling() -> None:
    assert quantize_applied_odds(Decimal("1.7273"), field_size=9) == Decimal("1.8")
    assert quantize_applied_odds(Decimal("1.7273"), field_size=8) == Decimal("0.9")


def test_ten_unit_stake_and_applied_odds_produce_exact_integer_payout() -> None:
    assert calculate_payout(10, Decimal("1.8")) == 18
    assert calculate_payout(30, Decimal("1.8")) == 54


def test_payout_rejects_provisional_precision_as_applied_odds() -> None:
    with pytest.raises(BettingOddsError):
        calculate_payout(10, Decimal("1.2345"))


def test_refund_returns_original_stake() -> None:
    assert calculate_refund(10) == 10


def test_zero_pool_nine_entry_odds_match_current_weighted_policy() -> None:
    projection = calculate_provisional_odds(tuple(range(1, 10)), ())
    by_type = {bet_type: tuple(item for item in projection if item.bet_type == bet_type) for bet_type in BetType}

    assert len(by_type[BetType.WIN]) == 9
    assert {item.odds for item in by_type[BetType.WIN]} == {Decimal("9.0000")}
    assert len(by_type[BetType.QUINELLA]) == 36
    assert {item.odds for item in by_type[BetType.QUINELLA]} == {Decimal("39.8406")}
    assert len(by_type[BetType.TRIO]) == 84
    assert {item.odds for item in by_type[BetType.TRIO]} == {Decimal("99.8934")}


def test_win_bet_propagates_to_weighted_market_odds_without_float_math() -> None:
    projection = calculate_provisional_odds(
        tuple(range(1, 10)),
        (BetPoolStake(BetType.WIN, (1,), 10),),
    )
    win = {item.selection_ids: item.odds for item in projection if item.bet_type == BetType.WIN}

    assert win[(1,)] == Decimal("1.7273")
    assert win[(2,)] == Decimal("19.0000")


def test_quinella_inheritance_uses_exact_common_denominator_before_ceiling() -> None:
    projection = calculate_provisional_odds(
        (1, 2, 3, 4),
        (
            BetPoolStake(BetType.WIN, (1,), 10),
            BetPoolStake(BetType.QUINELLA, (1, 2), 20),
        ),
    )
    quinella = {item.selection_ids: item.odds for item in projection if item.bet_type == BetType.QUINELLA}

    assert quinella == {
        (1, 2): Decimal("1.3677"),
        (1, 3): Decimal("11.6250"),
        (1, 4): Decimal("11.6250"),
        (2, 3): Decimal("31.0000"),
        (2, 4): Decimal("31.0000"),
        (3, 4): Decimal("31.0000"),
    }
    assert quantize_applied_odds(quinella[(2, 3)], field_size=4) == Decimal("15.5")
    assert calculate_payout(10, Decimal("15.5")) == 155


def test_trio_inheritance_uses_exact_common_denominator_before_ceiling() -> None:
    projection = calculate_provisional_odds(
        (1, 2, 3, 4),
        (
            BetPoolStake(BetType.WIN, (3,), 10),
            BetPoolStake(BetType.QUINELLA, (1, 2), 10),
        ),
    )
    trio = {item.selection_ids: item.odds for item in projection if item.bet_type == BetType.TRIO}

    assert trio == {
        (1, 2, 3): Decimal("2.9190"),
        (1, 2, 4): Decimal("4.0000"),
        (1, 3, 4): Decimal("4.9091"),
        (2, 3, 4): Decimal("4.9091"),
    }
    assert quantize_applied_odds(trio[(1, 2, 4)], field_size=4) == Decimal("2.0")
    assert calculate_payout(10, Decimal("2.0")) == 20


def test_trio_direct_and_inherited_stakes_match_fixed_reference_values() -> None:
    projection = calculate_provisional_odds(
        (1, 2, 3, 4),
        (
            BetPoolStake(BetType.WIN, (3,), 10),
            BetPoolStake(BetType.QUINELLA, (1, 2), 10),
            BetPoolStake(BetType.TRIO, (1, 2, 4), 10),
        ),
    )
    trio = {item.selection_ids: item.odds for item in projection if item.bet_type == BetType.TRIO}

    assert trio == {
        (1, 2, 3): Decimal("4.5406"),
        (1, 2, 4): Decimal("1.9311"),
        (1, 3, 4): Decimal("7.6364"),
        (2, 3, 4): Decimal("7.6364"),
    }


def test_unavailable_markets_are_not_created_for_small_fields() -> None:
    one_entry = calculate_provisional_odds((1,), ())
    two_entries = calculate_provisional_odds((1, 2), ())

    assert {item.bet_type for item in one_entry} == {BetType.WIN}
    assert {item.bet_type for item in two_entries} == {BetType.WIN, BetType.QUINELLA}


@pytest.mark.parametrize("field_size", (1, 2, 3, 9))
def test_zero_pool_market_projection_matches_complete_selection_projection(field_size: int) -> None:
    complete = calculate_provisional_odds(tuple(range(1, field_size + 1)), ())
    market_projection = calculate_zero_pool_market_odds(field_size)

    for market in market_projection:
        selections = tuple(item for item in complete if item.bet_type == market.bet_type)
        assert len(selections) == market.selection_count
        assert {item.odds for item in selections} == {market.odds}


def test_pool_rejects_non_current_selection_and_non_ten_unit_stake() -> None:
    with pytest.raises(BettingOddsError):
        calculate_provisional_odds((1, 2), (BetPoolStake(BetType.WIN, (3,), 10),))
    with pytest.raises(BettingStakeError):
        BetPoolStake(BetType.WIN, (1,), 11)


def test_no_calculation_path_uses_decimal_without_float() -> None:
    source = (
        inspect.getsource(calculate_payout)
        + inspect.getsource(calculate_provisional_odds)
        + inspect.getsource(quantize_provisional_odds)
        + inspect.getsource(calculate_refund)
    )
    assert "float(" not in source
    assert "Decimal(" in source
