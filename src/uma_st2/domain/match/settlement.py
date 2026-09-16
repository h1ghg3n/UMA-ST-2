"""Pure native V2 Match settlement reward rules."""

from __future__ import annotations

from typing import Final

from .models import MatchGrade

PLACEMENT_REWARD_RULE_VERSION: Final = "room_match_placement_reward_v2"

_PLACEMENT_REWARDS: Final = {
    MatchGrade.G1: (300, 200, 140, 80),
    MatchGrade.G2: (240, 160, 120, 80),
    MatchGrade.G3: (200, 120, 100, 80),
    MatchGrade.LISTED: (200, 120, 100, 80),
}


def calculate_match_placement_reward(*, grade: MatchGrade, rank: int, field_size: int) -> int:
    """Return one Persona placement reward from the confirmed complete board."""

    canonical_grade = MatchGrade(grade)
    for value, field_name in ((rank, "rank"), (field_size, "field_size")):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field_name} must be a positive integer.")
    if rank > field_size:
        raise ValueError("rank cannot exceed field_size.")
    if canonical_grade is MatchGrade.OP:
        return 0
    table = _PLACEMENT_REWARDS[canonical_grade]
    reward = table[min(rank, 4) - 1]
    return reward // 2 if field_size < 9 else reward
