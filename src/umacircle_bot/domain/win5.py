import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from umacircle_bot.domain.errors import Win5RuleError

MAX_ENTRY_NUMBER = 2_147_483_647
MAX_OPEN_ROUNDS_PER_SEASON = 3
WIN5_RESULT_COUNT = 5


class Win5SeasonStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class Win5RoundStatus(StrEnum):
    SETUP = "setup"
    OPEN = "open"
    CLOSED = "closed"
    RESULT_ENTERED = "result_entered"
    SCORED = "scored"


class Win5RoundType(StrEnum):
    NORMAL = "normal"
    SPECIAL = "special"


class Win5PredictionTier(StrEnum):
    TOP1 = "top1"
    TOP3 = "top3"
    TOP5 = "top5"
    SPECIAL_WINNER = "special_winner"


class Win5SubmissionStatus(StrEnum):
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"


PICK_COUNT_BY_TIER = {
    Win5PredictionTier.TOP1: 1,
    Win5PredictionTier.TOP3: 3,
    Win5PredictionTier.TOP5: 5,
    Win5PredictionTier.SPECIAL_WINNER: 1,
}
NORMAL_PREDICTION_TIERS = frozenset(
    {
        Win5PredictionTier.TOP1,
        Win5PredictionTier.TOP3,
        Win5PredictionTier.TOP5,
    }
)
CIRCLE_POINT_REWARD_BY_EXACT_POSITION_COUNT = {
    0: 0,
    1: 10,
    2: 20,
    3: 40,
    4: 70,
    5: 100,
}


@dataclass(frozen=True, slots=True)
class Win5PredictionJudgement:
    prediction_tier: Win5PredictionTier
    picks: tuple[int, ...]
    exact_position_count: int
    on_board_wrong_position_count: int
    off_board_count: int
    season_score_delta: int
    top1_score_delta: int

    @property
    def hit_count(self) -> int:
        return self.exact_position_count

    @property
    def is_hit(self) -> bool:
        return self.exact_position_count == len(self.picks)

    @property
    def score_delta(self) -> int:
        return self.season_score_delta


def normalize_prediction_tier(
    prediction_tier: Win5PredictionTier | str,
    *,
    allow_special: bool = False,
) -> Win5PredictionTier:
    try:
        normalized = Win5PredictionTier(prediction_tier)
    except (TypeError, ValueError) as exc:
        raise Win5RuleError("WIN5 prediction tier is not supported") from exc
    if normalized is Win5PredictionTier.SPECIAL_WINNER and not allow_special:
        raise Win5RuleError("WIN5 special winner tier is not valid for a normal round")
    return normalized


def normalize_win5_picks(
    picks: Sequence[int],
    *,
    prediction_tier: Win5PredictionTier | str = Win5PredictionTier.TOP5,
) -> tuple[int, ...]:
    tier = normalize_prediction_tier(prediction_tier, allow_special=True)
    normalized_picks = _normalize_entry_numbers(picks, field_name="WIN5 picks")
    expected_count = PICK_COUNT_BY_TIER[tier]
    if len(normalized_picks) != expected_count:
        raise Win5RuleError(f"WIN5 {tier.value} requires exactly {expected_count} ordered picks")
    return normalized_picks


def normalize_win5_result_order(result_order: Sequence[int]) -> tuple[int, ...]:
    normalized_result = _normalize_entry_numbers(result_order, field_name="WIN5 result order")
    if len(normalized_result) != WIN5_RESULT_COUNT:
        raise Win5RuleError(f"WIN5 result requires exactly {WIN5_RESULT_COUNT} finishers")
    return normalized_result


def win5_circle_point_reward(
    *,
    prediction_tier: Win5PredictionTier | str,
    exact_position_count: int,
) -> int:
    """Return the post-scale persisted Circle Point reward for a normal WIN5 submission."""
    tier = normalize_prediction_tier(prediction_tier, allow_special=True)
    if tier is Win5PredictionTier.SPECIAL_WINNER:
        return 0
    if not isinstance(exact_position_count, int) or isinstance(exact_position_count, bool):
        raise Win5RuleError("WIN5 exact position count must be an integer")
    if exact_position_count < 0 or exact_position_count > PICK_COUNT_BY_TIER[tier]:
        raise Win5RuleError("WIN5 exact position count is outside the prediction tier")
    return CIRCLE_POINT_REWARD_BY_EXACT_POSITION_COUNT[exact_position_count]


def normalize_win5_special_result_order(
    result_order: Sequence[int],
) -> tuple[int, ...]:
    normalized_result = _normalize_entry_numbers(
        result_order,
        field_name="WIN5 special result",
    )
    if len(normalized_result) != 1:
        raise Win5RuleError("WIN5 special result requires exactly one winner")
    return normalized_result


def judge_win5_prediction(
    picks: Sequence[int],
    result_order: Sequence[int],
    *,
    prediction_tier: Win5PredictionTier | str = Win5PredictionTier.TOP5,
) -> Win5PredictionJudgement:
    tier = normalize_prediction_tier(prediction_tier)
    normalized_picks = normalize_win5_picks(picks, prediction_tier=tier)
    normalized_result = normalize_win5_result_order(result_order)
    board = set(normalized_result)
    exact_position_count = 0
    on_board_wrong_position_count = 0
    off_board_count = 0
    for position, pick in enumerate(normalized_picks):
        if pick == normalized_result[position]:
            exact_position_count += 1
        elif pick in board:
            on_board_wrong_position_count += 1
        else:
            off_board_count += 1
    season_score_delta = exact_position_count * 3 + on_board_wrong_position_count
    top1_score_delta = 3 if tier is Win5PredictionTier.TOP1 and exact_position_count == 1 else 0
    return Win5PredictionJudgement(
        prediction_tier=tier,
        picks=normalized_picks,
        exact_position_count=exact_position_count,
        on_board_wrong_position_count=on_board_wrong_position_count,
        off_board_count=off_board_count,
        season_score_delta=season_score_delta,
        top1_score_delta=top1_score_delta,
    )


def judge_win5_special_winner(
    *,
    predicted_entry_number: int,
    result_order: Sequence[int],
) -> Win5PredictionJudgement:
    normalized_picks = normalize_win5_picks(
        (predicted_entry_number,),
        prediction_tier=Win5PredictionTier.SPECIAL_WINNER,
    )
    normalized_result = normalize_win5_special_result_order(result_order)
    is_winner = normalized_picks[0] == normalized_result[0]
    return Win5PredictionJudgement(
        prediction_tier=Win5PredictionTier.SPECIAL_WINNER,
        picks=normalized_picks,
        exact_position_count=int(is_winner),
        on_board_wrong_position_count=0,
        off_board_count=int(not is_winner),
        season_score_delta=int(is_winner),
        top1_score_delta=int(is_winner),
    )


def is_win5_prediction_hit(
    picks: Sequence[int],
    result_order: Sequence[int],
    *,
    prediction_tier: Win5PredictionTier | str = Win5PredictionTier.TOP5,
) -> bool:
    return judge_win5_prediction(
        picks,
        result_order,
        prediction_tier=prediction_tier,
    ).is_hit


def build_win5_picks_fingerprint(
    *,
    prediction_tier: Win5PredictionTier | str,
    picks: Sequence[int],
) -> str:
    tier = normalize_prediction_tier(prediction_tier, allow_special=True)
    normalized_picks = normalize_win5_picks(picks, prediction_tier=tier)
    return _stable_fingerprint(
        {
            "prediction_tier": tier.value,
            "picks": normalized_picks,
        }
    )


def build_win5_submission_fingerprint(
    *,
    game_account_id: int,
    season_id: int,
    round_id: int,
    prediction_tier: Win5PredictionTier | str,
    picks: Sequence[int],
    special_round_race_id: int | None = None,
) -> str:
    tier = normalize_prediction_tier(prediction_tier, allow_special=True)
    normalized_picks = normalize_win5_picks(picks, prediction_tier=tier)
    return _stable_fingerprint(
        {
            "game_account_id": game_account_id,
            "season_id": season_id,
            "round_id": round_id,
            "prediction_tier": tier.value,
            "special_round_race_id": special_round_race_id,
            "picks": normalized_picks,
        }
    )


def _normalize_entry_numbers(values: Sequence[int], *, field_name: str) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise Win5RuleError(f"{field_name} must be a sequence of integers")

    normalized_values = list(values)
    if any(not _is_supported_entry_number(value) for value in normalized_values):
        raise Win5RuleError(f"{field_name} must contain positive integers")
    if len(set(normalized_values)) != len(normalized_values):
        raise Win5RuleError(f"{field_name} must not contain duplicate entry numbers")
    return tuple(normalized_values)


def _is_supported_entry_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_ENTRY_NUMBER


def _stable_fingerprint(payload: dict[str, object]) -> str:
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return sha256(serialized.encode("utf-8")).hexdigest()
