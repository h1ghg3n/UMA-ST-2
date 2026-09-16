"""Pure domain invariants for MatchEntry sets and results.

No repository, persistence, or transport concerns are included; this module only
validates canonical business invariants.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from .errors import (
    MatchEntryCountError,
    MatchEntryMarginError,
    MatchEntryPopularityRankError,
    MatchEntryRankError,
    MatchEntryRegionError,
    MatchEntryVariantError,
)

MATCH_ENTRY_MAXIMUM_COUNT: Final = 18


@dataclass(frozen=True)
class MatchEntryCandidate:
    """Minimal MatchEntry data required for invariant validation."""

    game_account_id: int
    game_region: str
    umamusume_id: int
    umamusume_variant_id: int | None
    rank: int | None
    popularity_rank: int | None
    margin: str | None


def validate_match_entry_invariants(
    entries: Sequence[MatchEntryCandidate],
    *,
    variant_base_map: Mapping[int, int],
) -> None:
    """Validate set and result invariants for one Match.

    - no more than the supported maximum Entry count
    - uniform game_region within one Match
    - optional variant-to-base Umamusume consistency
    - unique non-null ranks
    - unique non-null popularity ranks
    - margin is optional source-display text for finishers with rank >= 2

    Unknown variant lookups are left untouched by default; this kernel only
    checks base mapping consistency when a canonical mapping entry exists.
    """

    if len(entries) > MATCH_ENTRY_MAXIMUM_COUNT:
        raise MatchEntryCountError(f"A Match cannot contain more than {MATCH_ENTRY_MAXIMUM_COUNT} Entries.")
    if not entries:
        return

    _validate_same_region(entries)
    _validate_variant_relationship(entries, variant_base_map)
    _validate_rank_invariants(entries)
    _validate_popularity_rank_invariants(entries)
    _validate_margin(entries)


def _validate_same_region(entries: Sequence[MatchEntryCandidate]) -> None:
    game_regions = {entry.game_region for entry in entries}

    if len(game_regions) > 1:
        raise MatchEntryRegionError("All MatchEntries in one Match must share the same game_region.")


def _validate_variant_relationship(
    entries: Sequence[MatchEntryCandidate],
    variant_base_map: Mapping[int, int],
) -> None:
    for entry in entries:
        if entry.umamusume_variant_id is None:
            continue

        base_umamusume_id = variant_base_map.get(entry.umamusume_variant_id)
        if base_umamusume_id is None:
            continue

        if base_umamusume_id != entry.umamusume_id:
            raise MatchEntryVariantError("When umamusume_variant_id is set, it must match the base umamusume_id.")


def _validate_rank_invariants(entries: Sequence[MatchEntryCandidate]) -> None:
    non_null_ranks = [entry.rank for entry in entries if entry.rank is not None]
    if len(non_null_ranks) != len(set(non_null_ranks)):
        raise MatchEntryRankError("Non-null ranks must be unique within one Match.")


def _validate_popularity_rank_invariants(entries: Sequence[MatchEntryCandidate]) -> None:
    non_null_popularity = [entry.popularity_rank for entry in entries if entry.popularity_rank is not None]

    if len(non_null_popularity) != len(set(non_null_popularity)):
        raise MatchEntryPopularityRankError("Non-null popularity ranks must be unique within one Match.")


def _validate_margin(entries: Sequence[MatchEntryCandidate]) -> None:
    for entry in entries:
        if entry.margin is None:
            continue
        if not isinstance(entry.margin, str):
            raise MatchEntryMarginError("Margin must be source-display text or None.")
        if entry.rank is None or entry.rank < 2:
            raise MatchEntryMarginError("Margin may only be set for a finisher with rank >= 2.")
