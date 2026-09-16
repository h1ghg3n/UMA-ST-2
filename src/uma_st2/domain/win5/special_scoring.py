"""Pure Special WIN5 judgement and scoring rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from .enums import Win5JudgementOutcome
from .errors import Win5ResultInvariantError, Win5SubmissionInvariantError
from .special_results import Win5SpecialResultWinner, fingerprint_special_result

WIN5_SPECIAL_SCORING_POLICY_VERSION: Final = "special-winner-plus-one-v1"
WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION: Final = "special-winner-plus-one-v2"
WIN5_SPECIAL_REWARD_POLICY_VERSION: Final = "special-no-circle-point-v1"


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


@dataclass(frozen=True, slots=True)
class Win5SpecialScoringPick:
    """One persisted Race winner prediction participating in Special scoring."""

    id: int
    race_id: int
    gate_number: int

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.race_id, field_name="race_id")
        _require_positive_int(self.gate_number, field_name="gate_number")


@dataclass(frozen=True, slots=True)
class Win5SpecialJudgementItem:
    """One canonical Special Race and its immutable judgement fact."""

    race_id: int
    submission_pick_id: int | None
    matched_result_id: int | None
    outcome: Win5JudgementOutcome
    season_score_delta: int

    def __post_init__(self) -> None:
        _require_positive_int(self.race_id, field_name="race_id")
        if self.submission_pick_id is not None:
            _require_positive_int(self.submission_pick_id, field_name="submission_pick_id")
        if self.matched_result_id is not None:
            _require_positive_int(self.matched_result_id, field_name="matched_result_id")
        object.__setattr__(self, "outcome", Win5JudgementOutcome(self.outcome))
        if self.outcome == Win5JudgementOutcome.VOID:
            expected = (self.submission_pick_id, None, 0)
        elif self.outcome == Win5JudgementOutcome.MISSING:
            expected = (None, None, 0)
        elif self.outcome == Win5JudgementOutcome.OFF_BOARD:
            expected = (self.submission_pick_id, None, 0)
        elif self.outcome == Win5JudgementOutcome.EXACT:
            expected = (self.submission_pick_id, self.matched_result_id, 1)
        else:
            raise Win5SubmissionInvariantError("Special judgement does not use wrong_position.")
        actual = (self.submission_pick_id, self.matched_result_id, self.season_score_delta)
        if (
            actual != expected
            or (
                self.outcome not in {Win5JudgementOutcome.MISSING, Win5JudgementOutcome.VOID}
                and self.submission_pick_id is None
            )
            or (self.outcome == Win5JudgementOutcome.EXACT and self.matched_result_id is None)
        ):
            raise Win5SubmissionInvariantError("Special judgement provenance does not match its outcome.")


@dataclass(frozen=True, slots=True)
class Win5SpecialSubmissionScore:
    """Complete score facts for one accepted Special Submission."""

    race_ids: tuple[int, ...]
    items: tuple[Win5SpecialJudgementItem, ...] = field(default_factory=tuple)
    exact_count: int = 0
    off_board_count: int = 0
    missing_count: int = 0
    void_count: int = 0
    season_score_delta: int = 0
    top1_score_delta: int = 0
    circle_point_reward: int = 0

    def __post_init__(self) -> None:
        canonical_race_ids = _canonical_race_ids(self.race_ids)
        object.__setattr__(self, "race_ids", canonical_race_ids)
        object.__setattr__(self, "items", tuple(self.items))
        if tuple(item.race_id for item in self.items) != canonical_race_ids:
            raise Win5SubmissionInvariantError("Special score items must cover every canonical Race in order.")
        outcomes = tuple(item.outcome for item in self.items)
        if (
            self.exact_count != outcomes.count(Win5JudgementOutcome.EXACT)
            or self.off_board_count != outcomes.count(Win5JudgementOutcome.OFF_BOARD)
            or self.missing_count != outcomes.count(Win5JudgementOutcome.MISSING)
            or self.void_count != outcomes.count(Win5JudgementOutcome.VOID)
            or self.season_score_delta != sum(item.season_score_delta for item in self.items)
        ):
            raise Win5SubmissionInvariantError("Special score totals must equal their Race-level judgement items.")
        validate_special_score_totals(
            race_count=len(canonical_race_ids),
            exact_count=self.exact_count,
            off_board_count=self.off_board_count,
            missing_count=self.missing_count,
            void_count=self.void_count,
            season_score_delta=self.season_score_delta,
            top1_score_delta=self.top1_score_delta,
            circle_point_reward=self.circle_point_reward,
        )


def _canonical_race_ids(race_ids: tuple[int, ...]) -> tuple[int, ...]:
    canonical = tuple(sorted(race_ids))
    if not canonical:
        raise Win5ResultInvariantError("Special scoring requires at least one canonical Race.")
    for race_id in canonical:
        _require_positive_int(race_id, field_name="race_id")
    if len(set(canonical)) != len(canonical):
        raise Win5ResultInvariantError("Special scoring Race IDs must be unique.")
    return canonical


def validate_special_score_totals(
    *,
    race_count: int,
    exact_count: int,
    off_board_count: int,
    missing_count: int,
    void_count: int = 0,
    season_score_delta: int,
    top1_score_delta: int,
    circle_point_reward: int,
) -> None:
    """Validate aggregate Special scoring facts without persistence concerns."""

    _require_positive_int(race_count, field_name="race_count")
    counts = (exact_count, off_board_count, missing_count, void_count)
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts):
        raise Win5SubmissionInvariantError("Special judgement counts must be non-negative integers.")
    if sum(counts) != race_count:
        raise Win5SubmissionInvariantError("Special judgement counts must equal the canonical Race count.")
    if season_score_delta != exact_count or top1_score_delta != exact_count:
        raise Win5SubmissionInvariantError("Special Season and TOP1 deltas must equal the exact Race count.")
    if circle_point_reward != 0:
        raise Win5SubmissionInvariantError("Special scoring cannot award Circle Point.")


def score_special_submission(
    *,
    race_ids: tuple[int, ...],
    void_race_ids: tuple[int, ...] = (),
    picks: tuple[Win5SpecialScoringPick, ...],
    results: tuple[Win5SpecialResultWinner, ...],
) -> Win5SpecialSubmissionScore:
    """Judge one partial Special Submission against a complete winner bundle."""

    canonical_race_ids = _canonical_race_ids(tuple(race_ids))
    canonical_void_race_ids = tuple(sorted(void_race_ids))
    if len(set(canonical_void_race_ids)) != len(canonical_void_race_ids):
        raise Win5ResultInvariantError("Special scoring void Race IDs must be unique.")
    if any(race_id not in set(canonical_race_ids) for race_id in canonical_void_race_ids):
        raise Win5ResultInvariantError("Special scoring void Race belongs to a foreign Round.")
    if len(canonical_void_race_ids) == len(canonical_race_ids):
        raise Win5ResultInvariantError("An all-void Special Round cannot be scored.")
    non_void_race_ids = tuple(race_id for race_id in canonical_race_ids if race_id not in set(canonical_void_race_ids))
    canonical_results = tuple(sorted(results, key=lambda result: result.race_id))
    fingerprint_special_result(canonical_results)
    if tuple(result.race_id for result in canonical_results) != non_void_race_ids:
        raise Win5ResultInvariantError("Special scoring requires exactly one Result for every non-void Race.")

    canonical_picks = tuple(picks)
    if len({pick.id for pick in canonical_picks}) != len(canonical_picks):
        raise Win5SubmissionInvariantError("Special scoring pick IDs must be unique.")
    if len({pick.race_id for pick in canonical_picks}) != len(canonical_picks):
        raise Win5SubmissionInvariantError("Special scoring allows at most one pick per Race.")
    if any(pick.race_id not in set(canonical_race_ids) for pick in canonical_picks):
        raise Win5SubmissionInvariantError("Special scoring pick belongs to a foreign Race.")

    results_by_race_id = {result.race_id: result for result in canonical_results}
    picks_by_race_id = {pick.race_id: pick for pick in canonical_picks}
    items: list[Win5SpecialJudgementItem] = []
    for race_id in canonical_race_ids:
        pick = picks_by_race_id.get(race_id)
        if race_id in set(canonical_void_race_ids):
            items.append(
                Win5SpecialJudgementItem(
                    race_id=race_id,
                    submission_pick_id=None if pick is None else pick.id,
                    matched_result_id=None,
                    outcome=Win5JudgementOutcome.VOID,
                    season_score_delta=0,
                )
            )
            continue
        result = results_by_race_id[race_id]
        if pick is None:
            items.append(
                Win5SpecialJudgementItem(
                    race_id=race_id,
                    submission_pick_id=None,
                    matched_result_id=None,
                    outcome=Win5JudgementOutcome.MISSING,
                    season_score_delta=0,
                )
            )
        elif pick.gate_number == result.gate_number:
            items.append(
                Win5SpecialJudgementItem(
                    race_id=race_id,
                    submission_pick_id=pick.id,
                    matched_result_id=result.id,
                    outcome=Win5JudgementOutcome.EXACT,
                    season_score_delta=1,
                )
            )
        else:
            items.append(
                Win5SpecialJudgementItem(
                    race_id=race_id,
                    submission_pick_id=pick.id,
                    matched_result_id=None,
                    outcome=Win5JudgementOutcome.OFF_BOARD,
                    season_score_delta=0,
                )
            )

    outcomes = tuple(item.outcome for item in items)
    exact_count = outcomes.count(Win5JudgementOutcome.EXACT)
    return Win5SpecialSubmissionScore(
        race_ids=canonical_race_ids,
        items=tuple(items),
        exact_count=exact_count,
        off_board_count=outcomes.count(Win5JudgementOutcome.OFF_BOARD),
        missing_count=outcomes.count(Win5JudgementOutcome.MISSING),
        void_count=outcomes.count(Win5JudgementOutcome.VOID),
        season_score_delta=exact_count,
        top1_score_delta=exact_count,
        circle_point_reward=0,
    )
