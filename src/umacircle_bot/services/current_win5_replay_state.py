from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    Persona,
    Race,
    RaceEntry,
    SheetImportRecord,
    SheetImportRun,
    Win5Entry,
    Win5Judgement,
    Win5OperationAudit,
    Win5Pick,
    Win5Result,
    Win5Round,
    Win5RoundRace,
    Win5Score,
    Win5ScoreEvent,
    Win5Season,
)
from umacircle_bot.domain.errors import CurrentWin5ReplayConflictError
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.services.current_win5_replay_points import build_current_win5_replay_point_report
from umacircle_bot.services.current_win5_replay_registration import registration_provenance_matches
from umacircle_bot.services.win5_first_baseline import (
    WIN5_FIRST_BASELINE_IMPORT_KIND,
    WIN5_FIRST_BASELINE_RECORD_TYPE,
)
from umacircle_bot.sheets.current_win5_replay_manifest import CurrentWin5ReplayManifest


def build_current_win5_replay_final_report(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
) -> tuple[dict[str, object], tuple[str, ...]]:
    graph_report, graph_errors = build_current_win5_replay_graph_report(session, manifest=manifest)
    accounts = load_current_win5_replay_participant_accounts(session, manifest=manifest, raise_on_error=False)
    participant_persona_ids = {account.persona_id for account in accounts.values() if account.persona_id is not None}
    point_report, point_errors = build_current_win5_replay_point_report(
        session,
        manifest=manifest,
        participant_persona_ids=participant_persona_ids,
    )
    report = {**graph_report, "room_points": point_report}
    errors = (
        *current_win5_replay_participant_identity_errors(session, manifest=manifest),
        *graph_errors,
        *point_errors,
    )
    return report, tuple(dict.fromkeys(errors))


def build_current_win5_replay_graph_report(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Verify the bounded native WIN5 graph without choosing an economic lane."""

    errors: list[str] = []
    accounts = load_current_win5_replay_participant_accounts(session, manifest=manifest, raise_on_error=False)
    participant_account_ids = {account.id for account in accounts.values()}

    seasons = tuple(session.scalars(select(Win5Season).order_by(Win5Season.id)))
    rounds = tuple(session.scalars(select(Win5Round).order_by(Win5Round.id)))
    races = tuple(session.scalars(select(Race).where(Race.race_kind == "win5").order_by(Race.id)))
    race_entries = (
        tuple(
            session.scalars(select(RaceEntry).where(RaceEntry.race_id == races[0].id).order_by(RaceEntry.entry_number))
        )
        if len(races) == 1
        else ()
    )
    entries = tuple(session.scalars(select(Win5Entry).order_by(Win5Entry.id)))
    results = tuple(session.scalars(select(Win5Result).order_by(Win5Result.id)))
    judgements = tuple(session.scalars(select(Win5Judgement).order_by(Win5Judgement.id)))
    scores = tuple(session.scalars(select(Win5Score).order_by(Win5Score.id)))
    score_events = tuple(session.scalars(select(Win5ScoreEvent).order_by(Win5ScoreEvent.id)))
    audits = tuple(session.scalars(select(Win5OperationAudit).order_by(Win5OperationAudit.id)))

    season = seasons[0] if len(seasons) == 1 else None
    round_ = rounds[0] if len(rounds) == 1 else None
    race = races[0] if len(races) == 1 else None
    if season is None or (
        season.season_number != manifest.season.season_number
        or season.name != manifest.season.name
        or season.status != "active"
        or _stored_datetime(season.starts_at) != manifest.season.starts_at
        or _stored_datetime(season.ends_at) != manifest.season.ends_at
    ):
        errors.append("season_graph_mismatch")
    if (
        round_ is None
        or season is None
        or (
            round_.season_id != season.id
            or round_.round_number != manifest.round.round_number
            or round_.round_label != manifest.round.round_label
            or round_.round_type != "normal"
            or round_.status != "scored"
            or round_.race_id != (race.id if race is not None else None)
            or _stored_datetime(round_.opens_at) != manifest.round.opens_at
            or _stored_datetime(round_.closes_at) != manifest.round.closes_at
        )
    ):
        errors.append("round_graph_mismatch")
    if race is None or (
        race.name != manifest.round.race_name
        or race.status != "scheduled"
        or _stored_datetime(race.starts_at) != manifest.round.starts_at
    ):
        errors.append("win5_race_mismatch")
    expected_race_entries = tuple((entry.entry_number, entry.display_name) for entry in manifest.round.race_entries)
    actual_race_entries = tuple((entry.entry_number, entry.horse_name_or_label) for entry in race_entries)
    if actual_race_entries != expected_race_entries:
        errors.append("win5_race_entry_mismatch")
    if int(session.scalar(select(func.count()).select_from(Win5RoundRace)) or 0):
        errors.append("unexpected_special_round_race")

    submission_by_discord = {submission.discord_user_id: submission for submission in manifest.round.submissions}
    entry_by_account = {entry.game_account_id: entry for entry in entries}
    if len(entries) != len(manifest.round.submissions) or set(entry_by_account) != participant_account_ids:
        errors.append("submission_population_mismatch")
    for discord_user_id, account in accounts.items():
        submission = submission_by_discord.get(discord_user_id)
        entry = entry_by_account.get(account.id)
        if (
            submission is None
            or entry is None
            or entry.status != "accepted"
            or entry.prediction_tier != submission.prediction_tier
        ):
            errors.append("submission_state_mismatch")
            continue
        picks = tuple(
            session.scalars(
                select(Win5Pick.entry_number).where(Win5Pick.win5_entry_id == entry.id).order_by(Win5Pick.pick_order)
            )
        )
        if picks != submission.picks:
            errors.append("submission_pick_mismatch")
    if len(results) != 1 or tuple(results[0].result_order) != manifest.round.result_order:
        errors.append("result_graph_mismatch")

    judgement_by_entry = {judgement.win5_entry_id: judgement for judgement in judgements}
    score_by_account = {score.game_account_id: score for score in scores}
    event_by_entry = {event.win5_entry_id: event for event in score_events}
    if len(judgements) != len(entries) or len(scores) != len(entries) or len(score_events) != len(entries):
        errors.append("score_population_mismatch")
    for discord_user_id, account in accounts.items():
        submission = submission_by_discord.get(discord_user_id)
        entry = entry_by_account.get(account.id)
        if submission is None or entry is None:
            continue
        expected = submission.expected_judgement
        judgement = judgement_by_entry.get(entry.id)
        event = event_by_entry.get(entry.id)
        score = score_by_account.get(account.id)
        if judgement is None or (
            judgement.prediction_tier,
            judgement.exact_position_count,
            judgement.on_board_wrong_position_count,
            judgement.off_board_count,
            judgement.season_score_delta,
            judgement.top1_score_delta,
        ) != (
            expected.prediction_tier,
            expected.exact_position_count,
            expected.on_board_wrong_position_count,
            expected.off_board_count,
            expected.season_score_delta,
            expected.top1_score_delta,
        ):
            errors.append("judgement_graph_mismatch")
        if event is None or (
            event.game_account_id,
            event.season_score_delta,
            event.top1_score_delta,
        ) != (account.id, expected.season_score_delta, expected.top1_score_delta):
            errors.append("score_event_graph_mismatch")
        if score is None or (score.season_score, score.top1_score) != (
            expected.season_score_delta,
            expected.top1_score_delta,
        ):
            errors.append("score_projection_mismatch")
    season_score = sum(score.season_score for score in scores)
    top1_score = sum(score.top1_score for score in scores)
    if (season_score, top1_score) != (manifest.expected_season_score, manifest.expected_top1_score):
        errors.append("score_total_mismatch")

    expected_audits = _expected_audits(
        manifest,
        season_id=season.id if season else 0,
        round_id=round_.id if round_ else 0,
    )
    actual_audits = {audit.idempotency_key: audit for audit in audits}
    if set(actual_audits) != set(expected_audits):
        errors.append("operation_audit_population_mismatch")
    for key, expected in expected_audits.items():
        audit = actual_audits.get(key)
        if audit is None or (audit.action, audit.actor_discord_user_id, audit.season_id, audit.round_id) != expected:
            errors.append("operation_audit_mismatch")

    report = {
        "season_id": season.id if season is not None else None,
        "round_id": round_.id if round_ is not None else None,
        "participant_count": len(accounts),
        "race_entry_count": len(race_entries),
        "submission_count": len(entries),
        "pick_count": int(session.scalar(select(func.count()).select_from(Win5Pick)) or 0),
        "result_count": len(results),
        "judgement_count": len(judgements),
        "score_event_count": len(score_events),
        "score_count": len(scores),
        "audit_count": len(audits),
        "season_score": season_score,
        "top1_score": top1_score,
    }
    return report, tuple(dict.fromkeys(errors))


def current_win5_replay_participant_identity_errors(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
) -> tuple[str, ...]:
    errors: list[str] = []
    persona_ids: set[str] = set()
    account_ids: set[int] = set()
    source_owners = _load_current_win5_replay_source_owners(session, manifest=manifest)
    source_persona_ids = {persona_id for owners in source_owners.values() for persona_id, _game_account_id in owners}
    for index, participant in enumerate(manifest.participants, start=1):
        discord = session.scalar(
            select(DiscordAccount).where(DiscordAccount.discord_user_id == participant.discord_user_id)
        )
        persona = session.get(Persona, discord.persona_id) if discord is not None and discord.persona_id else None
        matching_accounts = (
            tuple(
                session.scalars(
                    select(GameAccount).where(
                        GameAccount.persona_id == persona.id,
                        GameAccount.uma_pid == participant.uma_pid,
                        GameAccount.identity_status == "confirmed_identity",
                    )
                )
            )
            if persona is not None
            else ()
        )
        account = matching_accounts[0] if len(matching_accounts) == 1 else None
        wallet = (
            session.scalar(select(CirclePointAccount).where(CirclePointAccount.persona_id == persona.id))
            if persona is not None
            else None
        )
        provenance_matches = False
        if persona is not None and account is not None:
            if participant.provenance_kind == "approved_player_link" and participant.identity_source_key is not None:
                provenance_matches = source_owners.get(participant.identity_source_key) == {(persona.id, account.id)}
            elif participant.provenance_kind == "account_registration":
                provenance_matches = persona.id not in source_persona_ids and registration_provenance_matches(
                    session,
                    participant=participant,
                    persona=persona,
                    account=account,
                )
        if (
            discord is None
            or persona is None
            or persona.status != "active"
            or persona.id in persona_ids
            or account is None
            or account.id in account_ids
            or wallet is None
            or not provenance_matches
        ):
            errors.append(f"participant_identity_mismatch:{index}")
        if persona is not None:
            persona_ids.add(persona.id)
        if account is not None:
            account_ids.add(account.id)
    return tuple(errors)


def _load_current_win5_replay_source_owners(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
) -> dict[str, set[tuple[str, int]]]:
    records = tuple(
        session.scalars(
            select(SheetImportRecord)
            .join(SheetImportRun, SheetImportRun.id == SheetImportRecord.import_run_id)
            .where(
                SheetImportRun.import_kind == WIN5_FIRST_BASELINE_IMPORT_KIND,
                SheetImportRun.source_type == "xlsx",
                SheetImportRun.source_identifier == manifest.room_match_source_identifier,
                SheetImportRun.source_checksum == manifest.room_match_source_checksum,
                SheetImportRun.status == "completed",
                SheetImportRecord.record_type == WIN5_FIRST_BASELINE_RECORD_TYPE,
                SheetImportRecord.status == "applied",
            )
        )
    )
    owners: dict[str, set[tuple[str, int]]] = {}
    for record in records:
        detail = record.detail_json if isinstance(record.detail_json, dict) else {}
        source_key = detail.get("identity_source_key")
        persona_id = detail.get("persona_id")
        game_account_id = detail.get("game_account_id")
        if isinstance(source_key, str) and isinstance(persona_id, str) and isinstance(game_account_id, int):
            owners.setdefault(source_key, set()).add((persona_id, game_account_id))
    return owners


def load_current_win5_replay_participant_accounts(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
    raise_on_error: bool = True,
) -> dict[str, GameAccount]:
    accounts: dict[str, GameAccount] = {}
    for participant in manifest.participants:
        discord = session.scalar(
            select(DiscordAccount).where(DiscordAccount.discord_user_id == participant.discord_user_id)
        )
        candidates = (
            tuple(
                session.scalars(
                    select(GameAccount).where(
                        GameAccount.persona_id == discord.persona_id,
                        GameAccount.uma_pid == participant.uma_pid,
                        GameAccount.identity_status == "confirmed_identity",
                    )
                )
            )
            if discord is not None and discord.persona_id is not None
            else ()
        )
        account = candidates[0] if len(candidates) == 1 else None
        if account is None:
            if raise_on_error:
                raise CurrentWin5ReplayConflictError("current WIN5 replay participant account is missing")
            continue
        accounts[participant.discord_user_id] = account
    return accounts


def lock_current_win5_replay_gate(session: Session) -> None:
    for model in (SheetImportRun, Persona, DiscordAccount, GameAccount, CirclePointAccount, CirclePointTransaction):
        tuple(session.scalars(select(model).order_by(model.id).with_for_update()))
    tuple(session.scalars(select(Win5Season).order_by(Win5Season.id).with_for_update()))
    tuple(session.scalars(select(Race).where(Race.race_kind == "win5").order_by(Race.id).with_for_update()))


def build_current_win5_replay_request_key(manifest: CurrentWin5ReplayManifest, action: str) -> str:
    return f"current-win5-replay:{manifest.manifest_checksum}:{action}"


def build_current_win5_replay_participant_checksum(participant_ids: tuple[str, ...]) -> str:
    payload = "fresh-win5-participants-v1\n" + "\n".join(sorted(participant_ids))
    return sha256(payload.encode("utf-8")).hexdigest()


def _expected_audits(
    manifest: CurrentWin5ReplayManifest,
    *,
    season_id: int,
    round_id: int,
) -> dict[str, tuple[str, str, int, int | None]]:
    key = build_current_win5_replay_request_key
    expected = {
        key(manifest, "season-create"): (
            "win5_season_create",
            manifest.season.create_actor_discord_user_id,
            season_id,
            None,
        ),
        key(manifest, "season-activate"): (
            "win5_season_activate",
            manifest.season.activate_actor_discord_user_id,
            season_id,
            None,
        ),
        key(manifest, "round-create"): (
            "win5_round_create",
            manifest.round.create_actor_discord_user_id,
            season_id,
            round_id,
        ),
        key(manifest, "round-open"): (
            "win5_round_open",
            manifest.round.open_actor_discord_user_id,
            season_id,
            round_id,
        ),
        key(manifest, "round-close"): (
            "win5_round_close",
            manifest.round.close_actor_discord_user_id,
            season_id,
            round_id,
        ),
        key(manifest, "result-enter"): (
            "win5_result_enter",
            manifest.round.result_actor_discord_user_id,
            season_id,
            round_id,
        ),
        key(manifest, "round-score"): (
            "win5_round_score",
            manifest.round.score_actor_discord_user_id,
            season_id,
            round_id,
        ),
    }
    for index, submission in enumerate(manifest.round.submissions, start=1):
        expected[key(manifest, f"submission-{index}")] = (
            "win5_submission_accept",
            submission.actor_discord_user_id,
            season_id,
            round_id,
        )
    return expected


def _stored_datetime(value: datetime | None) -> datetime | None:
    return database_datetime_as_utc(value) if value is not None else None
