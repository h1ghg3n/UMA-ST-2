"""Pure Special WIN5 judgement and score contract tests."""

import pytest

from uma_st2.domain.win5 import (
    Win5JudgementOutcome,
    Win5ResultInvariantError,
    Win5SpecialResultWinner,
    Win5SpecialScoringPick,
    Win5SubmissionInvariantError,
    score_special_submission,
)


def _results() -> tuple[Win5SpecialResultWinner, ...]:
    return (
        Win5SpecialResultWinner(id=201, race_id=101, gate_number=3),
        Win5SpecialResultWinner(id=202, race_id=102, gate_number=7),
        Win5SpecialResultWinner(id=203, race_id=103, gate_number=1),
    )


def test_partial_special_submission_distinguishes_exact_off_board_and_missing() -> None:
    score = score_special_submission(
        race_ids=(103, 101, 102),
        picks=(
            Win5SpecialScoringPick(id=301, race_id=101, gate_number=3),
            Win5SpecialScoringPick(id=302, race_id=102, gate_number=4),
        ),
        results=tuple(reversed(_results())),
    )

    assert score.race_ids == (101, 102, 103)
    assert tuple(item.outcome for item in score.items) == (
        Win5JudgementOutcome.EXACT,
        Win5JudgementOutcome.OFF_BOARD,
        Win5JudgementOutcome.MISSING,
    )
    assert tuple(item.submission_pick_id for item in score.items) == (301, 302, None)
    assert tuple(item.matched_result_id for item in score.items) == (201, None, None)
    assert (score.exact_count, score.off_board_count, score.missing_count) == (1, 1, 1)
    assert score.season_score_delta == 1
    assert score.top1_score_delta == 1
    assert score.circle_point_reward == 0


def test_all_exact_special_submission_scores_both_projections_without_reward() -> None:
    score = score_special_submission(
        race_ids=(101, 102, 103),
        picks=tuple(
            Win5SpecialScoringPick(id=300 + index, race_id=result.race_id, gate_number=result.gate_number)
            for index, result in enumerate(_results(), start=1)
        ),
        results=_results(),
    )

    assert score.exact_count == 3
    assert score.season_score_delta == 3
    assert score.top1_score_delta == 3
    assert score.circle_point_reward == 0


def test_empty_partial_submission_creates_one_missing_fact_per_race() -> None:
    score = score_special_submission(
        race_ids=(101, 102, 103),
        picks=(),
        results=_results(),
    )

    assert [item.race_id for item in score.items] == [101, 102, 103]
    assert all(item.outcome == Win5JudgementOutcome.MISSING for item in score.items)
    assert score.missing_count == 3
    assert score.season_score_delta == 0
    assert score.top1_score_delta == 0


def test_mixed_void_submission_preserves_pick_and_scores_only_non_void_races() -> None:
    score = score_special_submission(
        race_ids=(103, 101, 102),
        void_race_ids=(102,),
        picks=(
            Win5SpecialScoringPick(id=301, race_id=101, gate_number=3),
            Win5SpecialScoringPick(id=302, race_id=102, gate_number=99),
        ),
        results=(_results()[2], _results()[0]),
    )

    assert score.race_ids == (101, 102, 103)
    assert tuple(item.outcome for item in score.items) == (
        Win5JudgementOutcome.EXACT,
        Win5JudgementOutcome.VOID,
        Win5JudgementOutcome.MISSING,
    )
    assert tuple(item.submission_pick_id for item in score.items) == (301, 302, None)
    assert tuple(item.matched_result_id for item in score.items) == (201, None, None)
    assert (score.exact_count, score.off_board_count, score.missing_count, score.void_count) == (1, 0, 1, 1)
    assert score.season_score_delta == score.top1_score_delta == 1
    assert score.circle_point_reward == 0


def test_special_scoring_rejects_incomplete_result_and_foreign_or_duplicate_pick() -> None:
    with pytest.raises(Win5ResultInvariantError, match="every non-void Race"):
        score_special_submission(
            race_ids=(101, 102, 103),
            picks=(),
            results=_results()[:2],
        )

    with pytest.raises(Win5SubmissionInvariantError, match="foreign Race"):
        score_special_submission(
            race_ids=(101, 102, 103),
            picks=(Win5SpecialScoringPick(id=301, race_id=999, gate_number=3),),
            results=_results(),
        )

    with pytest.raises(Win5ResultInvariantError, match="all-void"):
        score_special_submission(
            race_ids=(101, 102, 103),
            void_race_ids=(101, 102, 103),
            picks=(),
            results=(),
        )

    with pytest.raises(Win5ResultInvariantError, match="foreign Round"):
        score_special_submission(
            race_ids=(101, 102, 103),
            void_race_ids=(999,),
            picks=(),
            results=_results(),
        )

    with pytest.raises(Win5SubmissionInvariantError, match="at most one pick"):
        score_special_submission(
            race_ids=(101, 102, 103),
            picks=(
                Win5SpecialScoringPick(id=301, race_id=101, gate_number=3),
                Win5SpecialScoringPick(id=302, race_id=101, gate_number=4),
            ),
            results=_results(),
        )
