import pytest

from uma_st2.domain.match import (
    MATCH_ENTRY_MAXIMUM_COUNT,
    MatchEntryCandidate,
    MatchEntryCountError,
    MatchEntryMarginError,
    MatchEntryPopularityRankError,
    MatchEntryRankError,
    MatchEntryRegionError,
    MatchEntryVariantError,
    validate_match_entry_invariants,
)


def _counted_entries(count: int) -> list[MatchEntryCandidate]:
    return [MatchEntryCandidate(index, "KR", 100 + index, None, None, None, None) for index in range(1, count + 1)]


def test_validate_match_entry_invariants_accepts_valid_entry_set() -> None:
    validate_match_entry_invariants(
        [
            MatchEntryCandidate(
                game_account_id=1,
                game_region="KR",
                umamusume_id=10,
                umamusume_variant_id=1001,
                rank=1,
                popularity_rank=3,
                margin=None,
            ),
            MatchEntryCandidate(
                game_account_id=2,
                game_region="KR",
                umamusume_id=20,
                umamusume_variant_id=None,
                rank=2,
                popularity_rank=1,
                margin="코",
            ),
            MatchEntryCandidate(
                game_account_id=3,
                game_region="KR",
                umamusume_id=20,
                umamusume_variant_id=1002,
                rank=None,
                popularity_rank=2,
                margin=None,
            ),
        ],
        variant_base_map={1001: 10, 1002: 20},
    )


def test_validate_match_entry_invariants_with_empty_set_is_noop() -> None:
    validate_match_entry_invariants([], variant_base_map={})


def test_match_entry_count_accepts_eighteen_and_rejects_nineteen() -> None:
    assert MATCH_ENTRY_MAXIMUM_COUNT == 18
    validate_match_entry_invariants(_counted_entries(18), variant_base_map={})

    with pytest.raises(MatchEntryCountError, match="more than 18"):
        validate_match_entry_invariants(_counted_entries(19), variant_base_map={})


def test_duplicate_game_account_entries_are_preserved_for_later_rating_selection() -> None:
    payload = [
        MatchEntryCandidate(1, "KR", 10, None, 1, 1, None),
        MatchEntryCandidate(1, "KR", 11, None, 2, 2, "코"),
    ]

    validate_match_entry_invariants(payload, variant_base_map={})


def test_mixed_regions_rejected() -> None:
    payload = [
        MatchEntryCandidate(1, "KR", 10, None, 1, 1, None),
        MatchEntryCandidate(2, "JP", 11, None, 2, 2, "코"),
    ]

    with pytest.raises(MatchEntryRegionError):
        validate_match_entry_invariants(payload, variant_base_map={})


def test_variant_lookup_unknown_is_not_rejected_by_default() -> None:
    payload = [
        MatchEntryCandidate(1, "KR", 10, 999, 1, 1, None),
    ]

    validate_match_entry_invariants(payload, variant_base_map={})


def test_variant_mismatch_rejected() -> None:
    payload = [
        MatchEntryCandidate(1, "KR", 10, 1001, 1, 1, None),
    ]

    with pytest.raises(MatchEntryVariantError):
        validate_match_entry_invariants(payload, variant_base_map={1001: 20})


def test_duplicate_rank_rejected() -> None:
    payload = [
        MatchEntryCandidate(1, "KR", 10, None, 1, 1, None),
        MatchEntryCandidate(2, "KR", 11, None, 1, 2, None),
    ]

    with pytest.raises(MatchEntryRankError):
        validate_match_entry_invariants(payload, variant_base_map={})


def test_duplicate_popularity_rank_rejected() -> None:
    payload = [
        MatchEntryCandidate(1, "KR", 10, None, 1, 1, None),
        MatchEntryCandidate(2, "KR", 11, None, 2, 1, "머리"),
    ]

    with pytest.raises(MatchEntryPopularityRankError):
        validate_match_entry_invariants(payload, variant_base_map={})


def test_winner_rank_margin_must_be_null() -> None:
    payload = [
        MatchEntryCandidate(1, "KR", 10, None, 1, 1, "코"),
        MatchEntryCandidate(2, "KR", 11, None, 2, 2, "목"),
    ]

    with pytest.raises(MatchEntryMarginError):
        validate_match_entry_invariants(payload, variant_base_map={})


@pytest.mark.parametrize("margin", ["코", "머리", "목", "1/2", "대차"])
def test_non_winner_source_display_margin_is_allowed(margin: str) -> None:
    payload = [MatchEntryCandidate(1, "KR", 10, None, 2, 1, margin)]

    validate_match_entry_invariants(payload, variant_base_map={})


@pytest.mark.parametrize("margin", [0, -12, 1.5, object()])
def test_non_text_margin_is_rejected(margin: object) -> None:
    payload = [MatchEntryCandidate(1, "KR", 10, None, 2, 1, margin)]  # type: ignore[arg-type]

    with pytest.raises(MatchEntryMarginError, match="source-display text"):
        validate_match_entry_invariants(payload, variant_base_map={})


def test_margin_without_a_confirmed_rank_is_rejected() -> None:
    payload = [MatchEntryCandidate(1, "KR", 10, None, None, 1, "코")]

    with pytest.raises(MatchEntryMarginError, match="rank >= 2"):
        validate_match_entry_invariants(payload, variant_base_map={})


def test_multiple_none_ranks_allowed() -> None:
    payload = [
        MatchEntryCandidate(1, "KR", 10, None, None, 1, None),
        MatchEntryCandidate(2, "KR", 11, None, None, 2, None),
        MatchEntryCandidate(3, "KR", 12, None, 1, 3, None),
    ]

    validate_match_entry_invariants(payload, variant_base_map={})


def test_multiple_none_popularity_ranks_allowed() -> None:
    payload = [
        MatchEntryCandidate(1, "KR", 10, None, 1, None, None),
        MatchEntryCandidate(2, "KR", 11, None, 2, None, "목"),
        MatchEntryCandidate(3, "KR", 12, None, 3, 3, "대차"),
    ]

    validate_match_entry_invariants(payload, variant_base_map={})
