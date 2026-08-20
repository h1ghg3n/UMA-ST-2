from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.domain.errors import CurrentWin5ReplayConflictError, CurrentWin5ReplayError
from umacircle_bot.services.current_win5_replay_provenance import (
    create_current_win5_replay_provenance,
    inspect_current_win5_replay_provenance,
    load_current_win5_replay_runs,
)
from umacircle_bot.services.current_win5_replay_state import (
    build_current_win5_replay_final_report,
    build_current_win5_replay_participant_checksum,
    build_current_win5_replay_request_key,
    current_win5_replay_participant_identity_errors,
    load_current_win5_replay_participant_accounts,
    lock_current_win5_replay_gate,
)
from umacircle_bot.services.win5 import (
    CreateWin5RoundCommand,
    CreateWin5SeasonCommand,
    EnterWin5ResultCommand,
    ScoreWin5RoundCommand,
    Win5RoundEntryInput,
    Win5RoundTransitionCommand,
    Win5SeasonTransitionCommand,
    activate_win5_season,
    close_win5_round,
    create_win5_round,
    create_win5_season,
    enter_win5_result,
    open_win5_round,
    score_win5_round,
    submit_win5_prediction,
)
from umacircle_bot.services.win5_first_preflight import build_win5_first_launch_preflight
from umacircle_bot.sheets.current_win5_replay_manifest import CurrentWin5ReplayManifest

_BOT_COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


@dataclass(frozen=True, slots=True)
class CurrentWin5ReplayPreview:
    mode: str
    can_apply: bool
    errors: tuple[str, ...]
    participant_checksum: str
    preflight: dict[str, object] | None
    final_report: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class CurrentWin5ReplayResult:
    mode: str
    replay_import_run_id: int
    season_id: int
    round_id: int
    participant_checksum: str
    final_report: dict[str, object]


def preview_current_win5_epoch_replay(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
    bot_commit: str,
    runtime_bot_commit: str,
) -> CurrentWin5ReplayPreview:
    """Inspect either the empty pre-replay state or one exact completed replay."""

    _require_clean_session(session)
    normalized_bot_commit = _normalize_bot_commit(bot_commit)
    normalized_runtime_commit = _normalize_bot_commit(runtime_bot_commit)
    commit_errors = () if normalized_bot_commit == normalized_runtime_commit else ("bot_commit_mismatch",)

    runs = load_current_win5_replay_runs(session, lock=False)
    participant_checksum = build_current_win5_replay_participant_checksum(manifest.participant_discord_user_ids)
    if not runs:
        preflight = build_win5_first_launch_preflight(
            session,
            manifest=manifest,
            bot_commit=normalized_bot_commit,
            runtime_bot_commit=normalized_runtime_commit,
        )
        errors = list(commit_errors)
        errors.extend(preflight["errors"])
        errors.extend(current_win5_replay_participant_identity_errors(session, manifest=manifest))
        room_points = preflight.get("room_points")
        if (
            not isinstance(room_points, dict)
            or room_points.get("wallet_total") != manifest.expected_pre_replay_point_total
        ):
            errors.append("pre_replay_point_total_mismatch")
        participant_report = preflight.get("participant_manifest")
        if (
            not isinstance(participant_report, dict)
            or participant_report.get("participant_checksum") != participant_checksum
        ):
            errors.append("participant_checksum_mismatch")
        return CurrentWin5ReplayPreview(
            mode="ready",
            can_apply=not errors,
            errors=tuple(dict.fromkeys(errors)),
            participant_checksum=participant_checksum,
            preflight=preflight,
            final_report=None,
        )

    final_report, final_errors = build_current_win5_replay_final_report(
        session,
        manifest=manifest,
    )
    provenance_errors, replay_run = inspect_current_win5_replay_provenance(
        session,
        runs=runs,
        manifest=manifest,
        participant_checksum=participant_checksum,
        final_report=final_report,
        bot_commit=normalized_bot_commit,
        lock=False,
    )
    errors = tuple(dict.fromkeys((*commit_errors, *final_errors, *provenance_errors)))
    return CurrentWin5ReplayPreview(
        mode="exact_retry" if replay_run is not None else "conflict",
        can_apply=not errors and replay_run is not None,
        errors=errors,
        participant_checksum=participant_checksum,
        preflight=None,
        final_report=final_report,
    )


def apply_current_win5_epoch_replay(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
    bot_commit: str,
    runtime_bot_commit: str,
) -> CurrentWin5ReplayResult:
    """Replay one reviewed current epoch through native services in one outer transaction."""

    _require_clean_session(session)
    normalized_bot_commit = _normalize_bot_commit(bot_commit)
    normalized_runtime_commit = _normalize_bot_commit(runtime_bot_commit)
    _ensure_outer_database_transaction(session)
    try:
        with session.begin_nested():
            lock_current_win5_replay_gate(session)
            preview = preview_current_win5_epoch_replay(
                session,
                manifest=manifest,
                bot_commit=normalized_bot_commit,
                runtime_bot_commit=normalized_runtime_commit,
            )
            if not preview.can_apply:
                raise CurrentWin5ReplayConflictError(
                    "current WIN5 epoch replay precondition failed: " + ",".join(preview.errors[:5])
                )
            season_id, round_id = execute_current_win5_replay_graph(session, manifest=manifest)
            final_report, final_errors = build_current_win5_replay_final_report(
                session,
                manifest=manifest,
            )
            if final_errors:
                raise CurrentWin5ReplayConflictError(
                    "current WIN5 epoch replay verification failed: " + ",".join(final_errors[:5])
                )

            if preview.mode == "ready":
                replay_run = create_current_win5_replay_provenance(
                    session,
                    manifest=manifest,
                    participant_checksum=preview.participant_checksum,
                    season_id=season_id,
                    round_id=round_id,
                    final_report=final_report,
                    bot_commit=normalized_bot_commit,
                )
            else:
                provenance_errors, replay_run = inspect_current_win5_replay_provenance(
                    session,
                    runs=load_current_win5_replay_runs(session, lock=True),
                    manifest=manifest,
                    participant_checksum=preview.participant_checksum,
                    final_report=final_report,
                    bot_commit=normalized_bot_commit,
                    lock=True,
                )
                if replay_run is None or provenance_errors:
                    raise CurrentWin5ReplayConflictError(
                        "current WIN5 replay provenance changed: " + ",".join(provenance_errors[:5])
                    )
            return CurrentWin5ReplayResult(
                mode="applied" if preview.mode == "ready" else "exact_retry",
                replay_import_run_id=replay_run.id,
                season_id=season_id,
                round_id=round_id,
                participant_checksum=preview.participant_checksum,
                final_report=final_report,
            )
    except IntegrityError as exc:
        session.expire_all()
        retry = preview_current_win5_epoch_replay(
            session,
            manifest=manifest,
            bot_commit=normalized_bot_commit,
            runtime_bot_commit=normalized_runtime_commit,
        )
        if retry.can_apply and retry.mode == "exact_retry" and retry.final_report is not None:
            runs = load_current_win5_replay_runs(session, lock=False)
            return CurrentWin5ReplayResult(
                mode="exact_retry",
                replay_import_run_id=runs[0].id,
                season_id=int(retry.final_report["season_id"]),
                round_id=int(retry.final_report["round_id"]),
                participant_checksum=retry.participant_checksum,
                final_report=retry.final_report,
            )
        raise exc


def execute_current_win5_replay_graph(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
) -> tuple[int, int]:
    """Replay only the reviewed native WIN5 graph inside the caller transaction."""

    key = build_current_win5_replay_request_key
    reason = "reviewed bounded current-epoch replay"
    season_result = create_win5_season(
        session,
        command=CreateWin5SeasonCommand(
            name=manifest.season.name,
            starts_at=manifest.season.starts_at,
            ends_at=manifest.season.ends_at,
            actor_discord_user_id=manifest.season.create_actor_discord_user_id,
            idempotency_key=key(manifest, "season-create"),
            reason=reason,
        ),
    )
    if season_result.season is None or season_result.season.season_number != manifest.season.season_number:
        raise CurrentWin5ReplayConflictError("current WIN5 replay Season allocation changed")
    season_id = season_result.season.id
    activate_win5_season(
        session,
        command=Win5SeasonTransitionCommand(
            season_id=season_id,
            actor_discord_user_id=manifest.season.activate_actor_discord_user_id,
            idempotency_key=key(manifest, "season-activate"),
            reason=reason,
        ),
    )
    round_result = create_win5_round(
        session,
        command=CreateWin5RoundCommand(
            season_id=season_id,
            round_number=manifest.round.round_number,
            race_name=manifest.round.race_name,
            starts_at=manifest.round.starts_at,
            entries=tuple(
                Win5RoundEntryInput(entry_number=entry.entry_number, display_name=entry.display_name)
                for entry in manifest.round.race_entries
            ),
            actor_discord_user_id=manifest.round.create_actor_discord_user_id,
            idempotency_key=key(manifest, "round-create"),
            round_label=manifest.round.round_label,
            opens_at=manifest.round.opens_at,
            closes_at=manifest.round.closes_at,
            reason=reason,
        ),
    )
    if round_result.round is None or round_result.round.round_number != manifest.round.round_number:
        raise CurrentWin5ReplayConflictError("current WIN5 replay Round allocation changed")
    round_id = round_result.round.id
    open_win5_round(
        session,
        command=Win5RoundTransitionCommand(
            round_id=round_id,
            actor_discord_user_id=manifest.round.open_actor_discord_user_id,
            idempotency_key=key(manifest, "round-open"),
            reason=reason,
        ),
    )
    accounts = load_current_win5_replay_participant_accounts(session, manifest=manifest)
    for index, submission in enumerate(manifest.round.submissions, start=1):
        submit_win5_prediction(
            session,
            game_account_id=accounts[submission.discord_user_id].id,
            season_id=season_id,
            round_id=round_id,
            prediction_tier=submission.prediction_tier,
            picks=submission.picks,
            actor_discord_user_id=submission.actor_discord_user_id,
            idempotency_key=key(manifest, f"submission-{index}"),
        )
    close_win5_round(
        session,
        command=Win5RoundTransitionCommand(
            round_id=round_id,
            actor_discord_user_id=manifest.round.close_actor_discord_user_id,
            idempotency_key=key(manifest, "round-close"),
            reason=reason,
        ),
    )
    enter_win5_result(
        session,
        command=EnterWin5ResultCommand(
            round_id=round_id,
            result_order=manifest.round.result_order,
            actor_discord_user_id=manifest.round.result_actor_discord_user_id,
            idempotency_key=key(manifest, "result-enter"),
            reason=reason,
        ),
    )
    score_win5_round(
        session,
        command=ScoreWin5RoundCommand(
            round_id=round_id,
            actor_discord_user_id=manifest.round.score_actor_discord_user_id,
            idempotency_key=key(manifest, "round-score"),
            reason=reason,
        ),
    )
    return season_id, round_id


def _normalize_bot_commit(value: object) -> str:
    if not isinstance(value, str):
        raise CurrentWin5ReplayError("bot commit must be text")
    normalized = value.strip().lower()
    if _BOT_COMMIT_PATTERN.fullmatch(normalized) is None:
        raise CurrentWin5ReplayError("bot commit must be a full 40 or 64 character hexadecimal revision")
    return normalized


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise CurrentWin5ReplayError("current WIN5 replay requires a clean session")


def _ensure_outer_database_transaction(session: Session) -> None:
    connection = session.connection()
    if connection.dialect.name != "sqlite":
        return
    driver_connection = connection.connection.driver_connection
    if not driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
