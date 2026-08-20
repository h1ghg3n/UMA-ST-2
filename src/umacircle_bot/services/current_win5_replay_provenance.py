from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import SheetImportRecord, SheetImportRun
from umacircle_bot.sheets.current_win5_replay_manifest import (
    CURRENT_WIN5_REPLAY_MANIFEST_VERSION,
    CURRENT_WIN5_REPLAY_MODE,
    CurrentWin5ReplayManifest,
)

CURRENT_WIN5_REPLAY_IMPORT_KIND = "current_win5_epoch_replay"
CURRENT_WIN5_REPLAY_SOURCE_TYPE = "protected_json"
CURRENT_WIN5_REPLAY_RECORD_TYPE = "current_win5_epoch_replay"
CURRENT_WIN5_REPLAY_SOURCE_SHEET = "CURRENT_WIN5_EPOCH"


def create_current_win5_replay_provenance(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
    participant_checksum: str,
    season_id: int,
    round_id: int,
    final_report: dict[str, object],
    bot_commit: str,
) -> SheetImportRun:
    run = SheetImportRun(
        import_kind=CURRENT_WIN5_REPLAY_IMPORT_KIND,
        source_type=CURRENT_WIN5_REPLAY_SOURCE_TYPE,
        source_identifier=CURRENT_WIN5_REPLAY_MODE,
        source_checksum=manifest.manifest_checksum,
        status="running",
    )
    session.add(run)
    session.flush()
    detail = _provenance_detail(
        manifest=manifest,
        participant_checksum=participant_checksum,
        season_id=season_id,
        round_id=round_id,
        final_report=final_report,
        bot_commit=bot_commit,
    )
    session.add(
        SheetImportRecord(
            import_run_id=run.id,
            source_key=_replay_source_key(manifest),
            row_fingerprint=manifest.file_checksum,
            source_sheet_name=CURRENT_WIN5_REPLAY_SOURCE_SHEET,
            source_row_number=1,
            record_type=CURRENT_WIN5_REPLAY_RECORD_TYPE,
            status="applied",
            target_entity_type="win5_season",
            target_entity_id=season_id,
            detail_json=detail,
        )
    )
    run.status = "completed"
    run.finished_at = datetime.now(UTC)
    run.summary_json = detail
    session.flush()
    return run


def inspect_current_win5_replay_provenance(
    session: Session,
    *,
    runs: tuple[SheetImportRun, ...],
    manifest: CurrentWin5ReplayManifest,
    participant_checksum: str,
    final_report: dict[str, object],
    bot_commit: str,
    lock: bool,
) -> tuple[tuple[str, ...], SheetImportRun | None]:
    if len(runs) != 1:
        return ("replay_import_run_count",), None
    run = runs[0]
    statement = select(SheetImportRecord).where(SheetImportRecord.import_run_id == run.id)
    if lock:
        statement = statement.with_for_update()
    records = tuple(session.scalars(statement))
    season_id = final_report.get("season_id")
    round_id = final_report.get("round_id")
    if not isinstance(season_id, int) or not isinstance(round_id, int):
        return ("replay_target_missing",), None
    expected_detail = _provenance_detail(
        manifest=manifest,
        participant_checksum=participant_checksum,
        season_id=season_id,
        round_id=round_id,
        final_report=final_report,
        bot_commit=bot_commit,
    )
    errors: list[str] = []
    if (
        run.source_type != CURRENT_WIN5_REPLAY_SOURCE_TYPE
        or run.source_identifier != CURRENT_WIN5_REPLAY_MODE
        or run.source_checksum != manifest.manifest_checksum
        or run.status != "completed"
        or run.finished_at is None
        or run.summary_json != expected_detail
    ):
        errors.append("replay_import_run_mismatch")
    record = records[0] if len(records) == 1 else None
    if record is None or (
        record.source_key != _replay_source_key(manifest)
        or record.row_fingerprint != manifest.file_checksum
        or record.source_sheet_name != CURRENT_WIN5_REPLAY_SOURCE_SHEET
        or record.source_row_number != 1
        or record.record_type != CURRENT_WIN5_REPLAY_RECORD_TYPE
        or record.status != "applied"
        or record.target_entity_type != "win5_season"
        or record.target_entity_id != season_id
        or record.detail_json != expected_detail
    ):
        errors.append("replay_import_record_mismatch")
    return tuple(errors), run if not errors else None


def load_current_win5_replay_runs(session: Session, *, lock: bool) -> tuple[SheetImportRun, ...]:
    statement = (
        select(SheetImportRun)
        .where(SheetImportRun.import_kind == CURRENT_WIN5_REPLAY_IMPORT_KIND)
        .order_by(SheetImportRun.id)
    )
    if lock:
        statement = statement.with_for_update()
    return tuple(session.scalars(statement))


def _provenance_detail(
    *,
    manifest: CurrentWin5ReplayManifest,
    participant_checksum: str,
    season_id: int,
    round_id: int,
    final_report: dict[str, object],
    bot_commit: str,
) -> dict[str, object]:
    room_points = final_report.get("room_points")
    point_report = room_points if isinstance(room_points, dict) else {}
    return {
        "manifest_version": CURRENT_WIN5_REPLAY_MANIFEST_VERSION,
        "manifest_checksum": manifest.manifest_checksum,
        "file_checksum": manifest.file_checksum,
        "bot_commit": bot_commit,
        "room_match_source_identifier": manifest.room_match_source_identifier,
        "room_match_source_checksum": manifest.room_match_source_checksum,
        "participant_count": len(manifest.participants),
        "participant_checksum": participant_checksum,
        "season_id": season_id,
        "round_id": round_id,
        "submission_count": final_report.get("submission_count"),
        "judgement_count": final_report.get("judgement_count"),
        "season_score": final_report.get("season_score"),
        "top1_score": final_report.get("top1_score"),
        "wallet_total": point_report.get("wallet_total"),
        "transaction_total": point_report.get("transaction_total"),
        "win5_reward_total": point_report.get("win5_reward_total"),
    }


def _replay_source_key(manifest: CurrentWin5ReplayManifest) -> str:
    return sha256(f"{CURRENT_WIN5_REPLAY_IMPORT_KIND}:{manifest.manifest_checksum}".encode()).hexdigest()
