from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.domain.errors import LegacyImportConflictError, LegacyImportError
from umacircle_bot.domain.win5 import win5_circle_point_reward
from umacircle_bot.runtime_preflight import EXPECTED_ALEMBIC_HEAD
from umacircle_bot.services._s5e_rebuild_point import inspect_point_phases
from umacircle_bot.services._s5e_rebuild_provenance import (
    create_rebuild_provenance,
    exact_retry_run_ids,
    inspect_rebuild_provenance,
    lock_rebuild_gate,
    participant_identity_errors,
    rebuild_runs,
    win5_graph_present,
)
from umacircle_bot.services._s5e_rebuild_rating import inspect_upstream_artifacts
from umacircle_bot.services.current_win5_replay import execute_current_win5_replay_graph
from umacircle_bot.services.current_win5_replay_provenance import (
    create_current_win5_replay_provenance,
    inspect_current_win5_replay_provenance,
    load_current_win5_replay_runs,
)
from umacircle_bot.services.current_win5_replay_state import (
    build_current_win5_replay_graph_report,
    build_current_win5_replay_participant_checksum,
    load_current_win5_replay_participant_accounts,
)
from umacircle_bot.sheets.current_win5_replay_manifest import CurrentWin5ReplayManifest
from umacircle_bot.sheets.s5e_rebuild_manifest import S5ERebuildManifest, canonical_checksum

__all__ = (
    "S5ERebuildApplyResult",
    "S5ERebuildPreview",
    "apply_s5e_rebuild_current_win5",
    "build_s5e_score_reward_checksum",
    "preview_s5e_rebuild",
)


@dataclass(frozen=True, slots=True)
class S5ERebuildPreview:
    mode: str
    can_apply: bool
    manifest_checksum: str
    participant_checksum: str
    rating_signature_checksum: str | None
    score_reward_checksum: str
    business_signature: str | None
    phase_reports: dict[str, object]
    point_report: dict[str, object]
    graph_report: dict[str, object] | None
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class S5ERebuildApplyResult:
    mode: str
    rebuild_import_run_id: int
    current_win5_replay_run_id: int
    season_id: int
    round_id: int
    business_signature: str
    final_report: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def preview_s5e_rebuild(
    session: Session,
    *,
    manifest: S5ERebuildManifest,
    win5_manifest: CurrentWin5ReplayManifest,
    bot_commit: str,
    runtime_bot_commit: str,
    e2_artifact_file_checksum: str,
) -> S5ERebuildPreview:
    """Inspect the V1 baseline-first state before apply or after one exact replay."""

    _require_clean_session(session)
    errors: list[str] = []
    if bot_commit != manifest.source_commit or runtime_bot_commit != manifest.source_commit:
        errors.append("source_commit_mismatch")
    if e2_artifact_file_checksum != manifest.phase_a.artifact_file_checksum:
        errors.append("e2_artifact_file_checksum_mismatch")
    revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if revision != manifest.alembic_head or revision != EXPECTED_ALEMBIC_HEAD:
        errors.append("alembic_head_mismatch")
    if (
        win5_manifest.manifest_checksum != manifest.phase_d.win5_manifest_checksum
        or win5_manifest.file_checksum != manifest.phase_d.win5_file_checksum
    ):
        errors.append("win5_graph_artifact_mismatch")

    participant_checksum = build_current_win5_replay_participant_checksum(win5_manifest.participant_discord_user_ids)
    if participant_checksum != manifest.phase_d.participant_checksum:
        errors.append("win5_participant_checksum_mismatch")
    score_reward_checksum = build_s5e_score_reward_checksum(win5_manifest)
    if score_reward_checksum != manifest.phase_d.score_reward_checksum:
        errors.append("win5_score_reward_checksum_mismatch")

    upstream_errors, rating_signature = inspect_upstream_artifacts(session, manifest=manifest)
    errors.extend(upstream_errors)
    participant_errors, participant_accounts = participant_identity_errors(session, manifest=win5_manifest)
    errors.extend(participant_errors)

    replay_runs = load_current_win5_replay_runs(session, lock=False)
    rebuild_runs_found = rebuild_runs(session, lock=False)
    graph_present = win5_graph_present(session)
    if not replay_runs and not rebuild_runs_found and not graph_present:
        mode = "ready"
        include_phase_d = False
    elif len(replay_runs) == 1 and len(rebuild_runs_found) == 1 and graph_present:
        mode = "exact_retry"
        include_phase_d = True
    else:
        mode = "conflict"
        include_phase_d = graph_present
        errors.append("s5e_rebuild_partial_state")

    phase_reports, point_report, phase_errors, business_signature = inspect_point_phases(
        session,
        manifest=manifest,
        win5_manifest=win5_manifest,
        participant_accounts=participant_accounts,
        include_phase_d=include_phase_d,
        rating_signature=rating_signature,
        score_reward_checksum=score_reward_checksum,
    )
    errors.extend(phase_errors)

    graph_report: dict[str, object] | None = None
    if include_phase_d:
        graph_report, graph_errors = build_current_win5_replay_graph_report(session, manifest=win5_manifest)
        errors.extend(graph_errors)
        if (
            graph_report.get("season_score") != manifest.phase_d.expected_season_score
            or graph_report.get("top1_score") != manifest.phase_d.expected_top1_score
        ):
            errors.append("win5_score_total_mismatch")
        final_report = {**graph_report, "room_points": point_report}
        provenance_errors, replay_run = inspect_current_win5_replay_provenance(
            session,
            runs=replay_runs,
            manifest=win5_manifest,
            participant_checksum=participant_checksum,
            final_report=final_report,
            bot_commit=manifest.source_commit,
            lock=False,
        )
        errors.extend(provenance_errors)
        if replay_run is None:
            errors.append("current_win5_replay_provenance_missing")
        errors.extend(
            inspect_rebuild_provenance(
                session,
                runs=rebuild_runs_found,
                manifest=manifest,
                preview_business_signature=business_signature,
                final_report=final_report,
                current_win5_replay_run_id=replay_run.id if replay_run is not None else None,
                lock=False,
            )
        )

    normalized_errors = tuple(dict.fromkeys(errors))
    return S5ERebuildPreview(
        mode=mode,
        can_apply=mode in {"ready", "exact_retry"} and not normalized_errors,
        manifest_checksum=manifest.manifest_checksum,
        participant_checksum=participant_checksum,
        rating_signature_checksum=rating_signature,
        score_reward_checksum=score_reward_checksum,
        business_signature=business_signature,
        phase_reports=phase_reports,
        point_report=point_report,
        graph_report=graph_report,
        errors=normalized_errors,
    )


def apply_s5e_rebuild_current_win5(
    session: Session,
    *,
    manifest: S5ERebuildManifest,
    win5_manifest: CurrentWin5ReplayManifest,
    bot_commit: str,
    runtime_bot_commit: str,
    e2_artifact_file_checksum: str,
) -> S5ERebuildApplyResult:
    """Apply phase D through native WIN5 services or prove an exact retry."""

    _require_clean_session(session)
    _ensure_outer_database_transaction(session)
    try:
        with session.begin_nested():
            lock_rebuild_gate(session)
            preview = preview_s5e_rebuild(
                session,
                manifest=manifest,
                win5_manifest=win5_manifest,
                bot_commit=bot_commit,
                runtime_bot_commit=runtime_bot_commit,
                e2_artifact_file_checksum=e2_artifact_file_checksum,
            )
            if not preview.can_apply:
                raise LegacyImportConflictError("S5E rebuild precondition failed: " + ",".join(preview.errors[:5]))
            if preview.mode == "exact_retry":
                return _exact_retry_result(session, preview=preview)

            season_id, round_id = execute_current_win5_replay_graph(session, manifest=win5_manifest)
            graph_report, graph_errors = build_current_win5_replay_graph_report(session, manifest=win5_manifest)
            if graph_errors:
                raise LegacyImportConflictError("S5E WIN5 graph verification failed: " + ",".join(graph_errors[:5]))
            participant_accounts = load_current_win5_replay_participant_accounts(session, manifest=win5_manifest)
            phase_reports, point_report, phase_errors, business_signature = inspect_point_phases(
                session,
                manifest=manifest,
                win5_manifest=win5_manifest,
                participant_accounts=participant_accounts,
                include_phase_d=True,
                rating_signature=preview.rating_signature_checksum,
                score_reward_checksum=preview.score_reward_checksum,
            )
            if phase_errors or business_signature is None:
                raise LegacyImportConflictError(
                    "S5E final reconciliation failed: " + ",".join(phase_errors[:5] or ("business_signature_missing",))
                )
            final_report = {**graph_report, "room_points": point_report}
            replay_run = create_current_win5_replay_provenance(
                session,
                manifest=win5_manifest,
                participant_checksum=preview.participant_checksum,
                season_id=season_id,
                round_id=round_id,
                final_report=final_report,
                bot_commit=manifest.source_commit,
            )
            rebuild_run = create_rebuild_provenance(
                session,
                manifest=manifest,
                business_signature=business_signature,
                phase_reports=phase_reports,
                final_report=final_report,
                current_win5_replay_run_id=replay_run.id,
            )
            return S5ERebuildApplyResult(
                mode="applied",
                rebuild_import_run_id=rebuild_run.id,
                current_win5_replay_run_id=replay_run.id,
                season_id=season_id,
                round_id=round_id,
                business_signature=business_signature,
                final_report=final_report,
            )
    except IntegrityError as exc:
        session.expire_all()
        retry = preview_s5e_rebuild(
            session,
            manifest=manifest,
            win5_manifest=win5_manifest,
            bot_commit=bot_commit,
            runtime_bot_commit=runtime_bot_commit,
            e2_artifact_file_checksum=e2_artifact_file_checksum,
        )
        if retry.can_apply and retry.mode == "exact_retry":
            return _exact_retry_result(session, preview=retry)
        raise exc


def build_s5e_score_reward_checksum(manifest: CurrentWin5ReplayManifest) -> str:
    rows: list[dict[str, object]] = []
    participant_by_id = {participant.discord_user_id: participant for participant in manifest.participants}
    for submission in sorted(manifest.round.submissions, key=lambda row: row.discord_user_id):
        participant = participant_by_id[submission.discord_user_id]
        judgement = submission.expected_judgement
        rows.append(
            {
                "discord_user_id": submission.discord_user_id,
                "uma_pid": participant.uma_pid,
                "prediction_tier": submission.prediction_tier,
                "exact_position_count": judgement.exact_position_count,
                "on_board_wrong_position_count": judgement.on_board_wrong_position_count,
                "off_board_count": judgement.off_board_count,
                "season_score_delta": judgement.season_score_delta,
                "top1_score_delta": judgement.top1_score_delta,
                "circle_point_delta": win5_circle_point_reward(
                    prediction_tier=submission.prediction_tier,
                    exact_position_count=judgement.exact_position_count,
                ),
            }
        )
    return canonical_checksum(
        {
            "win5_manifest_checksum": manifest.manifest_checksum,
            "rows": rows,
            "season_score": manifest.expected_season_score,
            "top1_score": manifest.expected_top1_score,
        }
    )


def _exact_retry_result(
    session: Session,
    *,
    preview: S5ERebuildPreview,
) -> S5ERebuildApplyResult:
    if preview.graph_report is None or preview.business_signature is None:
        raise LegacyImportConflictError("S5E exact retry report is incomplete")
    rebuild_import_run_id, current_win5_replay_run_id = exact_retry_run_ids(session)
    season_id = preview.graph_report.get("season_id")
    round_id = preview.graph_report.get("round_id")
    if not isinstance(season_id, int) or not isinstance(round_id, int):
        raise LegacyImportConflictError("S5E exact retry graph targets are missing")
    return S5ERebuildApplyResult(
        mode="exact_retry",
        rebuild_import_run_id=rebuild_import_run_id,
        current_win5_replay_run_id=current_win5_replay_run_id,
        season_id=season_id,
        round_id=round_id,
        business_signature=preview.business_signature,
        final_report={**preview.graph_report, "room_points": preview.point_report},
    )


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("S5E rebuild requires a clean session")


def _ensure_outer_database_transaction(session: Session) -> None:
    connection = session.connection()
    if connection.dialect.name != "sqlite":
        return
    driver_connection = connection.connection.driver_connection
    if not driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
