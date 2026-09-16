"""Pure Normal WIN5 judgement, scoring, reward, and result fingerprint rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Final

from .enums import Win5JudgementOutcome, Win5SubmissionTier
from .errors import Win5ResultInvariantError, Win5SubmissionInvariantError

WIN5_NORMAL_SCORING_POLICY_VERSION: Final = "normal-3-1-0-v1"
WIN5_NORMAL_REWARD_POLICY_VERSION: Final = "normal-exact-reward-v1"

_RESULT_POSITIONS = (1, 2, 3, 4, 5)
_TIER_CAPACITY = {
    Win5SubmissionTier.TOP1: 1,
    Win5SubmissionTier.TOP3: 3,
    Win5SubmissionTier.TOP5: 5,
}
_REWARD_BY_EXACT_COUNT = (0, 10, 20, 40, 70, 100)


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


@dataclass(frozen=True, slots=True)
class Win5NormalResultPlacement:
    """One persisted authoritative Normal result row."""

    id: int
    position: int
    race_entry_id: int

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.position, field_name="position")
        _require_positive_int(self.race_entry_id, field_name="race_entry_id")


@dataclass(frozen=True, slots=True)
class Win5NormalScoringPick:
    """One actual persisted pick participating in Normal scoring."""

    id: int
    position: int
    race_entry_id: int

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.position, field_name="position")
        _require_positive_int(self.race_entry_id, field_name="race_entry_id")


@dataclass(frozen=True, slots=True)
class Win5NormalJudgementItem:
    """One expected tier position and its immutable judgement fact."""

    position: int
    submission_pick_id: int | None
    matched_result_id: int | None
    outcome: Win5JudgementOutcome
    season_score_delta: int

    def __post_init__(self) -> None:
        _require_positive_int(self.position, field_name="position")
        if self.submission_pick_id is not None:
            _require_positive_int(self.submission_pick_id, field_name="submission_pick_id")
        if self.matched_result_id is not None:
            _require_positive_int(self.matched_result_id, field_name="matched_result_id")
        object.__setattr__(self, "outcome", Win5JudgementOutcome(self.outcome))
        if self.outcome == Win5JudgementOutcome.MISSING:
            expected = (None, None, 0)
        elif self.outcome == Win5JudgementOutcome.OFF_BOARD:
            expected = (self.submission_pick_id, None, 0)
        elif self.outcome == Win5JudgementOutcome.WRONG_POSITION:
            expected = (self.submission_pick_id, self.matched_result_id, 1)
        else:
            expected = (self.submission_pick_id, self.matched_result_id, 3)
        actual = (self.submission_pick_id, self.matched_result_id, self.season_score_delta)
        if (
            actual != expected
            or (self.outcome != Win5JudgementOutcome.MISSING and self.submission_pick_id is None)
            or (
                self.outcome in {Win5JudgementOutcome.EXACT, Win5JudgementOutcome.WRONG_POSITION}
                and self.matched_result_id is None
            )
        ):
            raise Win5SubmissionInvariantError("Normal judgement item provenance does not match its outcome.")


@dataclass(frozen=True, slots=True)
class Win5NormalSubmissionScore:
    """Complete score and reward facts for one accepted Submission."""

    tier: Win5SubmissionTier
    items: tuple[Win5NormalJudgementItem, ...] = field(default_factory=tuple)
    exact_count: int = 0
    wrong_position_count: int = 0
    off_board_count: int = 0
    missing_count: int = 0
    season_score_delta: int = 0
    top1_score_delta: int = 0
    circle_point_reward: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "tier", Win5SubmissionTier(self.tier))
        object.__setattr__(self, "items", tuple(self.items))
        capacity = normal_tier_capacity(self.tier)
        if tuple(item.position for item in self.items) != tuple(range(1, capacity + 1)):
            raise Win5SubmissionInvariantError("Normal score items must cover every expected tier position in order.")
        outcomes = tuple(item.outcome for item in self.items)
        if (
            self.exact_count != outcomes.count(Win5JudgementOutcome.EXACT)
            or self.wrong_position_count != outcomes.count(Win5JudgementOutcome.WRONG_POSITION)
            or self.off_board_count != outcomes.count(Win5JudgementOutcome.OFF_BOARD)
            or self.missing_count != outcomes.count(Win5JudgementOutcome.MISSING)
            or self.season_score_delta != sum(item.season_score_delta for item in self.items)
        ):
            raise Win5SubmissionInvariantError("Normal score totals must equal their position-level judgement items.")
        validate_normal_score_totals(
            tier=self.tier,
            exact_count=self.exact_count,
            wrong_position_count=self.wrong_position_count,
            off_board_count=self.off_board_count,
            missing_count=self.missing_count,
            season_score_delta=self.season_score_delta,
            top1_score_delta=self.top1_score_delta,
            circle_point_reward=self.circle_point_reward,
        )


def normal_tier_capacity(tier: Win5SubmissionTier) -> int:
    """Return the expected Normal positions for a supported tier."""

    try:
        canonical_tier = Win5SubmissionTier(tier)
    except ValueError as exc:
        raise Win5SubmissionInvariantError(f"Unknown WIN5 Submission tier: {tier!r}.") from exc
    capacity = _TIER_CAPACITY.get(canonical_tier)
    if capacity is None:
        raise Win5SubmissionInvariantError("Normal scoring requires TOP1, TOP3, or TOP5 tier.")
    return capacity


def validate_normal_score_totals(
    *,
    tier: Win5SubmissionTier,
    exact_count: int,
    wrong_position_count: int,
    off_board_count: int,
    missing_count: int,
    season_score_delta: int,
    top1_score_delta: int,
    circle_point_reward: int,
) -> None:
    """Validate aggregate Normal scoring facts without persistence concerns."""

    capacity = normal_tier_capacity(tier)
    counts = (exact_count, wrong_position_count, off_board_count, missing_count)
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts):
        raise Win5SubmissionInvariantError("Normal judgement counts must be non-negative integers.")
    if sum(counts) != capacity:
        raise Win5SubmissionInvariantError("Normal judgement counts must equal the expected tier position count.")
    if season_score_delta != (exact_count * 3) + wrong_position_count:
        raise Win5SubmissionInvariantError("Normal Season score delta must equal exact*3 + wrong_position.")
    expected_top1_delta = 3 if tier == Win5SubmissionTier.TOP1 and exact_count == 1 else 0
    if top1_score_delta != expected_top1_delta:
        raise Win5SubmissionInvariantError("Normal TOP1 score delta does not match its tier and exact count.")
    if circle_point_reward != _REWARD_BY_EXACT_COUNT[exact_count]:
        raise Win5SubmissionInvariantError("Normal Circle Point reward does not match its exact count.")


def _ordered_valid_result(
    placements: tuple[Win5NormalResultPlacement, ...],
) -> tuple[Win5NormalResultPlacement, ...]:
    ordered = tuple(sorted(placements, key=lambda placement: placement.position))
    if tuple(placement.position for placement in ordered) != _RESULT_POSITIONS:
        raise Win5ResultInvariantError("Normal scoring requires exactly one Result for each position 1 through 5.")
    if len({placement.id for placement in ordered}) != len(_RESULT_POSITIONS):
        raise Win5ResultInvariantError("Normal scoring Result IDs must be unique.")
    if len({placement.race_entry_id for placement in ordered}) != len(_RESULT_POSITIONS):
        raise Win5ResultInvariantError("Normal scoring Result RaceEntries must be unique.")
    return ordered


def fingerprint_normal_result(
    placements: tuple[Win5NormalResultPlacement, ...],
) -> str:
    """Return the stable v1 fingerprint of one complete authoritative board."""

    ordered = _ordered_valid_result(tuple(placements))
    canonical = "win5-normal-result-v1\n" + "\n".join(
        f"{placement.position}:{placement.id}:{placement.race_entry_id}" for placement in ordered
    )
    return sha256(canonical.encode("ascii")).hexdigest()


def score_normal_submission(
    *,
    tier: Win5SubmissionTier,
    picks: tuple[Win5NormalScoringPick, ...],
    results: tuple[Win5NormalResultPlacement, ...],
) -> Win5NormalSubmissionScore:
    """Judge one partial Normal Submission against the complete top-five board."""

    try:
        canonical_tier = Win5SubmissionTier(tier)
    except ValueError as exc:
        raise Win5SubmissionInvariantError(f"Unknown WIN5 Submission tier: {tier!r}.") from exc
    capacity = normal_tier_capacity(canonical_tier)

    ordered_results = _ordered_valid_result(tuple(results))
    actual_by_position = {placement.position: placement for placement in ordered_results}
    actual_by_entry = {placement.race_entry_id: placement for placement in ordered_results}

    canonical_picks = tuple(picks)
    if len({pick.id for pick in canonical_picks}) != len(canonical_picks):
        raise Win5SubmissionInvariantError("Normal scoring pick IDs must be unique.")
    if len({pick.position for pick in canonical_picks}) != len(canonical_picks):
        raise Win5SubmissionInvariantError("Normal scoring allows at most one pick per expected position.")
    if len({pick.race_entry_id for pick in canonical_picks}) != len(canonical_picks):
        raise Win5SubmissionInvariantError("Normal scoring picks cannot repeat a RaceEntry.")
    if any(pick.position > capacity for pick in canonical_picks):
        raise Win5SubmissionInvariantError(
            f"{canonical_tier.value} scoring picks must use positions 1 through {capacity}."
        )

    picks_by_position = {pick.position: pick for pick in canonical_picks}
    items: list[Win5NormalJudgementItem] = []
    for position in range(1, capacity + 1):
        pick = picks_by_position.get(position)
        if pick is None:
            items.append(
                Win5NormalJudgementItem(
                    position=position,
                    submission_pick_id=None,
                    matched_result_id=None,
                    outcome=Win5JudgementOutcome.MISSING,
                    season_score_delta=0,
                )
            )
            continue

        exact_result = actual_by_position[position]
        if pick.race_entry_id == exact_result.race_entry_id:
            outcome = Win5JudgementOutcome.EXACT
            matched_result_id = exact_result.id
            delta = 3
        elif (matched_result := actual_by_entry.get(pick.race_entry_id)) is not None:
            outcome = Win5JudgementOutcome.WRONG_POSITION
            matched_result_id = matched_result.id
            delta = 1
        else:
            outcome = Win5JudgementOutcome.OFF_BOARD
            matched_result_id = None
            delta = 0
        items.append(
            Win5NormalJudgementItem(
                position=position,
                submission_pick_id=pick.id,
                matched_result_id=matched_result_id,
                outcome=outcome,
                season_score_delta=delta,
            )
        )

    outcomes = tuple(item.outcome for item in items)
    exact_count = outcomes.count(Win5JudgementOutcome.EXACT)
    return Win5NormalSubmissionScore(
        tier=canonical_tier,
        items=tuple(items),
        exact_count=exact_count,
        wrong_position_count=outcomes.count(Win5JudgementOutcome.WRONG_POSITION),
        off_board_count=outcomes.count(Win5JudgementOutcome.OFF_BOARD),
        missing_count=outcomes.count(Win5JudgementOutcome.MISSING),
        season_score_delta=sum(item.season_score_delta for item in items),
        top1_score_delta=(
            3 if canonical_tier == Win5SubmissionTier.TOP1 and items[0].outcome == Win5JudgementOutcome.EXACT else 0
        ),
        circle_point_reward=_REWARD_BY_EXACT_COUNT[exact_count],
    )
