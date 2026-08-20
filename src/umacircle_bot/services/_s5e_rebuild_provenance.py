from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    AccountRegistrationOperationAudit,
    AccountRegistrationRequest,
    Bet,
    BetJudgement,
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    Persona,
    Race,
    RaceRatingContext,
    RaceResult,
    RatingEvent,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.errors import LegacyImportConflictError, LegacyImportError
from umacircle_bot.services.current_win5_replay_provenance import load_current_win5_replay_runs
from umacircle_bot.services.current_win5_replay_registration import registration_provenance_matches
from umacircle_bot.services.current_win5_replay_state import lock_current_win5_replay_gate
from umacircle_bot.services.fresh_win5_launch import WIN5_OPERATIONAL_MODELS
from umacircle_bot.services.legacy_import import LEGACY_IDENTITY_POINT_RECORD_TYPE
from umacircle_bot.sheets.current_win5_replay_manifest import (
    CurrentWin5ReplayManifest,
    CurrentWin5ReplayParticipant,
)
from umacircle_bot.sheets.s5e_rebuild_manifest import S5ERebuildManifest, canonical_checksum

S5E_REBUILD_IMPORT_KIND = "s5e_rebuild_reconciliation"
S5E_REBUILD_SOURCE_TYPE = "protected_json"
S5E_REBUILD_RECORD_TYPE = "s5e_rebuild_reconciliation"
S5E_REBUILD_SOURCE_SHEET = "S5E_REBUILD"


def build_registration_provenance_checksum(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
) -> str:
    participants = tuple(row for row in manifest.participants if row.provenance_kind == "account_registration")
    if len(participants) != 1:
        raise LegacyImportError("S5E registration provenance requires one reviewed participant")
    participant = participants[0]
    persona, account = participant_identity(session, participant)
    if (
        persona is None
        or account is None
        or not registration_provenance_matches(
            session,
            participant=participant,
            persona=persona,
            account=account,
        )
    ):
        raise LegacyImportError("S5E registration provenance does not match the reviewed participant")
    return canonical_checksum(
        {
            "discord_user_id": participant.discord_user_id,
            "discord_nickname": participant.discord_nickname,
            "uma_pid": participant.uma_pid,
            "nickname": participant.nickname,
            "ingame_name": participant.ingame_name,
            "guild_id": participant.registration_guild_id,
            "approved_by": participant.registration_approved_by_discord_user_id,
            "initial_grant": participant.registration_target_initial_grant,
        }
    )


def participant_identity_errors(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
) -> tuple[tuple[str, ...], dict[str, GameAccount]]:
    errors: list[str] = []
    accounts: dict[str, GameAccount] = {}
    persona_ids: set[str] = set()
    account_ids: set[int] = set()
    for index, participant in enumerate(manifest.participants, 1):
        persona, account = participant_identity(session, participant)
        wallet = (
            session.scalar(select(CirclePointAccount).where(CirclePointAccount.persona_id == persona.id))
            if persona is not None
            else None
        )
        provenance_matches = False
        if persona is not None and account is not None:
            if participant.provenance_kind == "approved_player_link" and participant.identity_source_key is not None:
                records = tuple(
                    session.scalars(
                        select(SheetImportRecord).where(
                            SheetImportRecord.source_key == participant.identity_source_key,
                            SheetImportRecord.record_type == LEGACY_IDENTITY_POINT_RECORD_TYPE,
                            SheetImportRecord.status == "applied",
                        )
                    )
                )
                provenance_matches = (
                    len(records) == 1
                    and records[0].target_entity_type == "game_account"
                    and records[0].target_entity_id == account.id
                )
            elif participant.provenance_kind == "account_registration":
                provenance_matches = registration_provenance_matches(
                    session,
                    participant=participant,
                    persona=persona,
                    account=account,
                )
        if (
            persona is None
            or persona.status != "active"
            or account is None
            or wallet is None
            or persona.id in persona_ids
            or account.id in account_ids
            or not provenance_matches
        ):
            errors.append(f"participant_identity_mismatch:{index}")
        else:
            accounts[participant.discord_user_id] = account
            persona_ids.add(persona.id)
            account_ids.add(account.id)
    return tuple(errors), accounts


def participant_identity(
    session: Session,
    participant: CurrentWin5ReplayParticipant,
) -> tuple[Persona | None, GameAccount | None]:
    discord = session.scalar(
        select(DiscordAccount).where(DiscordAccount.discord_user_id == participant.discord_user_id)
    )
    persona = session.get(Persona, discord.persona_id) if discord is not None and discord.persona_id else None
    accounts = (
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
    return persona, accounts[0] if len(accounts) == 1 else None


def win5_graph_present(session: Session) -> bool:
    if any(int(session.scalar(select(func.count()).select_from(model)) or 0) for model in WIN5_OPERATIONAL_MODELS):
        return True
    return bool(
        session.scalar(select(Race.id).where(Race.race_kind == "win5").limit(1))
        or session.scalar(
            select(CirclePointTransaction.id)
            .where((CirclePointTransaction.source == "win5_score") | (CirclePointTransaction.type == "win5_reward"))
            .limit(1)
        )
    )


def create_rebuild_provenance(
    session: Session,
    *,
    manifest: S5ERebuildManifest,
    business_signature: str,
    phase_reports: dict[str, object],
    final_report: dict[str, object],
    current_win5_replay_run_id: int,
) -> SheetImportRun:
    season_id = final_report.get("season_id")
    if not isinstance(season_id, int):
        raise LegacyImportConflictError("S5E final report Season is missing")
    detail = rebuild_provenance_detail(
        manifest=manifest,
        business_signature=business_signature,
        phase_reports=phase_reports,
        final_report=final_report,
        current_win5_replay_run_id=current_win5_replay_run_id,
    )
    run = SheetImportRun(
        import_kind=S5E_REBUILD_IMPORT_KIND,
        source_type=S5E_REBUILD_SOURCE_TYPE,
        source_identifier=S5E_REBUILD_IMPORT_KIND,
        source_checksum=manifest.manifest_checksum,
        status="running",
    )
    session.add(run)
    session.flush()
    session.add(
        SheetImportRecord(
            import_run_id=run.id,
            source_key=rebuild_source_key(manifest),
            row_fingerprint=manifest.file_checksum,
            source_sheet_name=S5E_REBUILD_SOURCE_SHEET,
            source_row_number=1,
            record_type=S5E_REBUILD_RECORD_TYPE,
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


def inspect_rebuild_provenance(
    session: Session,
    *,
    runs: Sequence[SheetImportRun],
    manifest: S5ERebuildManifest,
    preview_business_signature: str | None,
    final_report: dict[str, object],
    current_win5_replay_run_id: int | None,
    lock: bool,
) -> tuple[str, ...]:
    if len(runs) != 1 or preview_business_signature is None or current_win5_replay_run_id is None:
        return ("s5e_rebuild_provenance_missing",)
    run = runs[0]
    statement = select(SheetImportRecord).where(SheetImportRecord.import_run_id == run.id)
    if lock:
        statement = statement.with_for_update()
    records = tuple(session.scalars(statement))
    record = records[0] if len(records) == 1 else None
    if (
        run.source_type != S5E_REBUILD_SOURCE_TYPE
        or run.source_identifier != S5E_REBUILD_IMPORT_KIND
        or run.source_checksum != manifest.manifest_checksum
        or run.status != "completed"
        or run.finished_at is None
        or not isinstance(run.summary_json, dict)
        or run.summary_json.get("business_signature") != preview_business_signature
        or run.summary_json.get("manifest_checksum") != manifest.manifest_checksum
        or run.summary_json.get("current_win5_replay_run_id") != current_win5_replay_run_id
        or record is None
        or record.row_fingerprint != manifest.file_checksum
        or record.record_type != S5E_REBUILD_RECORD_TYPE
        or record.status != "applied"
        or record.detail_json != run.summary_json
        or record.target_entity_id != final_report.get("season_id")
    ):
        return ("s5e_rebuild_provenance_changed",)
    return ()


def rebuild_provenance_detail(
    *,
    manifest: S5ERebuildManifest,
    business_signature: str,
    phase_reports: dict[str, object],
    final_report: dict[str, object],
    current_win5_replay_run_id: int,
) -> dict[str, object]:
    return {
        "manifest_version": 1,
        "manifest_checksum": manifest.manifest_checksum,
        "manifest_file_checksum": manifest.file_checksum,
        "source_commit": manifest.source_commit,
        "alembic_head": manifest.alembic_head,
        "business_signature": business_signature,
        "rating_signature_checksum": manifest.reviewed_artifacts.rating_signature_checksum,
        "win5_manifest_checksum": manifest.phase_d.win5_manifest_checksum,
        "current_win5_replay_run_id": current_win5_replay_run_id,
        "season_id": final_report.get("season_id"),
        "round_id": final_report.get("round_id"),
        "season_score": final_report.get("season_score"),
        "top1_score": final_report.get("top1_score"),
        "phase_reports": phase_reports,
    }


def rebuild_source_key(manifest: S5ERebuildManifest) -> str:
    return canonical_checksum({"kind": S5E_REBUILD_IMPORT_KIND, "manifest_checksum": manifest.manifest_checksum})


def rebuild_runs(session: Session, *, lock: bool) -> tuple[SheetImportRun, ...]:
    statement = (
        select(SheetImportRun).where(SheetImportRun.import_kind == S5E_REBUILD_IMPORT_KIND).order_by(SheetImportRun.id)
    )
    if lock:
        statement = statement.with_for_update()
    return tuple(session.scalars(statement))


def lock_rebuild_gate(session: Session) -> None:
    lock_current_win5_replay_gate(session)
    for model in (
        SheetImportRecord,
        AccountRegistrationRequest,
        AccountRegistrationOperationAudit,
        Bet,
        BetJudgement,
        RaceResult,
        RaceRatingContext,
        RatingEvent,
    ):
        tuple(session.scalars(select(model).order_by(model.id).with_for_update()))


def exact_retry_run_ids(session: Session) -> tuple[int, int]:
    replay_runs = load_current_win5_replay_runs(session, lock=False)
    rebuild_import_runs = rebuild_runs(session, lock=False)
    if len(replay_runs) != 1 or len(rebuild_import_runs) != 1:
        raise LegacyImportConflictError("S5E exact retry provenance is incomplete")
    summary = rebuild_import_runs[0].summary_json
    if not isinstance(summary, dict) or summary.get("current_win5_replay_run_id") != replay_runs[0].id:
        raise LegacyImportConflictError("S5E exact retry replay run link changed")
    return rebuild_import_runs[0].id, replay_runs[0].id
