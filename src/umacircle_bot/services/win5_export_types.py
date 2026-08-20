from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Win5SeasonExportResult:
    export_run_id: int
    output_path: Path
    season_id: int
    season_number: int
    season_name: str
    round_count: int
    submission_count: int
    participant_count: int
    hall_of_fame_count: int
    sha256_checksum: str


@dataclass(frozen=True, slots=True)
class Win5StandingSnapshot:
    season_rank: int
    top1_rank: int
    game_account_id: int
    participant_name: str
    season_score: int
    top1_score: int
    participated_round_count: int
    perfect_top5_count: int
    latest_judged_at: datetime | None


@dataclass(frozen=True, slots=True)
class Win5RoundSnapshot:
    round_id: int
    round_number: int
    round_label: str | None
    round_type: str
    round_status: str
    race_id: int | None
    race_display_order: int | None
    race_name: str | None
    race_starts_at: datetime | None
    result_numbers: tuple[int, ...]
    result_labels: tuple[str, ...]
    accepted_submission_count: int
    cancelled_submission_count: int
    opens_at: datetime | None
    closes_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Win5SubmissionSnapshot:
    entry_id: int
    round_number: int
    round_label: str | None
    round_type: str
    race_name: str | None
    participant_name: str
    prediction_tier: str
    status: str
    pick_numbers: tuple[int, ...]
    pick_labels: tuple[str, ...]
    result_numbers: tuple[int, ...]
    result_labels: tuple[str, ...]
    exact_position_count: int | None
    on_board_wrong_position_count: int | None
    off_board_count: int | None
    season_score_delta: int | None
    top1_score_delta: int | None
    circle_point_reward: int
    submitted_at: datetime
    judged_at: datetime | None


@dataclass(frozen=True, slots=True)
class Win5SeasonSnapshot:
    season_id: int
    season_number: int
    season_name: str
    season_status: str
    starts_at: datetime | None
    ends_at: datetime | None
    generated_at: datetime
    round_count: int
    scored_round_count: int
    participant_count: int
    standings: tuple[Win5StandingSnapshot, ...]
    rounds: tuple[Win5RoundSnapshot, ...]
    submissions: tuple[Win5SubmissionSnapshot, ...]
    hall_of_fame: tuple[Win5SubmissionSnapshot, ...]
