"""Pure Normal WIN5 judgement and reward contract tests."""

import pytest

from uma_st2.domain.win5 import (
    Win5JudgementOutcome,
    Win5NormalResultPlacement,
    Win5NormalScoringPick,
    Win5ResultInvariantError,
    Win5SubmissionInvariantError,
    Win5SubmissionTier,
    fingerprint_normal_result,
    score_normal_submission,
)


def _results() -> tuple[Win5NormalResultPlacement, ...]:
    return tuple(
        Win5NormalResultPlacement(
            id=200 + position,
            position=position,
            race_entry_id=100 + position,
        )
        for position in range(1, 6)
    )


def test_partial_top5_distinguishes_all_judgements_and_keeps_tier_capacity() -> None:
    result = score_normal_submission(
        tier=Win5SubmissionTier.TOP5,
        picks=(
            Win5NormalScoringPick(id=301, position=1, race_entry_id=101),
            Win5NormalScoringPick(id=302, position=2, race_entry_id=103),
            Win5NormalScoringPick(id=303, position=3, race_entry_id=999),
            Win5NormalScoringPick(id=305, position=5, race_entry_id=105),
        ),
        results=_results(),
    )

    assert tuple(item.outcome for item in result.items) == (
        Win5JudgementOutcome.EXACT,
        Win5JudgementOutcome.WRONG_POSITION,
        Win5JudgementOutcome.OFF_BOARD,
        Win5JudgementOutcome.MISSING,
        Win5JudgementOutcome.EXACT,
    )
    assert tuple(item.submission_pick_id for item in result.items) == (301, 302, 303, None, 305)
    assert tuple(item.matched_result_id for item in result.items) == (201, 203, None, None, 205)
    assert (
        result.exact_count,
        result.wrong_position_count,
        result.off_board_count,
        result.missing_count,
    ) == (2, 1, 1, 1)
    assert result.season_score_delta == 7
    assert result.top1_score_delta == 0
    assert result.circle_point_reward == 20


@pytest.mark.parametrize(
    ("tier", "pick_count", "season_score", "top1_score", "reward"),
    [
        (Win5SubmissionTier.TOP1, 1, 3, 3, 10),
        (Win5SubmissionTier.TOP3, 3, 9, 0, 40),
        (Win5SubmissionTier.TOP5, 5, 15, 0, 100),
    ],
)
def test_exact_scores_respect_tier_projection_and_reward_policy(
    tier: Win5SubmissionTier,
    pick_count: int,
    season_score: int,
    top1_score: int,
    reward: int,
) -> None:
    result = score_normal_submission(
        tier=tier,
        picks=tuple(
            Win5NormalScoringPick(id=300 + position, position=position, race_entry_id=100 + position)
            for position in range(1, pick_count + 1)
        ),
        results=_results(),
    )

    assert len(result.items) == pick_count
    assert result.exact_count == pick_count
    assert result.season_score_delta == season_score
    assert result.top1_score_delta == top1_score
    assert result.circle_point_reward == reward


def test_empty_partial_top3_becomes_three_missing_facts_and_zero_reward() -> None:
    result = score_normal_submission(
        tier=Win5SubmissionTier.TOP3,
        picks=(),
        results=_results(),
    )

    assert [item.position for item in result.items] == [1, 2, 3]
    assert all(item.outcome == Win5JudgementOutcome.MISSING for item in result.items)
    assert all(item.submission_pick_id is None for item in result.items)
    assert result.missing_count == 3
    assert result.season_score_delta == 0
    assert result.circle_point_reward == 0


def test_result_fingerprint_is_order_independent_but_provenance_sensitive() -> None:
    results = _results()
    expected = fingerprint_normal_result(results)

    assert fingerprint_normal_result(tuple(reversed(results))) == expected
    changed = results[:-1] + (Win5NormalResultPlacement(id=999, position=5, race_entry_id=105),)
    assert fingerprint_normal_result(changed) != expected


def test_normal_scoring_rejects_incomplete_results_special_tier_and_invalid_picks() -> None:
    with pytest.raises(Win5ResultInvariantError, match="each position 1 through 5"):
        score_normal_submission(tier=Win5SubmissionTier.TOP1, picks=(), results=_results()[:4])

    with pytest.raises(Win5SubmissionInvariantError, match="TOP1, TOP3, or TOP5"):
        score_normal_submission(tier=Win5SubmissionTier.SPECIAL_WINNER, picks=(), results=_results())

    with pytest.raises(Win5SubmissionInvariantError, match="at most one pick"):
        score_normal_submission(
            tier=Win5SubmissionTier.TOP3,
            picks=(
                Win5NormalScoringPick(id=1, position=1, race_entry_id=101),
                Win5NormalScoringPick(id=2, position=1, race_entry_id=102),
            ),
            results=_results(),
        )
