from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from umacircle_bot.domain.errors import CurrentWin5ReplayError, InvalidUmaPidError, Win5RuleError
from umacircle_bot.domain.identity import validate_registration_pid
from umacircle_bot.domain.imports import normalize_import_source_identifier, normalize_sha256_hex
from umacircle_bot.domain.win5 import (
    judge_win5_prediction,
    normalize_prediction_tier,
    normalize_win5_picks,
    normalize_win5_result_order,
    win5_circle_point_reward,
)
from umacircle_bot.runtime_preflight import EXPECTED_ROOM_POINT_SCALE

CURRENT_WIN5_REPLAY_MANIFEST_VERSION = 2
CURRENT_WIN5_REPLAY_MODE = "bounded_current_win5_epoch_rehearsal"
CURRENT_WIN5_REPLAY_SOURCE_REVISION = "20260802_0021"
CURRENT_WIN5_GRAPH_MANIFEST_VERSION = 1
CURRENT_WIN5_GRAPH_MODE = "bounded_current_win5_graph"


@dataclass(frozen=True, slots=True)
class CurrentWin5ReplayJudgement:
    prediction_tier: str
    exact_position_count: int
    on_board_wrong_position_count: int
    off_board_count: int
    season_score_delta: int
    top1_score_delta: int


@dataclass(frozen=True, slots=True)
class CurrentWin5ReplayParticipant:
    discord_user_id: str
    discord_nickname: str
    uma_pid: str
    nickname: str | None
    ingame_name: str | None
    provenance_kind: str
    identity_source_key: str | None
    registration_guild_id: str | None
    registration_approved_by_discord_user_id: str | None
    registration_target_initial_grant: int | None


@dataclass(frozen=True, slots=True)
class CurrentWin5ReplaySubmission:
    discord_user_id: str
    prediction_tier: str
    picks: tuple[int, ...]
    actor_discord_user_id: str
    expected_judgement: CurrentWin5ReplayJudgement


@dataclass(frozen=True, slots=True)
class CurrentWin5ReplaySeason:
    season_number: int
    name: str
    starts_at: datetime | None
    ends_at: datetime | None
    create_actor_discord_user_id: str
    activate_actor_discord_user_id: str


@dataclass(frozen=True, slots=True)
class CurrentWin5ReplayRaceEntry:
    entry_number: int
    display_name: str


@dataclass(frozen=True, slots=True)
class CurrentWin5ReplayRound:
    round_number: int
    round_label: str | None
    race_name: str
    starts_at: datetime
    opens_at: datetime | None
    closes_at: datetime | None
    race_entries: tuple[CurrentWin5ReplayRaceEntry, ...]
    submissions: tuple[CurrentWin5ReplaySubmission, ...]
    result_order: tuple[int, ...]
    create_actor_discord_user_id: str
    open_actor_discord_user_id: str
    close_actor_discord_user_id: str
    result_actor_discord_user_id: str
    score_actor_discord_user_id: str


@dataclass(frozen=True, slots=True)
class CurrentWin5ReplayPointTotals:
    room_match_baseline: int
    registration: int
    win5_reward: int
    current: int


@dataclass(frozen=True, slots=True)
class CurrentWin5ReplayManifest:
    manifest_checksum: str
    file_checksum: str
    room_match_source_identifier: str
    room_match_source_checksum: str
    win5_first_baseline_checksum: str
    expected_source_identity_count: int
    season: CurrentWin5ReplaySeason
    round: CurrentWin5ReplayRound
    participants: tuple[CurrentWin5ReplayParticipant, ...]
    source_point_totals: CurrentWin5ReplayPointTotals
    target_point_totals: CurrentWin5ReplayPointTotals
    expected_season_score: int
    expected_top1_score: int

    @property
    def participant_discord_user_ids(self) -> tuple[str, ...]:
        return tuple(participant.discord_user_id for participant in self.participants)

    @property
    def expected_source_participant_count(self) -> int:
        return sum(participant.provenance_kind == "approved_player_link" for participant in self.participants)

    @property
    def expected_pre_replay_point_total(self) -> int:
        return self.target_point_totals.room_match_baseline + self.target_point_totals.registration

    @property
    def expected_reward_transaction_count(self) -> int:
        return sum(
            win5_circle_point_reward(
                prediction_tier=submission.prediction_tier,
                exact_position_count=submission.expected_judgement.exact_position_count,
            )
            > 0
            for submission in self.round.submissions
        )


@dataclass(frozen=True, slots=True)
class CurrentWin5GraphManifest:
    """S5E projection of the reviewed WIN5 graph without the superseded Point envelope."""

    manifest_checksum: str
    file_checksum: str
    room_match_source_identifier: str
    room_match_source_checksum: str
    season: CurrentWin5ReplaySeason
    round: CurrentWin5ReplayRound
    participants: tuple[CurrentWin5ReplayParticipant, ...]
    expected_season_score: int
    expected_top1_score: int

    @property
    def participant_discord_user_ids(self) -> tuple[str, ...]:
        return tuple(participant.discord_user_id for participant in self.participants)


def load_current_win5_replay_manifest(path: Path) -> CurrentWin5ReplayManifest:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise CurrentWin5ReplayError("current WIN5 replay manifest is unavailable") from exc
    return parse_current_win5_replay_manifest(payload)


def load_current_win5_graph_manifest(path: Path) -> CurrentWin5GraphManifest:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise CurrentWin5ReplayError("current WIN5 graph artifact is unavailable") from exc
    return parse_current_win5_graph_manifest(payload)


def parse_current_win5_graph_manifest(payload: bytes) -> CurrentWin5GraphManifest:
    """Project the protected current-epoch artifact onto graph facts used by S5E."""

    root, _source_manifest_checksum, file_checksum = _decode_reviewed_manifest(payload)
    source_version = _integer(root.get("manifest_version"), field="manifest version")
    if source_version not in {1, CURRENT_WIN5_REPLAY_MANIFEST_VERSION}:
        raise CurrentWin5ReplayError("current WIN5 graph source version is not supported")
    if _text(root.get("mode"), field="manifest mode", maximum=64) != CURRENT_WIN5_REPLAY_MODE:
        raise CurrentWin5ReplayError("current WIN5 graph source mode is not supported")
    if _text(root.get("source_revision"), field="source revision", maximum=32) != (CURRENT_WIN5_REPLAY_SOURCE_REVISION):
        raise CurrentWin5ReplayError("current WIN5 graph source revision is not approved")
    source_scale = _positive_integer(root.get("source_point_scale"), field="source point scale")
    target_scale = _positive_integer(root.get("target_point_scale"), field="target point scale")
    if source_scale != 1 or target_scale != EXPECTED_ROOM_POINT_SCALE:
        raise CurrentWin5ReplayError("current WIN5 graph registration scale is not supported")

    source_identifier = _source_identifier(root.get("room_match_source_identifier"))
    source_checksum = _sha256_hex(root.get("room_match_source_checksum"), field="Room Match source checksum")
    participants = _parse_participants(
        root.get("participants"),
        source_scale=source_scale,
        target_scale=target_scale,
    )
    season = _parse_season(root.get("season"))
    round_ = _parse_round(root.get("rounds"))
    score_totals = _mapping(root.get("expected_score_totals"), field="expected score totals")
    expected_season_score = _nonnegative_integer(score_totals.get("season_score"), field="expected season score")
    expected_top1_score = _nonnegative_integer(score_totals.get("top1_score"), field="expected TOP1 score")
    _validate_graph_semantics(
        participants=participants,
        season=season,
        round_=round_,
        expected_season_score=expected_season_score,
        expected_top1_score=expected_top1_score,
    )
    graph_payload = {
        "manifest_version": CURRENT_WIN5_GRAPH_MANIFEST_VERSION,
        "mode": CURRENT_WIN5_GRAPH_MODE,
        "source_revision": CURRENT_WIN5_REPLAY_SOURCE_REVISION,
        "source_point_scale": source_scale,
        "target_point_scale": target_scale,
        "room_match_source_identifier": source_identifier,
        "room_match_source_checksum": source_checksum,
        "participants": root.get("participants"),
        "season": root.get("season"),
        "rounds": root.get("rounds"),
        "expected_score_totals": root.get("expected_score_totals"),
    }
    graph_checksum = sha256(
        json.dumps(graph_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return CurrentWin5GraphManifest(
        manifest_checksum=graph_checksum,
        file_checksum=file_checksum,
        room_match_source_identifier=source_identifier,
        room_match_source_checksum=source_checksum,
        season=season,
        round=round_,
        participants=participants,
        expected_season_score=expected_season_score,
        expected_top1_score=expected_top1_score,
    )


def parse_current_win5_replay_manifest(payload: bytes) -> CurrentWin5ReplayManifest:
    root, stored_checksum, file_checksum = _decode_reviewed_manifest(payload)

    if _integer(root.get("manifest_version"), field="manifest version") != CURRENT_WIN5_REPLAY_MANIFEST_VERSION:
        raise CurrentWin5ReplayError("current WIN5 replay manifest version is not supported")
    if _text(root.get("mode"), field="manifest mode", maximum=64) != CURRENT_WIN5_REPLAY_MODE:
        raise CurrentWin5ReplayError("current WIN5 replay manifest mode is not supported")
    if _text(root.get("source_revision"), field="source revision", maximum=32) != CURRENT_WIN5_REPLAY_SOURCE_REVISION:
        raise CurrentWin5ReplayError("current WIN5 replay source revision is not approved")
    source_scale = _positive_integer(root.get("source_point_scale"), field="source point scale")
    target_scale = _positive_integer(root.get("target_point_scale"), field="target point scale")
    if source_scale != 1 or target_scale != EXPECTED_ROOM_POINT_SCALE:
        raise CurrentWin5ReplayError("current WIN5 replay point scale is not supported")

    source_identifier = _source_identifier(root.get("room_match_source_identifier"))
    source_checksum = _sha256_hex(
        root.get("room_match_source_checksum"),
        field="Room Match source checksum",
    )
    baseline_checksum = _sha256_hex(
        root.get("win5_first_baseline_checksum"),
        field="WIN5-first baseline checksum",
    )
    expected_source_identity_count = _positive_integer(
        root.get("expected_source_identity_count"),
        field="expected source identity count",
    )
    participants = _parse_participants(
        root.get("participants"),
        source_scale=source_scale,
        target_scale=target_scale,
    )
    season = _parse_season(root.get("season"))
    round_ = _parse_round(root.get("rounds"))
    source_points = _parse_point_totals(root.get("source_point_totals"), field="source point totals")
    target_points = _parse_point_totals(root.get("expected_target_point_totals"), field="target point totals")
    score_totals = _mapping(root.get("expected_score_totals"), field="expected score totals")
    expected_season_score = _nonnegative_integer(score_totals.get("season_score"), field="expected season score")
    expected_top1_score = _nonnegative_integer(score_totals.get("top1_score"), field="expected TOP1 score")

    _validate_manifest_semantics(
        participants=participants,
        season=season,
        round_=round_,
        source_points=source_points,
        target_points=target_points,
        source_scale=source_scale,
        target_scale=target_scale,
        expected_source_identity_count=expected_source_identity_count,
        expected_season_score=expected_season_score,
        expected_top1_score=expected_top1_score,
    )
    return CurrentWin5ReplayManifest(
        manifest_checksum=stored_checksum,
        file_checksum=file_checksum,
        room_match_source_identifier=source_identifier,
        room_match_source_checksum=source_checksum,
        win5_first_baseline_checksum=baseline_checksum,
        expected_source_identity_count=expected_source_identity_count,
        season=season,
        round=round_,
        participants=participants,
        source_point_totals=source_points,
        target_point_totals=target_points,
        expected_season_score=expected_season_score,
        expected_top1_score=expected_top1_score,
    )


def _decode_reviewed_manifest(payload: bytes) -> tuple[dict[str, object], str, str]:
    if not isinstance(payload, bytes) or not payload:
        raise CurrentWin5ReplayError("current WIN5 manifest payload must be non-empty bytes")
    file_checksum = sha256(payload).hexdigest()
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentWin5ReplayError("current WIN5 manifest is not valid UTF-8 JSON") from exc
    root = _mapping(document, field="manifest")
    stored_checksum = _sha256_hex(root.get("manifest_checksum"), field="manifest checksum")
    checksum_payload = dict(root)
    checksum_payload.pop("manifest_checksum", None)
    calculated_checksum = sha256(
        json.dumps(checksum_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    if stored_checksum != calculated_checksum:
        raise CurrentWin5ReplayError("current WIN5 manifest checksum does not match its content")
    return root, stored_checksum, file_checksum


def _parse_participants(
    value: object,
    *,
    source_scale: int,
    target_scale: int,
) -> tuple[CurrentWin5ReplayParticipant, ...]:
    rows = _sequence(value, field="participants")
    participants: list[CurrentWin5ReplayParticipant] = []
    for index, value_row in enumerate(rows, start=1):
        row = _mapping(value_row, field=f"participant {index}")
        discord_user_id = _discord_user_id(row.get("discord_user_id"), field=f"participant {index} Discord ID")
        discord_nickname = _text(
            row.get("discord_nickname"),
            field=f"participant {index} Discord nickname",
            maximum=100,
        )
        if _text(row.get("identity_status"), field=f"participant {index} identity status", maximum=32) != (
            "confirmed_identity"
        ):
            raise CurrentWin5ReplayError(f"participant {index} identity status is not confirmed")
        pid = _text(row.get("uma_pid"), field=f"participant {index} PID", maximum=32)
        nickname = _optional_text(row.get("nickname"), field=f"participant {index} nickname", maximum=100)
        ingame_name = _optional_text(
            row.get("ingame_name"),
            field=f"participant {index} ingame name",
            maximum=100,
        )
        try:
            validate_registration_pid(pid)
        except InvalidUmaPidError as exc:
            raise CurrentWin5ReplayError(f"participant {index} PID is invalid") from exc
        provenance = _mapping(row.get("provenance"), field=f"participant {index} provenance")
        kind = _text(provenance.get("kind"), field=f"participant {index} provenance kind", maximum=32)
        identity_source_key: str | None = None
        registration_guild_id: str | None = None
        registration_approved_by_discord_user_id: str | None = None
        registration_target_initial_grant: int | None = None
        if kind == "approved_player_link":
            identity_source_key = _sha256_hex(
                provenance.get("identity_source_key"),
                field=f"participant {index} identity source key",
            )
            request = _mapping(provenance.get("request"), field=f"participant {index} player-link request")
            if request.get("requester_discord_user_id") != discord_user_id or request.get("submitted_uma_pid") != pid:
                raise CurrentWin5ReplayError(f"participant {index} player-link provenance changed")
        elif kind == "account_registration":
            registration_guild_id = _discord_user_id(
                provenance.get("guild_id"),
                field=f"participant {index} registration guild ID",
            )
            registration_approved_by_discord_user_id = _discord_user_id(
                provenance.get("source_created_by_discord_user_id"),
                field=f"participant {index} registration approver",
            )
            source_initial_grant = _nonnegative_integer(
                provenance.get("source_initial_grant"),
                field=f"participant {index} source initial grant",
            )
            registration_target_initial_grant = source_initial_grant * target_scale // source_scale
        else:
            raise CurrentWin5ReplayError(f"participant {index} provenance kind is not approved")
        participants.append(
            CurrentWin5ReplayParticipant(
                discord_user_id=discord_user_id,
                discord_nickname=discord_nickname,
                uma_pid=pid,
                nickname=nickname,
                ingame_name=ingame_name,
                provenance_kind=kind,
                identity_source_key=identity_source_key,
                registration_guild_id=registration_guild_id,
                registration_approved_by_discord_user_id=registration_approved_by_discord_user_id,
                registration_target_initial_grant=registration_target_initial_grant,
            )
        )
    return tuple(participants)


def _parse_season(value: object) -> CurrentWin5ReplaySeason:
    season = _mapping(value, field="season")
    if _text(season.get("status"), field="season status", maximum=32) != "active":
        raise CurrentWin5ReplayError("current WIN5 replay Season must be active")
    _positive_integer(season.get("id"), field="source Season ID")
    return CurrentWin5ReplaySeason(
        season_number=_positive_integer(season.get("season_number"), field="Season number"),
        name=_text(season.get("name"), field="Season name", maximum=100),
        starts_at=_optional_source_datetime(season.get("starts_at"), field="Season starts_at"),
        ends_at=_optional_source_datetime(season.get("ends_at"), field="Season ends_at"),
        create_actor_discord_user_id=_audit_actor(season.get("create_audit"), field="Season create audit"),
        activate_actor_discord_user_id=_audit_actor(season.get("activate_audit"), field="Season activate audit"),
    )


def _parse_round(value: object) -> CurrentWin5ReplayRound:
    rounds = _sequence(value, field="rounds")
    if len(rounds) != 1:
        raise CurrentWin5ReplayError("current WIN5 replay requires exactly one Round")
    row = _mapping(rounds[0], field="Round")
    if _text(row.get("round_type"), field="Round type", maximum=32) != "normal":
        raise CurrentWin5ReplayError("current WIN5 replay supports only one normal Round")
    if _text(row.get("status"), field="Round status", maximum=32) != "scored":
        raise CurrentWin5ReplayError("current WIN5 replay Round must be scored")
    _positive_integer(row.get("id"), field="source Round ID")
    _positive_integer(row.get("season_id"), field="source Round Season ID")
    _positive_integer(row.get("race_id"), field="source Race ID")

    entries = tuple(
        CurrentWin5ReplayRaceEntry(
            entry_number=_positive_integer(
                _mapping(item, field=f"race entry {index}").get("entry_number"),
                field=f"race entry {index} number",
            ),
            display_name=_text(
                _mapping(item, field=f"race entry {index}").get("display_name"),
                field=f"race entry {index} display name",
                maximum=200,
            ),
        )
        for index, item in enumerate(_sequence(row.get("race_entries"), field="race entries"), start=1)
    )
    submissions = tuple(
        _parse_submission(item, index=index)
        for index, item in enumerate(_sequence(row.get("submissions"), field="submissions"), start=1)
    )
    try:
        result_order = normalize_win5_result_order(_sequence(row.get("result_order"), field="result order"))
    except Win5RuleError as exc:
        raise CurrentWin5ReplayError(str(exc)) from exc
    return CurrentWin5ReplayRound(
        round_number=_positive_integer(row.get("round_number"), field="Round number"),
        round_label=_optional_text(row.get("round_label"), field="Round label", maximum=64),
        race_name=_text(row.get("race_name"), field="Race name", maximum=200),
        starts_at=_source_datetime(row.get("starts_at"), field="Round starts_at"),
        opens_at=_optional_source_datetime(row.get("opens_at"), field="Round opens_at"),
        closes_at=_optional_source_datetime(row.get("closes_at"), field="Round closes_at"),
        race_entries=entries,
        submissions=submissions,
        result_order=result_order,
        create_actor_discord_user_id=_audit_actor(row.get("create_audit"), field="Round create audit"),
        open_actor_discord_user_id=_audit_actor(row.get("open_audit"), field="Round open audit"),
        close_actor_discord_user_id=_audit_actor(row.get("close_audit"), field="Round close audit"),
        result_actor_discord_user_id=_audit_actor(row.get("result_audit"), field="Round result audit"),
        score_actor_discord_user_id=_audit_actor(row.get("score_audit"), field="Round score audit"),
    )


def _parse_submission(value: object, *, index: int) -> CurrentWin5ReplaySubmission:
    row = _mapping(value, field=f"submission {index}")
    discord_user_id = _discord_user_id(row.get("discord_user_id"), field=f"submission {index} Discord ID")
    try:
        tier = normalize_prediction_tier(row.get("prediction_tier"))
        picks = normalize_win5_picks(
            _sequence(row.get("picks"), field=f"submission {index} picks"), prediction_tier=tier
        )
    except Win5RuleError as exc:
        raise CurrentWin5ReplayError(str(exc)) from exc
    expected = _mapping(row.get("expected_judgement"), field=f"submission {index} judgement")
    judgement = CurrentWin5ReplayJudgement(
        prediction_tier=_text(expected.get("prediction_tier"), field="judgement tier", maximum=32),
        exact_position_count=_nonnegative_integer(expected.get("exact_position_count"), field="exact count"),
        on_board_wrong_position_count=_nonnegative_integer(
            expected.get("on_board_wrong_position_count"), field="on-board count"
        ),
        off_board_count=_nonnegative_integer(expected.get("off_board_count"), field="off-board count"),
        season_score_delta=_nonnegative_integer(expected.get("season_score_delta"), field="Season score delta"),
        top1_score_delta=_nonnegative_integer(expected.get("top1_score_delta"), field="TOP1 score delta"),
    )
    return CurrentWin5ReplaySubmission(
        discord_user_id=discord_user_id,
        prediction_tier=tier.value,
        picks=picks,
        actor_discord_user_id=_audit_actor(row.get("submission_audit"), field=f"submission {index} audit"),
        expected_judgement=judgement,
    )


def _parse_point_totals(value: object, *, field: str) -> CurrentWin5ReplayPointTotals:
    totals = _mapping(value, field=field)
    return CurrentWin5ReplayPointTotals(
        room_match_baseline=_nonnegative_integer(totals.get("room_match_baseline"), field=f"{field} baseline"),
        registration=_nonnegative_integer(totals.get("registration"), field=f"{field} registration"),
        win5_reward=_nonnegative_integer(totals.get("win5_reward"), field=f"{field} WIN5 reward"),
        current=_nonnegative_integer(totals.get("current"), field=f"{field} current"),
    )


def _validate_manifest_semantics(
    *,
    participants: tuple[CurrentWin5ReplayParticipant, ...],
    season: CurrentWin5ReplaySeason,
    round_: CurrentWin5ReplayRound,
    source_points: CurrentWin5ReplayPointTotals,
    target_points: CurrentWin5ReplayPointTotals,
    source_scale: int,
    target_scale: int,
    expected_source_identity_count: int,
    expected_season_score: int,
    expected_top1_score: int,
) -> None:
    calculated_reward = _validate_graph_semantics(
        participants=participants,
        season=season,
        round_=round_,
        expected_season_score=expected_season_score,
        expected_top1_score=expected_top1_score,
    )
    source_participant_count = sum(
        participant.provenance_kind == "approved_player_link" for participant in participants
    )
    if expected_source_identity_count < source_participant_count:
        raise CurrentWin5ReplayError(
            "current WIN5 replay source identity population is smaller than its source participants"
        )
    registration_grant_total = sum(
        participant.registration_target_initial_grant or 0
        for participant in participants
        if participant.provenance_kind == "account_registration"
    )
    if registration_grant_total != target_points.registration:
        raise CurrentWin5ReplayError("current WIN5 replay registration provenance does not match Point totals")
    if calculated_reward != target_points.win5_reward:
        raise CurrentWin5ReplayError("current WIN5 replay reward total does not match current rules")

    source_values = (
        source_points.room_match_baseline,
        source_points.registration,
        source_points.win5_reward,
        source_points.current,
    )
    target_values = (
        target_points.room_match_baseline,
        target_points.registration,
        target_points.win5_reward,
        target_points.current,
    )
    multiplier = target_scale // source_scale
    if target_scale % source_scale or target_values != tuple(value * multiplier for value in source_values):
        raise CurrentWin5ReplayError("current WIN5 replay target Point totals are not the exact scale conversion")
    if source_points.current != sum(source_values[:3]) or target_points.current != sum(target_values[:3]):
        raise CurrentWin5ReplayError("current WIN5 replay Point totals do not reconcile")


def _validate_graph_semantics(
    *,
    participants: tuple[CurrentWin5ReplayParticipant, ...],
    season: CurrentWin5ReplaySeason,
    round_: CurrentWin5ReplayRound,
    expected_season_score: int,
    expected_top1_score: int,
) -> int:
    if season.season_number != 1 or round_.round_number != 1:
        raise CurrentWin5ReplayError("current WIN5 replay requires first Season and first Round")
    if len(participants) != 3 or len(round_.submissions) != 3:
        raise CurrentWin5ReplayError("current WIN5 replay requires exactly three participants and submissions")
    participant_ids = tuple(participant.discord_user_id for participant in participants)
    submission_ids = tuple(submission.discord_user_id for submission in round_.submissions)
    if len(set(participant_ids)) != len(participant_ids) or set(submission_ids) != set(participant_ids):
        raise CurrentWin5ReplayError("current WIN5 replay participant and submission sets do not match")
    if len({participant.uma_pid for participant in participants}) != len(participants):
        raise CurrentWin5ReplayError("current WIN5 replay participant PIDs are not unique")
    if sum(participant.provenance_kind == "approved_player_link" for participant in participants) != 2:
        raise CurrentWin5ReplayError("current WIN5 replay requires two approved player-link participants")
    if sum(participant.provenance_kind == "account_registration" for participant in participants) != 1:
        raise CurrentWin5ReplayError("current WIN5 replay requires one account-registration participant")
    if any(submission.actor_discord_user_id != submission.discord_user_id for submission in round_.submissions):
        raise CurrentWin5ReplayError("WIN5 submission actor must be the participant")

    entry_numbers = tuple(entry.entry_number for entry in round_.race_entries)
    if len(entry_numbers) < 5 or len(set(entry_numbers)) != len(entry_numbers):
        raise CurrentWin5ReplayError("current WIN5 replay Race entries are incomplete or duplicated")
    if not set(round_.result_order).issubset(entry_numbers):
        raise CurrentWin5ReplayError("current WIN5 replay result references an unknown Race entry")

    calculated_season_score = 0
    calculated_top1_score = 0
    calculated_reward = 0
    for index, submission in enumerate(round_.submissions, start=1):
        if not set(submission.picks).issubset(entry_numbers):
            raise CurrentWin5ReplayError(f"submission {index} references an unknown Race entry")
        judgement = judge_win5_prediction(
            submission.picks,
            round_.result_order,
            prediction_tier=submission.prediction_tier,
        )
        expected = submission.expected_judgement
        if (
            expected.prediction_tier,
            expected.exact_position_count,
            expected.on_board_wrong_position_count,
            expected.off_board_count,
            expected.season_score_delta,
            expected.top1_score_delta,
        ) != (
            judgement.prediction_tier.value,
            judgement.exact_position_count,
            judgement.on_board_wrong_position_count,
            judgement.off_board_count,
            judgement.season_score_delta,
            judgement.top1_score_delta,
        ):
            raise CurrentWin5ReplayError(f"submission {index} judgement does not match current WIN5 rules")
        calculated_season_score += judgement.season_score_delta
        calculated_top1_score += judgement.top1_score_delta
        calculated_reward += win5_circle_point_reward(
            prediction_tier=judgement.prediction_tier,
            exact_position_count=judgement.exact_position_count,
        )
    if (calculated_season_score, calculated_top1_score) != (expected_season_score, expected_top1_score):
        raise CurrentWin5ReplayError("current WIN5 replay score totals do not match current rules")
    return calculated_reward


def _audit_actor(value: object, *, field: str) -> str:
    audit = _mapping(value, field=field)
    _source_datetime(audit.get("created_at"), field=f"{field} created_at")
    return _discord_user_id(audit.get("actor_discord_user_id"), field=f"{field} actor")


def _source_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CurrentWin5ReplayError(f"{field} must be a source datetime string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CurrentWin5ReplayError(f"{field} is not a valid datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_source_datetime(value: object, *, field: str) -> datetime | None:
    return None if value is None else _source_datetime(value, field=field)


def _mapping(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CurrentWin5ReplayError(f"{field} must be an object")
    return value


def _source_identifier(value: object) -> str:
    try:
        return normalize_import_source_identifier(value)
    except ValueError as exc:
        raise CurrentWin5ReplayError("Room Match source identifier is invalid") from exc


def _sha256_hex(value: object, *, field: str) -> str:
    try:
        return normalize_sha256_hex(value, field_name=field)
    except ValueError as exc:
        raise CurrentWin5ReplayError(f"{field} is invalid") from exc


def _sequence(value: object, *, field: str) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise CurrentWin5ReplayError(f"{field} must be a list")
    return tuple(value)


def _text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise CurrentWin5ReplayError(f"{field} must be non-empty text no longer than {maximum}")
    return value


def _optional_text(value: object, *, field: str, maximum: int) -> str | None:
    return None if value is None else _text(value, field=field, maximum=maximum)


def _discord_user_id(value: object, *, field: str) -> str:
    normalized = _text(value, field=field, maximum=32)
    if not normalized.isascii() or not normalized.isdigit():
        raise CurrentWin5ReplayError(f"{field} must contain only ASCII digits")
    return normalized


def _integer(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CurrentWin5ReplayError(f"{field} must be an integer")
    return value


def _positive_integer(value: object, *, field: str) -> int:
    normalized = _integer(value, field=field)
    if normalized <= 0:
        raise CurrentWin5ReplayError(f"{field} must be positive")
    return normalized


def _nonnegative_integer(value: object, *, field: str) -> int:
    normalized = _integer(value, field=field)
    if normalized < 0:
        raise CurrentWin5ReplayError(f"{field} must be nonnegative")
    return normalized
