from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from umacircle_bot.domain.errors import MatchPlacementRewardError

FULL_REWARD_MIN_PARTICIPANT_COUNT = 9

MATCH_PLACEMENT_REWARD_TABLE: dict[str, tuple[int, int, int, int]] = {
    "GI": (300, 200, 140, 80),
    "GII": (240, 160, 120, 80),
    "GIII": (200, 120, 100, 80),
}


@dataclass(frozen=True, slots=True)
class MatchPlacementCandidate:
    result_business_key: str
    game_account_business_key: str
    owner_business_key: str
    entry_number: int
    rank: int


@dataclass(frozen=True, slots=True)
class MatchPlacementSelection:
    candidate: MatchPlacementCandidate
    amount: int
    suppressed_result_business_keys: tuple[str, ...]


def calculate_match_placement_reward(*, grade: str, rank: int, participant_count: int) -> int | None:
    """Return the x10 Circle Point reward for one supported final result.

    OP is an explicit no-reward grade. Other unsupported grades are rejected
    rather than inferred from a nearby grade.
    """

    normalized_grade = _normalize_grade(grade)
    normalized_rank = _positive_int(rank, field="rank")
    if normalized_grade == "OP":
        _nonnegative_int(participant_count, field="participant count")
        return None

    normalized_participants = _positive_int(participant_count, field="participant count")
    if normalized_rank > normalized_participants:
        raise MatchPlacementRewardError("rank must not exceed participant count")
    rewards = MATCH_PLACEMENT_REWARD_TABLE.get(normalized_grade)
    if rewards is None:
        raise MatchPlacementRewardError(f"unsupported placement reward grade: {normalized_grade}")
    reward_index = min(normalized_rank, 4) - 1
    reward = rewards[reward_index]
    return reward if normalized_participants >= FULL_REWARD_MIN_PARTICIPANT_COUNT else reward // 2


def allocate_match_placement_rewards(
    candidates: Sequence[MatchPlacementCandidate],
    *,
    grade: str,
    participant_count: int,
) -> tuple[MatchPlacementSelection, ...]:
    """Select one best Result per owner while preserving account provenance."""

    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise MatchPlacementRewardError("placement candidates must be a sequence")
    normalized = tuple(_normalize_candidate(candidate) for candidate in candidates)
    result_keys = [candidate.result_business_key for candidate in normalized]
    entry_numbers = [candidate.entry_number for candidate in normalized]
    if len(result_keys) != len(set(result_keys)):
        raise MatchPlacementRewardError("placement candidates contain duplicate Result business keys")
    if len(entry_numbers) != len(set(entry_numbers)):
        raise MatchPlacementRewardError("placement candidates contain duplicate entry numbers")

    by_owner: dict[str, list[MatchPlacementCandidate]] = defaultdict(list)
    for candidate in normalized:
        by_owner[candidate.owner_business_key].append(candidate)

    selections: list[MatchPlacementSelection] = []
    for owner_key in sorted(by_owner):
        owner_candidates = sorted(
            by_owner[owner_key],
            key=lambda candidate: (candidate.rank, candidate.entry_number),
        )
        selected = owner_candidates[0]
        amount = calculate_match_placement_reward(
            grade=grade,
            rank=selected.rank,
            participant_count=participant_count,
        )
        if amount is None:
            continue
        selections.append(
            MatchPlacementSelection(
                candidate=selected,
                amount=amount,
                suppressed_result_business_keys=tuple(
                    candidate.result_business_key for candidate in owner_candidates[1:]
                ),
            )
        )
    return tuple(selections)


def placement_reward_policy_basis() -> dict[str, object]:
    return {
        "storage_scale": 10,
        "full_reward_min_participant_count": FULL_REWARD_MIN_PARTICIPANT_COUNT,
        "reduced_reward_numerator": 1,
        "reduced_reward_denominator": 2,
        "op_reward": 0,
        "reward_table": {
            grade: {
                "rank_1": rewards[0],
                "rank_2": rewards[1],
                "rank_3": rewards[2],
                "rank_4_or_lower": rewards[3],
            }
            for grade, rewards in sorted(MATCH_PLACEMENT_REWARD_TABLE.items())
        },
        "persona_cap": "best_rank_then_entry_number",
    }


def _normalize_candidate(candidate: MatchPlacementCandidate) -> MatchPlacementCandidate:
    if not isinstance(candidate, MatchPlacementCandidate):
        raise MatchPlacementRewardError("placement candidate is invalid")
    return MatchPlacementCandidate(
        result_business_key=_required_text(candidate.result_business_key, field="Result business key"),
        game_account_business_key=_required_text(
            candidate.game_account_business_key,
            field="GameAccount business key",
        ),
        owner_business_key=_required_text(candidate.owner_business_key, field="owner business key"),
        entry_number=_positive_int(candidate.entry_number, field="entry number"),
        rank=_positive_int(candidate.rank, field="rank"),
    )


def _normalize_grade(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MatchPlacementRewardError("placement reward grade is required")
    return value.strip().upper()


def _required_text(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MatchPlacementRewardError(f"{field} is required")
    return value.strip()


def _positive_int(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MatchPlacementRewardError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MatchPlacementRewardError(f"{field} must be a nonnegative integer")
    return value
