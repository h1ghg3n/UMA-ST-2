from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class PersonaDTO:
    id: str
    display_name: str
    display_name_source: str
    status: str

    @property
    def short_id(self) -> str:
        from umacircle_bot.domain.personas import persona_short_id

        return persona_short_id(self.id)


@dataclass(frozen=True, slots=True)
class GameAccountDTO:
    id: int
    persona_id: str | None
    discord_account_id: int | None
    uma_pid: str | None
    nickname: str | None
    ingame_name: str | None
    identity_status: str


@dataclass(frozen=True, slots=True)
class GameAccountRegistrationDTO:
    id: int
    discord_account_id: int | None
    uma_pid: str | None
    nickname: str | None
    ingame_name: str | None
    identity_status: str
    circle_point_account_id: int
    circle_point_balance: int
    initial_grant_transaction_id: int | None
    initial_grant_amount: int
    was_created: bool


@dataclass(frozen=True, slots=True)
class AccountRegistrationRequestDTO:
    id: int
    guild_id: str
    requester_discord_user_id: str
    discord_nickname_snapshot: str
    submitted_uma_pid: str
    submitted_nickname: str | None
    submitted_ingame_name: str | None
    status: str
    reviewed_by_discord_user_id: str | None
    review_note: str | None
    accepted_persona_id: str | None
    accepted_persona_display_name: str | None
    accepted_persona_short_id: str | None
    accepted_game_account_id: int | None
    accepted_game_account_name: str | None
    initial_grant_amount: int
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class AccountRegistrationMutationDTO:
    action: str
    audit_id: int
    request: AccountRegistrationRequestDTO


@dataclass(frozen=True, slots=True)
class PlayerLinkRequestDTO:
    id: int
    guild_id: str
    requester_discord_user_id: str
    discord_nickname_snapshot: str
    submitted_ingame_name: str
    submitted_uma_pid: str
    submitted_nickname_chunk: str | None
    submitted_participation_hint: str | None
    requester_note: str | None
    status: str
    selected_game_account_id: int | None
    reviewed_by_discord_user_id: str | None
    review_note: str | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class PlayerLinkMutationDTO:
    action: str
    audit_id: int
    request: PlayerLinkRequestDTO


@dataclass(frozen=True, slots=True)
class PlayerLinkApprovalDTO:
    audit_id: int
    request: PlayerLinkRequestDTO
    game_account_id: int
    circle_point_account_id: int
    circle_point_balance: int
    ledger_count: int
    latest_transaction_id: int | None
    previous_discord_account_id: int
    current_discord_account_id: int


@dataclass(frozen=True, slots=True)
class PlayerLinkCandidateDTO:
    game_account_id: int
    candidate_fingerprint: str
    legacy_nickname: str | None
    ingame_name: str | None
    discord_nickname: str
    current_balance: int
    ledger_count: int
    last_activity_at: datetime | None
    import_source: str
    identity_status: str
    match_score: int
    match_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AccountInfoDTO:
    """Account fields safe to retain and render after the database session closes."""

    uma_pid: str | None
    nickname: str | None
    ingame_name: str | None
    identity_status: str
    circle_point_balance: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Win5AccountInfoDTO:
    season: Win5SeasonDTO | None
    season_score: int
    top1_score: int


@dataclass(frozen=True, slots=True)
class AccountOverviewDTO:
    account: AccountInfoDTO
    win5: Win5AccountInfoDTO


@dataclass(frozen=True, slots=True)
class MatchBetDTO:
    id: int
    event_id: int | None
    race_id: int
    persona_id: str
    game_account_id: int
    betting_mode: str
    bet_type: str
    numbers: tuple[int, ...]
    amount: int
    betting_window_version: int
    status: str


@dataclass(frozen=True, slots=True)
class CirclePointTransactionDTO:
    id: int
    persona_id: str
    game_account_id: int
    type: str
    amount: int
    reason: str | None
    source: str | None
    related_bet_id: int | None
    related_race_result_id: int | None
    created_by_discord_user_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CirclePointMutationDTO:
    transaction: CirclePointTransactionDTO
    uma_pid: str


@dataclass(frozen=True, slots=True)
class Win5SeasonDTO:
    id: int
    season_number: int
    name: str
    starts_at: datetime | None
    ends_at: datetime | None
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Win5RaceEntryDTO:
    entry_number: int
    display_name: str


@dataclass(frozen=True, slots=True)
class Win5RoundRaceDTO:
    id: int
    race_id: int
    display_order: int
    race_name: str
    race_starts_at: datetime | None
    entries: tuple[Win5RaceEntryDTO, ...]


@dataclass(frozen=True, slots=True)
class Win5RoundDTO:
    id: int
    season_id: int
    race_id: int | None
    round_type: str
    round_number: int
    round_label: str | None
    status: str
    opens_at: datetime | None
    closes_at: datetime | None
    race_name: str | None
    race_starts_at: datetime | None
    entries: tuple[Win5RaceEntryDTO, ...]
    special_races: tuple[Win5RoundRaceDTO, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Win5SeasonInfoDTO:
    season: Win5SeasonDTO
    total_round_count: int
    open_round_count: int
    latest_open_round: Win5RoundDTO | None
    season_score: int
    top1_score: int


@dataclass(frozen=True, slots=True)
class Win5EntryDTO:
    id: int
    season_id: int
    round_id: int
    game_account_id: int
    prediction_tier: str
    special_round_race_id: int | None
    special_race_id: int | None
    picks: tuple[int, ...]
    status: str
    idempotency_key: str
    cancelled_at: datetime | None
    cancelled_by_discord_user_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Win5MutationResultDTO:
    action: str
    audit_id: int
    season: Win5SeasonDTO | None = None
    round: Win5RoundDTO | None = None
    submission: Win5EntryDTO | None = None


@dataclass(frozen=True, slots=True)
class Win5ResultDTO:
    id: int
    season_id: int
    round_id: int
    race_id: int
    result_order: tuple[int, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Win5JudgementDTO:
    id: int
    win5_entry_id: int
    season_id: int
    game_account_id: int
    prediction_tier: str
    picks: tuple[int, ...]
    result_order: tuple[int, ...]
    exact_position_count: int
    on_board_wrong_position_count: int
    off_board_count: int
    season_score_delta: int
    top1_score_delta: int
    judged_at: datetime
    judged_by_discord_user_id: str

    @property
    def hit_count(self) -> int:
        return self.exact_position_count

    @property
    def score_delta(self) -> int:
        return self.season_score_delta


@dataclass(frozen=True, slots=True)
class Win5SubmissionRoundDTO:
    round: Win5RoundDTO
    submissions: tuple[Win5EntryDTO, ...]
    judgements: tuple[Win5JudgementDTO, ...]


@dataclass(frozen=True, slots=True)
class Win5ScoreDTO:
    season_id: int
    game_account_id: int
    season_score: int
    top1_score: int
    created_at: datetime
    updated_at: datetime

    @property
    def score(self) -> int:
        return self.season_score


@dataclass(frozen=True, slots=True)
class Win5ResultMutationDTO:
    action: str
    audit_id: int
    round: Win5RoundDTO
    result: Win5ResultDTO


@dataclass(frozen=True, slots=True)
class Win5ScoringResultDTO:
    action: str
    audit_id: int
    round: Win5RoundDTO
    result: Win5ResultDTO | None
    results: tuple[Win5ResultDTO, ...]
    judgements: tuple[Win5JudgementDTO, ...]
    scores: tuple[Win5ScoreDTO, ...]


@dataclass(frozen=True, slots=True)
class Win5StandingDTO:
    rank: int
    game_account_id: int
    display_name: str
    score: int


@dataclass(frozen=True, slots=True)
class BetJudgementDTO:
    id: int
    bet_id: int
    race_id: int
    game_account_id: int
    bet_type: str
    selected_entry_numbers: tuple[int, ...]
    stake_amount: int
    judgement_status: str
    is_hit: bool
    payout_amount: int
    point_delta: int
    payout_rate: Decimal | None
    judged_at: datetime | None
    judged_by_discord_user_id: str | None


@dataclass(frozen=True, slots=True)
class GameEventDTO:
    id: int
    name: str
    event_type: str
    event_scope: str
    starts_at: datetime | None
    ends_at: datetime | None
    status: str
    created_by_discord_user_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MatchEntrySnapshotDTO:
    entry_number: int
    display_name: str
    game_account_id: int | None
    character_name: str | None


@dataclass(frozen=True, slots=True)
class RaceConditionDTO:
    grade: str
    venue: str
    track_surface: str
    distance: int
    direction: str
    season: str
    weather: str
    track_condition: str
    condition_label: str
    participant_count: int


@dataclass(frozen=True, slots=True)
class MatchRaceDTO:
    id: int
    event_id: int | None
    name: str
    description: str | None
    starts_at: datetime | None
    status: str
    betting_window_version: int
    betting_opened_at: datetime | None
    betting_closed_at: datetime | None
    entry_count: int


@dataclass(frozen=True, slots=True)
class RaceOperationResultDTO:
    action: str
    audit_id: int
    race: MatchRaceDTO
    condition: RaceConditionDTO | None
    entries: tuple[MatchEntrySnapshotDTO, ...]
