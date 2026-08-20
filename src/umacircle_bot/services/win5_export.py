from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    CirclePointTransaction,
    GameAccount,
    Race,
    RaceEntry,
    SheetExportRun,
    Win5Entry,
    Win5Judgement,
    Win5Pick,
    Win5Result,
    Win5Round,
    Win5RoundRace,
    Win5Score,
    Win5Season,
)
from umacircle_bot.domain.errors import Win5LifecycleError, Win5MutationConflictError
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.domain.win5 import Win5RoundType, Win5SeasonStatus, Win5SubmissionStatus
from umacircle_bot.services.application import run_application_command
from umacircle_bot.services.win5_export_types import (
    Win5RoundSnapshot,
    Win5SeasonExportResult,
    Win5SeasonSnapshot,
    Win5StandingSnapshot,
    Win5SubmissionSnapshot,
)
from umacircle_bot.sheets.win5_season_workbook import write_win5_season_workbook

WIN5_EXPORT_TYPE = "win5_season_snapshot_xlsx"
OUTPUT_PATH_RESERVATION_ATTEMPTS = 10
EXPORTABLE_SEASON_STATUSES = frozenset((Win5SeasonStatus.ACTIVE.value, Win5SeasonStatus.CLOSED.value))


def execute_win5_season_export(
    *,
    season_id: int,
    output_dir: Path,
    generated_at: datetime | None = None,
) -> Win5SeasonExportResult:
    """Run a season export with application-owned commit and artifact cleanup."""

    result: Win5SeasonExportResult | None = None

    def operation(session: Session) -> Win5SeasonExportResult:
        nonlocal result
        result = export_win5_season_snapshot(
            session,
            season_id=season_id,
            output_dir=output_dir,
            generated_at=generated_at,
        )
        return result

    try:
        return run_application_command(operation)
    except Exception:
        if result is not None:
            remove_win5_export_artifact(result)
        raise


def export_win5_season_snapshot(
    session: Session,
    *,
    season_id: int,
    output_dir: Path,
    generated_at: datetime | None = None,
) -> Win5SeasonExportResult:
    normalized_season_id = _positive_id(season_id, field="season ID")
    timestamp = _aware_datetime(generated_at or datetime.now(UTC), field="generated_at")
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise ValueError("export output path must be a directory")

    season = session.get(Win5Season, normalized_season_id)
    if season is None:
        raise Win5LifecycleError("WIN5 season not found")
    if season.status not in EXPORTABLE_SEASON_STATUSES:
        raise Win5LifecycleError("WIN5 export requires an active or closed season")

    snapshot = _build_snapshot(session, season=season, generated_at=timestamp)
    output_path = _reserve_output_path(
        output_dir,
        season_number=season.season_number,
        timestamp=timestamp,
    )
    try:
        write_win5_season_workbook(output_path, snapshot=snapshot)
        checksum = _file_checksum(output_path)
        export_run = SheetExportRun(
            target_spreadsheet_id=output_path.name,
            finished_at=timestamp,
            status="completed",
            summary_json={
                "export_type": WIN5_EXPORT_TYPE,
                "season_id": snapshot.season_id,
                "season_number": snapshot.season_number,
                "round_count": snapshot.round_count,
                "submission_count": len(snapshot.submissions),
                "participant_count": snapshot.participant_count,
                "hall_of_fame_count": len(snapshot.hall_of_fame),
                "sha256_checksum": checksum,
            },
        )
        session.add(export_run)
        try:
            session.flush()
        except Exception:
            if export_run in session:
                session.expunge(export_run)
            raise
        return Win5SeasonExportResult(
            export_run_id=export_run.id,
            output_path=output_path,
            season_id=snapshot.season_id,
            season_number=snapshot.season_number,
            season_name=snapshot.season_name,
            round_count=snapshot.round_count,
            submission_count=len(snapshot.submissions),
            participant_count=snapshot.participant_count,
            hall_of_fame_count=len(snapshot.hall_of_fame),
            sha256_checksum=checksum,
        )
    except Exception:
        output_path.unlink(missing_ok=True)
        raise


def remove_win5_export_artifact(result: Win5SeasonExportResult) -> None:
    result.output_path.unlink(missing_ok=True)


def _build_snapshot(session: Session, *, season: Win5Season, generated_at: datetime) -> Win5SeasonSnapshot:
    rounds = tuple(
        session.scalars(
            select(Win5Round).where(Win5Round.season_id == season.id).order_by(Win5Round.round_number, Win5Round.id)
        )
    )
    round_by_id = {round_.id: round_ for round_ in rounds}
    round_ids = tuple(round_by_id)

    memberships = (
        tuple(
            session.scalars(
                select(Win5RoundRace)
                .where(Win5RoundRace.round_id.in_(round_ids))
                .order_by(Win5RoundRace.round_id, Win5RoundRace.display_order, Win5RoundRace.id)
            )
        )
        if round_ids
        else ()
    )
    membership_by_id = {membership.id: membership for membership in memberships}
    memberships_by_round: dict[int, list[Win5RoundRace]] = defaultdict(list)
    for membership in memberships:
        memberships_by_round[membership.round_id].append(membership)

    race_ids = {round_.race_id for round_ in rounds if round_.race_id is not None} | {
        membership.race_id for membership in memberships
    }
    races = tuple(session.scalars(select(Race).where(Race.id.in_(race_ids)).order_by(Race.id))) if race_ids else ()
    race_by_id = {race.id: race for race in races}
    entry_labels_by_race: dict[int, dict[int, str]] = defaultdict(dict)
    if race_ids:
        race_entries = session.scalars(
            select(RaceEntry)
            .where(RaceEntry.race_id.in_(race_ids))
            .order_by(RaceEntry.race_id, RaceEntry.entry_number, RaceEntry.id)
        )
        for race_entry in race_entries:
            entry_labels_by_race[race_entry.race_id][race_entry.entry_number] = (
                race_entry.horse_name_or_label or f"#{race_entry.entry_number}"
            )

    results = tuple(
        session.scalars(
            select(Win5Result)
            .where(Win5Result.season_id == season.id)
            .order_by(Win5Result.round_id, Win5Result.race_id, Win5Result.id)
        )
    )
    result_by_race_id: dict[int, Win5Result] = {}
    for result in results:
        if result.race_id in result_by_race_id:
            raise Win5MutationConflictError("WIN5 export found duplicate authoritative results")
        result_by_race_id[result.race_id] = result

    entries = tuple(
        session.scalars(
            select(Win5Entry)
            .where(Win5Entry.season_id == season.id)
            .order_by(Win5Entry.round_id, Win5Entry.special_round_race_id, Win5Entry.game_account_id, Win5Entry.id)
        )
    )
    entry_by_id = {entry.id: entry for entry in entries}
    entry_ids = tuple(entry_by_id)
    picks_by_entry: dict[int, list[int]] = defaultdict(list)
    if entry_ids:
        picks = session.execute(
            select(Win5Pick.win5_entry_id, Win5Pick.entry_number)
            .where(Win5Pick.win5_entry_id.in_(entry_ids))
            .order_by(Win5Pick.win5_entry_id, Win5Pick.pick_order, Win5Pick.id)
        )
        for pick in picks:
            picks_by_entry[pick.win5_entry_id].append(pick.entry_number)

    judgements = (
        tuple(session.scalars(select(Win5Judgement).where(Win5Judgement.win5_entry_id.in_(entry_ids))))
        if entry_ids
        else ()
    )
    judgement_by_entry = {judgement.win5_entry_id: judgement for judgement in judgements}
    if len(judgement_by_entry) != len(judgements):
        raise Win5MutationConflictError("WIN5 export found duplicate judgements")

    account_ids = {entry.game_account_id for entry in entries}
    scores = tuple(
        session.scalars(select(Win5Score).where(Win5Score.season_id == season.id).order_by(Win5Score.game_account_id))
    )
    account_ids.update(score.game_account_id for score in scores)
    accounts = (
        tuple(session.scalars(select(GameAccount).where(GameAccount.id.in_(account_ids)).order_by(GameAccount.id)))
        if account_ids
        else ()
    )
    account_name_by_id = {account.id: _account_display_name(account) for account in accounts}
    missing_accounts = account_ids - set(account_name_by_id)
    if missing_accounts:
        raise Win5MutationConflictError("WIN5 export found missing participant accounts")

    reward_by_entry = _load_persisted_rewards(session, entry_ids=entry_ids)
    submission_rows = tuple(
        _submission_row(
            entry=entry,
            round_by_id=round_by_id,
            membership_by_id=membership_by_id,
            race_by_id=race_by_id,
            entry_labels_by_race=entry_labels_by_race,
            result_by_race_id=result_by_race_id,
            picks=tuple(picks_by_entry[entry.id]),
            judgement=judgement_by_entry.get(entry.id),
            participant_name=account_name_by_id[entry.game_account_id],
            circle_point_reward=reward_by_entry.get(entry.id, 0),
        )
        for entry in entries
    )
    round_rows = _round_rows(
        rounds=rounds,
        memberships_by_round=memberships_by_round,
        race_by_id=race_by_id,
        entry_labels_by_race=entry_labels_by_race,
        result_by_race_id=result_by_race_id,
        entries=entries,
    )
    standings = _standing_rows(
        scores=scores,
        entries=entries,
        judgements=judgements,
        account_name_by_id=account_name_by_id,
    )
    hall_of_fame = tuple(
        sorted(
            (
                row
                for row in submission_rows
                if row.status == Win5SubmissionStatus.ACCEPTED.value
                and row.prediction_tier == "top5"
                and row.exact_position_count == 5
            ),
            key=lambda row: (row.judged_at or row.submitted_at, row.entry_id),
        )
    )
    return Win5SeasonSnapshot(
        season_id=season.id,
        season_number=season.season_number,
        season_name=season.name,
        season_status=season.status,
        starts_at=_stored_optional_datetime(season.starts_at),
        ends_at=_stored_optional_datetime(season.ends_at),
        generated_at=generated_at,
        round_count=len(rounds),
        scored_round_count=sum(1 for round_ in rounds if round_.status == "scored"),
        participant_count=len({entry.game_account_id for entry in entries}),
        standings=standings,
        rounds=round_rows,
        submissions=submission_rows,
        hall_of_fame=hall_of_fame,
    )


def _load_persisted_rewards(session: Session, *, entry_ids: tuple[int, ...]) -> dict[int, int]:
    if not entry_ids:
        return {}
    key_to_entry_id = {f"win5_reward:entry:{entry_id}": entry_id for entry_id in entry_ids}
    rows = session.execute(
        select(CirclePointTransaction.idempotency_key, CirclePointTransaction.amount).where(
            CirclePointTransaction.type == "win5_reward",
            CirclePointTransaction.source == "win5_score",
            CirclePointTransaction.idempotency_key.in_(tuple(key_to_entry_id)),
        )
    )
    rewards: dict[int, int] = {}
    for row in rows:
        entry_id = key_to_entry_id.get(row.idempotency_key)
        if entry_id is None or entry_id in rewards:
            raise Win5MutationConflictError("WIN5 export reward provenance is inconsistent")
        rewards[entry_id] = row.amount
    return rewards


def _submission_row(
    *,
    entry: Win5Entry,
    round_by_id: dict[int, Win5Round],
    membership_by_id: dict[int, Win5RoundRace],
    race_by_id: dict[int, Race],
    entry_labels_by_race: dict[int, dict[int, str]],
    result_by_race_id: dict[int, Win5Result],
    picks: tuple[int, ...],
    judgement: Win5Judgement | None,
    participant_name: str,
    circle_point_reward: int,
) -> Win5SubmissionSnapshot:
    round_ = round_by_id.get(entry.round_id)
    if round_ is None:
        raise Win5MutationConflictError("WIN5 export submission has no Round")
    if entry.special_round_race_id is None:
        race_id = round_.race_id
    else:
        membership = membership_by_id.get(entry.special_round_race_id)
        if membership is None or membership.round_id != round_.id:
            raise Win5MutationConflictError("WIN5 export special submission membership changed")
        race_id = membership.race_id
    if race_id is None:
        raise Win5MutationConflictError("WIN5 export submission has no Race")
    race = race_by_id.get(race_id)
    if race is None:
        raise Win5MutationConflictError("WIN5 export submission Race is missing")
    result = result_by_race_id.get(race_id)
    result_numbers = tuple(result.result_order) if result is not None else ()
    labels = entry_labels_by_race.get(race_id, {})
    return Win5SubmissionSnapshot(
        entry_id=entry.id,
        round_number=round_.round_number,
        round_label=round_.round_label,
        round_type=round_.round_type,
        race_name=race.name,
        participant_name=participant_name,
        prediction_tier=entry.prediction_tier,
        status=entry.status,
        pick_numbers=picks,
        pick_labels=tuple(_entry_display(number, labels=labels) for number in picks),
        result_numbers=result_numbers,
        result_labels=tuple(_entry_display(number, labels=labels) for number in result_numbers),
        exact_position_count=judgement.exact_position_count if judgement is not None else None,
        on_board_wrong_position_count=(judgement.on_board_wrong_position_count if judgement is not None else None),
        off_board_count=judgement.off_board_count if judgement is not None else None,
        season_score_delta=judgement.season_score_delta if judgement is not None else None,
        top1_score_delta=judgement.top1_score_delta if judgement is not None else None,
        circle_point_reward=circle_point_reward,
        submitted_at=database_datetime_as_utc(entry.created_at),
        judged_at=(
            database_datetime_as_utc(judgement.judged_at)
            if judgement is not None and judgement.judged_at is not None
            else None
        ),
    )


def _round_rows(
    *,
    rounds: tuple[Win5Round, ...],
    memberships_by_round: dict[int, list[Win5RoundRace]],
    race_by_id: dict[int, Race],
    entry_labels_by_race: dict[int, dict[int, str]],
    result_by_race_id: dict[int, Win5Result],
    entries: tuple[Win5Entry, ...],
) -> tuple[Win5RoundSnapshot, ...]:
    counts: dict[tuple[int, int | None, str], int] = defaultdict(int)
    for entry in entries:
        counts[(entry.round_id, entry.special_round_race_id, entry.status)] += 1

    rows: list[Win5RoundSnapshot] = []
    for round_ in rounds:
        if round_.round_type == Win5RoundType.NORMAL.value:
            race_targets = ((round_.race_id, None, None),)
        elif round_.round_type == Win5RoundType.SPECIAL.value:
            race_targets = tuple(
                (membership.race_id, membership.id, membership.display_order)
                for membership in memberships_by_round.get(round_.id, [])
            )
            if not race_targets:
                raise Win5MutationConflictError("WIN5 export special Round has no Race membership")
        else:
            raise Win5MutationConflictError("WIN5 export Round type is invalid")
        for race_id, membership_id, display_order in race_targets:
            race = race_by_id.get(race_id) if race_id is not None else None
            if race_id is not None and race is None:
                raise Win5MutationConflictError("WIN5 export Round Race is missing")
            result = result_by_race_id.get(race_id) if race_id is not None else None
            result_numbers = tuple(result.result_order) if result is not None else ()
            labels = entry_labels_by_race.get(race_id, {}) if race_id is not None else {}
            rows.append(
                Win5RoundSnapshot(
                    round_id=round_.id,
                    round_number=round_.round_number,
                    round_label=round_.round_label,
                    round_type=round_.round_type,
                    round_status=round_.status,
                    race_id=race_id,
                    race_display_order=display_order,
                    race_name=race.name if race is not None else None,
                    race_starts_at=_stored_optional_datetime(race.starts_at) if race is not None else None,
                    result_numbers=result_numbers,
                    result_labels=tuple(_entry_display(number, labels=labels) for number in result_numbers),
                    accepted_submission_count=counts[(round_.id, membership_id, Win5SubmissionStatus.ACCEPTED.value)],
                    cancelled_submission_count=counts[(round_.id, membership_id, Win5SubmissionStatus.CANCELLED.value)],
                    opens_at=_stored_optional_datetime(round_.opens_at),
                    closes_at=_stored_optional_datetime(round_.closes_at),
                    created_at=database_datetime_as_utc(round_.created_at),
                    updated_at=database_datetime_as_utc(round_.updated_at),
                )
            )
    return tuple(rows)


def _standing_rows(
    *,
    scores: tuple[Win5Score, ...],
    entries: tuple[Win5Entry, ...],
    judgements: tuple[Win5Judgement, ...],
    account_name_by_id: dict[int, str],
) -> tuple[Win5StandingSnapshot, ...]:
    accepted_rounds: dict[int, set[int]] = defaultdict(set)
    entry_account_by_id = {entry.id: entry.game_account_id for entry in entries}
    for entry in entries:
        if entry.status == Win5SubmissionStatus.ACCEPTED.value:
            accepted_rounds[entry.game_account_id].add(entry.round_id)

    perfect_top5_count: dict[int, int] = defaultdict(int)
    latest_judged_at: dict[int, datetime] = {}
    for judgement in judgements:
        account_id = entry_account_by_id.get(judgement.win5_entry_id)
        if account_id is None:
            raise Win5MutationConflictError("WIN5 export judgement entry is missing")
        if judgement.prediction_tier == "top5" and judgement.exact_position_count == 5:
            perfect_top5_count[account_id] += 1
        if judgement.judged_at is not None:
            judged_at = database_datetime_as_utc(judgement.judged_at)
            latest_judged_at[account_id] = max(latest_judged_at.get(account_id, judged_at), judged_at)

    season_ranks = _competition_ranks({score.game_account_id: score.season_score for score in scores})
    top1_ranks = _competition_ranks({score.game_account_id: score.top1_score for score in scores})
    ordered_scores = sorted(scores, key=lambda score: (-score.season_score, score.game_account_id))
    return tuple(
        Win5StandingSnapshot(
            season_rank=season_ranks[score.game_account_id],
            top1_rank=top1_ranks[score.game_account_id],
            game_account_id=score.game_account_id,
            participant_name=account_name_by_id[score.game_account_id],
            season_score=score.season_score,
            top1_score=score.top1_score,
            participated_round_count=len(accepted_rounds[score.game_account_id]),
            perfect_top5_count=perfect_top5_count[score.game_account_id],
            latest_judged_at=latest_judged_at.get(score.game_account_id),
        )
        for score in ordered_scores
    )


def _competition_ranks(values: dict[int, int]) -> dict[int, int]:
    ranks: dict[int, int] = {}
    previous_value: int | None = None
    previous_rank = 0
    for position, (account_id, value) in enumerate(
        sorted(values.items(), key=lambda item: (-item[1], item[0])), start=1
    ):
        if value != previous_value:
            previous_rank = position
            previous_value = value
        ranks[account_id] = previous_rank
    return ranks


def _reserve_output_path(output_dir: Path, *, season_number: int, timestamp: datetime) -> Path:
    stem = f"win5-season-{season_number}-snapshot-{timestamp.astimezone(UTC):%Y%m%dT%H%M%SZ}"
    for _attempt in range(OUTPUT_PATH_RESERVATION_ATTEMPTS):
        candidate = output_dir / f"{stem}-{uuid4().hex}.xlsx"
        try:
            candidate.touch(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("could not reserve a unique WIN5 export path")


def _account_display_name(account: GameAccount) -> str:
    return account.ingame_name or account.nickname or "표시명 없음"


def _entry_display(number: int, *, labels: dict[int, str]) -> str:
    label = labels.get(number)
    return f"{number}. {label}" if label else str(number)


def _stored_optional_datetime(value: datetime | None) -> datetime | None:
    return database_datetime_as_utc(value) if value is not None else None


def _positive_id(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _aware_datetime(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include timezone information")
    return value


def _file_checksum(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
