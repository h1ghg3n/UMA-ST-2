from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Bet,
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    Persona,
    Race,
    RaceCondition,
    RaceEntry,
    RaceRatingContext,
    RaceResult,
    RatingEvent,
    RatingRuleVersion,
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
from umacircle_bot.domain.errors import InvalidUmaPidError, LegacyImportError, MatchRatingConflictError
from umacircle_bot.domain.identity import IdentityStatus, validate_registration_pid
from umacircle_bot.domain.imports import normalize_import_source_identifier, normalize_sha256_hex
from umacircle_bot.domain.player_link import normalize_player_name_strict
from umacircle_bot.runtime_preflight import EXPECTED_ALEMBIC_HEAD, EXPECTED_ROOM_POINT_SCALE
from umacircle_bot.services.circle_point_integrity import count_historical_bet_owner_audit_mismatches
from umacircle_bot.services.legacy_identity_mapping import (
    HISTORY_CONTEXT_ONLY,
    LEGACY_IDENTITY_DISPOSITIONS,
    LEGACY_IDENTITY_MAPPING_IMPORT_KIND,
    LEGACY_IDENTITY_MAPPING_RECORD_TYPE,
    LEGACY_IDENTITY_MAPPING_SOURCE_TYPE,
    MAPPING_MANIFEST_VERSION,
    RATING_REPLAY,
    UNRESOLVED_HISTORY,
    build_legacy_identity_mapping_manifest,
)
from umacircle_bot.services.legacy_import import LEGACY_IDENTITY_POINT_IMPORT_KIND
from umacircle_bot.services.legacy_ledger_import import LEGACY_LEDGER_IMPORT_KIND
from umacircle_bot.services.legacy_payout_correction import LEGACY_PAYOUT_CORRECTION_IMPORT_KIND
from umacircle_bot.services.legacy_race_import import LEGACY_RACE_IMPORT_KIND
from umacircle_bot.services.legacy_rating_backfill import verify_source_rating_snapshot
from umacircle_bot.services.legacy_rating_chain import RATING_REPLAY_MODES
from umacircle_bot.services.legacy_rating_chain_disposition import (
    LEGACY_RATING_CHAIN_DISPOSITION_IMPORT_KIND,
    LEGACY_RATING_CHAIN_DISPOSITION_SOURCE_TYPE,
    load_applied_legacy_rating_chain_dispositions,
)
from umacircle_bot.services.legacy_result_import import (
    LEGACY_RESULT_IMPORT_KIND,
    LEGACY_RESULT_RECORD_TYPE,
    LEGACY_RESULT_TARGET_ENTITY_TYPE,
)

WIN5_OPERATIONAL_MODELS = (
    Win5Season,
    Win5Round,
    Win5RoundRace,
    Win5Entry,
    Win5Pick,
    Win5Result,
    Win5Judgement,
    Win5Score,
    Win5ScoreEvent,
    Win5OperationAudit,
)
REQUIRED_IMPORT_KINDS = (
    LEGACY_IDENTITY_POINT_IMPORT_KIND,
    LEGACY_RACE_IMPORT_KIND,
    LEGACY_RESULT_IMPORT_KIND,
    LEGACY_LEDGER_IMPORT_KIND,
    LEGACY_PAYOUT_CORRECTION_IMPORT_KIND,
)
_BOT_COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def build_fresh_win5_launch_preflight(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
    bot_commit: str,
    runtime_bot_commit: str,
    participant_discord_user_ids: tuple[str, ...],
) -> dict[str, object]:
    """Build a read-only first-Season launch manifest and readiness report."""

    normalized_source = normalize_import_source_identifier(source_identifier)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    normalized_commit = _normalize_bot_commit(bot_commit)
    normalized_runtime_commit = _normalize_bot_commit(runtime_bot_commit)
    participant_ids = _normalize_participant_ids(participant_discord_user_ids)
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("fresh WIN5 launch preflight requires a clean session")

    revision = session.scalar(text("SELECT version_num FROM alembic_version"))
    point_scale = session.scalar(text("SELECT scale_version FROM room_point_scale_state WHERE id = 1"))
    import_runs = {
        import_kind: tuple(
            session.scalars(
                select(SheetImportRun).where(
                    SheetImportRun.import_kind == import_kind,
                    SheetImportRun.source_type == "xlsx",
                    SheetImportRun.source_identifier == normalized_source,
                    SheetImportRun.source_checksum == normalized_checksum,
                    SheetImportRun.status == "completed",
                    SheetImportRun.finished_at.is_not(None),
                )
            )
        )
        for import_kind in REQUIRED_IMPORT_KINDS
    }
    import_run_counts = {import_kind: len(runs) for import_kind, runs in import_runs.items()}

    win5_table_counts = {
        model.__tablename__: int(session.scalar(select(func.count()).select_from(model)) or 0)
        for model in WIN5_OPERATIONAL_MODELS
    }
    win5_race_count = int(session.scalar(select(func.count()).select_from(Race).where(Race.race_kind == "win5")) or 0)

    room_points, room_point_errors = _room_point_report(session)
    result_import_run = (
        import_runs[LEGACY_RESULT_IMPORT_KIND][0] if import_run_counts[LEGACY_RESULT_IMPORT_KIND] == 1 else None
    )
    result_provenance, result_provenance_errors = _result_provenance_report(
        session,
        source_identifier=normalized_source,
        result_import_run=result_import_run,
    )
    identity_mapping, identity_mapping_errors = _identity_mapping_report(
        session,
        source_identifier=normalized_source,
        source_checksum=normalized_checksum,
        source_result_count=int(result_provenance["source_result_count"]),
    )
    rating, rating_errors = _rating_report(
        session,
        source_identifier=normalized_source,
        source_checksum=normalized_checksum,
    )
    participant_checks, participant_errors, participant_warnings = _participant_checks(
        session,
        participant_ids=participant_ids,
    )
    warnings: list[str] = []
    if identity_mapping["not_started"]:
        warnings.append("identity_mapping_not_started")
    if rating["not_started"]:
        warnings.append("rating_backfill_not_started")
    warnings.extend(participant_warnings)
    errors: list[str] = []
    if identity_mapping["not_started"]:
        errors.append("identity_mapping_not_started")
    if rating["not_started"]:
        errors.append("rating_backfill_not_started")
    if revision != EXPECTED_ALEMBIC_HEAD:
        errors.append("unexpected_alembic_revision")
    if normalized_runtime_commit != normalized_commit:
        errors.append("bot_commit_mismatch")
    if point_scale != EXPECTED_ROOM_POINT_SCALE:
        errors.append("unexpected_room_point_scale")
    errors.extend(f"import_run_count:{import_kind}" for import_kind, count in import_run_counts.items() if count != 1)
    errors.extend(room_point_errors)
    errors.extend(result_provenance_errors)
    errors.extend(identity_mapping_errors)
    errors.extend(rating_errors)
    win5_reward_transaction_count = int(
        session.scalar(
            select(func.count())
            .select_from(CirclePointTransaction)
            .where(
                or_(
                    CirclePointTransaction.type == "win5_reward",
                    CirclePointTransaction.source == "win5_score",
                )
            )
        )
        or 0
    )
    if any(win5_table_counts.values()) or win5_race_count or win5_reward_transaction_count:
        errors.append("win5_epoch_not_empty")
    errors.extend(participant_errors)

    return {
        "manifest_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_identifier": normalized_source,
        "source_checksum": normalized_checksum,
        "bot_commit": normalized_commit,
        "runtime_bot_commit": normalized_runtime_commit,
        "expected_alembic_revision": EXPECTED_ALEMBIC_HEAD,
        "alembic_revision": revision,
        "room_point_scale": point_scale,
        "import_run_counts": import_run_counts,
        "result_provenance": result_provenance,
        "identity_mapping": identity_mapping,
        "room_points": room_points,
        "rating": rating,
        "win5_table_counts": win5_table_counts,
        "win5_race_count": win5_race_count,
        "win5_reward_transaction_count": win5_reward_transaction_count,
        "expected_first_season_number": 1,
        "participant_manifest": {
            "participant_count": len(participant_ids),
            "participant_checksum": _participant_checksum(participant_ids),
        },
        "participant_checks": participant_checks,
        "ready_to_launch": not errors,
        "errors": tuple(errors),
        "warnings": tuple(warnings),
    }


def _room_point_report(session: Session) -> tuple[dict[str, object], tuple[str, ...]]:
    wallet_rows = tuple(
        session.execute(
            select(CirclePointAccount.persona_id, CirclePointAccount.balance).order_by(CirclePointAccount.persona_id)
        )
    )
    transaction_total_rows = tuple(
        session.execute(
            select(CirclePointTransaction.persona_id, func.sum(CirclePointTransaction.amount))
            .group_by(CirclePointTransaction.persona_id)
            .order_by(CirclePointTransaction.persona_id)
        )
    )
    wallet_balances = {str(row.persona_id): int(row.balance) for row in wallet_rows}
    transaction_totals = {str(row.persona_id): int(row[1] or 0) for row in transaction_total_rows}
    mismatched_persona_ids = tuple(
        persona_id
        for persona_id in sorted(set(wallet_balances) | set(transaction_totals))
        if wallet_balances.get(persona_id, 0) != transaction_totals.get(persona_id, 0)
    )
    transaction_count = int(session.scalar(select(func.count()).select_from(CirclePointTransaction)) or 0)
    transaction_orphan_count = int(
        session.scalar(
            select(func.count())
            .select_from(CirclePointTransaction)
            .outerjoin(Persona, Persona.id == CirclePointTransaction.persona_id)
            .outerjoin(CirclePointAccount, CirclePointAccount.persona_id == CirclePointTransaction.persona_id)
            .outerjoin(GameAccount, GameAccount.id == CirclePointTransaction.game_account_id)
            .where(
                or_(
                    Persona.id.is_(None),
                    CirclePointAccount.id.is_(None),
                    GameAccount.id.is_(None),
                )
            )
        )
        or 0
    )
    bet_orphan_count = int(
        session.scalar(
            select(func.count())
            .select_from(Bet)
            .outerjoin(Persona, Persona.id == Bet.persona_id)
            .outerjoin(CirclePointAccount, CirclePointAccount.persona_id == Bet.persona_id)
            .outerjoin(GameAccount, GameAccount.id == Bet.game_account_id)
            .where(
                or_(
                    Persona.id.is_(None),
                    CirclePointAccount.id.is_(None),
                    GameAccount.id.is_(None),
                )
            )
        )
        or 0
    )
    bet_ledger_provenance_mismatch_count = int(
        session.scalar(
            select(func.count())
            .select_from(CirclePointTransaction)
            .outerjoin(Bet, Bet.id == CirclePointTransaction.related_bet_id)
            .where(
                CirclePointTransaction.related_bet_id.is_not(None),
                or_(
                    Bet.id.is_(None),
                    Bet.persona_id != CirclePointTransaction.persona_id,
                    Bet.game_account_id != CirclePointTransaction.game_account_id,
                ),
            )
        )
        or 0
    )
    historical_bet_owner_audit_mismatch_count = count_historical_bet_owner_audit_mismatches(session)
    provenance_error_count = (
        transaction_orphan_count
        + bet_orphan_count
        + bet_ledger_provenance_mismatch_count
        + historical_bet_owner_audit_mismatch_count
    )
    wallet_total = sum(wallet_balances.values())
    transaction_total = sum(transaction_totals.values())
    errors: list[str] = []
    if not wallet_rows or transaction_count == 0:
        errors.append("missing_room_point_history")
    if wallet_total != transaction_total or mismatched_persona_ids:
        errors.append("room_point_reconciliation_mismatch")
    if provenance_error_count:
        errors.append("room_point_transaction_owner_mismatch")
    return (
        {
            "wallet_count": len(wallet_rows),
            "transaction_count": transaction_count,
            "wallet_total": wallet_total,
            "transaction_total": transaction_total,
            "mismatched_persona_count": len(mismatched_persona_ids),
            "mismatched_persona_ids": mismatched_persona_ids,
            "transaction_owner_mismatch_count": provenance_error_count,
            "transaction_orphan_count": transaction_orphan_count,
            "bet_orphan_count": bet_orphan_count,
            "bet_ledger_provenance_mismatch_count": bet_ledger_provenance_mismatch_count,
            "historical_bet_owner_audit_mismatch_count": historical_bet_owner_audit_mismatch_count,
        },
        tuple(errors),
    )


def _result_provenance_report(
    session: Session,
    *,
    source_identifier: str,
    result_import_run: SheetImportRun | None,
) -> tuple[dict[str, object], tuple[str, ...]]:
    source_races = tuple(
        session.scalars(select(Race).where(Race.external_source == source_identifier, Race.race_kind == "room_match"))
    )
    source_results = tuple(
        session.scalars(
            select(RaceResult)
            .join(Race, Race.id == RaceResult.race_id)
            .where(Race.external_source == source_identifier, Race.race_kind == "room_match")
            .order_by(RaceResult.id)
        )
    )
    record_ids = tuple(
        result.source_import_record_id for result in source_results if result.source_import_record_id is not None
    )
    records = (
        {
            record.id: record
            for record in session.scalars(select(SheetImportRecord).where(SheetImportRecord.id.in_(record_ids)))
        }
        if record_ids
        else {}
    )
    entries = (
        {
            entry.source_import_record_id: entry
            for entry in session.scalars(select(RaceEntry).where(RaceEntry.source_import_record_id.in_(record_ids)))
        }
        if record_ids
        else {}
    )

    linked_record_count = 0
    authoritative_count = 0
    synthetic_count = 0
    provenance_error_count = 0
    for result in source_results:
        record_id = result.source_import_record_id
        record = records.get(record_id) if record_id is not None else None
        entry = entries.get(record_id) if record_id is not None else None
        detail = record.detail_json if record is not None and isinstance(record.detail_json, dict) else {}
        valid = (
            result_import_run is not None
            and record is not None
            and entry is not None
            and record.import_run_id == result_import_run.id
            and record.record_type == LEGACY_RESULT_RECORD_TYPE
            and record.status == "applied"
            and record.target_entity_type == LEGACY_RESULT_TARGET_ENTITY_TYPE
            and record.target_entity_id == result.id
            and entry.race_id == result.race_id
            and entry.entry_number == result.entry_number
            and detail.get("race_entry_id") == entry.id
            and detail.get("race_result_id") == result.id
            and detail.get("race_id") == result.race_id
            and detail.get("entry_number") == result.entry_number
        )
        if valid:
            linked_record_count += 1
        else:
            provenance_error_count += 1
        if entry is not None and entry.entry_number_source == "synthetic":
            synthetic_count += 1
        elif entry is not None:
            authoritative_count += 1

    summary = result_import_run.summary_json if result_import_run is not None else None
    expected_summary = {
        "row_count": len(source_results),
        "race_count": len(source_races),
        "authoritative_entry_number_count": authoritative_count,
        "synthetic_entry_number_count": synthetic_count,
        "rating_excluded_count": sum(result.is_rating_excluded for result in source_results),
    }
    summary_matches = isinstance(summary, dict) and all(
        summary.get(key) == value for key, value in expected_summary.items()
    )
    errors: list[str] = []
    if not source_results or not source_races:
        errors.append("missing_result_import_data")
    if provenance_error_count or linked_record_count != len(source_results):
        errors.append("result_import_provenance_mismatch")
    if not summary_matches:
        errors.append("result_import_summary_mismatch")
    return (
        {
            "result_import_run_id": result_import_run.id if result_import_run is not None else None,
            "source_race_count": len(source_races),
            "source_result_count": len(source_results),
            "linked_record_count": linked_record_count,
            "provenance_error_count": provenance_error_count,
            "summary_matches": summary_matches,
        },
        tuple(errors),
    )


def _identity_mapping_report(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
    source_result_count: int,
) -> tuple[dict[str, object], tuple[str, ...]]:
    all_runs = tuple(
        session.scalars(
            select(SheetImportRun).where(
                SheetImportRun.import_kind == LEGACY_IDENTITY_MAPPING_IMPORT_KIND,
                SheetImportRun.source_type == LEGACY_IDENTITY_MAPPING_SOURCE_TYPE,
                SheetImportRun.source_identifier == source_identifier,
            )
        )
    )
    completed_runs = tuple(
        candidate for candidate in all_runs if candidate.status == "completed" and candidate.finished_at is not None
    )
    run = completed_runs[0] if len(all_runs) == 1 and len(completed_runs) == 1 else None
    summary = run.summary_json if run is not None and isinstance(run.summary_json, dict) else {}
    mapping_checksum = summary.get("mapping_checksum")
    decision_checksum = summary.get("decision_checksum")
    try:
        live_mapping_manifest = build_legacy_identity_mapping_manifest(
            session,
            source_identifier=source_identifier,
            source_checksum=source_checksum,
        )
        live_mapping_checksum = live_mapping_manifest.get("mapping_checksum")
    except LegacyImportError:
        live_mapping_checksum = None
    mapping_count = summary.get("mapping_count")
    occurrence_count = summary.get("occurrence_count")
    mapped_occurrence_count = summary.get("mapped_occurrence_count")
    skipped_occurrence_count = summary.get("skipped_occurrence_count")
    rating_replay_count = summary.get("rating_replay_count")
    history_context_only_count = summary.get("history_context_only_count")
    unresolved_history_count = summary.get("unresolved_history_count")
    history_context_only_occurrence_count = summary.get("history_context_only_occurrence_count")
    unresolved_history_occurrence_count = summary.get("unresolved_history_occurrence_count")
    replay_occurrence_total = (
        mapped_occurrence_count + skipped_occurrence_count
        if all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (mapped_occurrence_count, skipped_occurrence_count)
        )
        else None
    )
    summary_matches = (
        run is not None
        and summary.get("mapping_manifest_version") == MAPPING_MANIFEST_VERSION
        and summary.get("source_checksum") == source_checksum
        and _is_sha256(mapping_checksum)
        and mapping_checksum == live_mapping_checksum
        and _is_sha256(decision_checksum)
        and run.source_checksum == decision_checksum
        and isinstance(mapping_count, int)
        and not isinstance(mapping_count, bool)
        and mapping_count > 0
        and isinstance(occurrence_count, int)
        and not isinstance(occurrence_count, bool)
        and occurrence_count == source_result_count
        and isinstance(mapped_occurrence_count, int)
        and not isinstance(mapped_occurrence_count, bool)
        and isinstance(skipped_occurrence_count, int)
        and not isinstance(skipped_occurrence_count, bool)
        and isinstance(history_context_only_occurrence_count, int)
        and not isinstance(history_context_only_occurrence_count, bool)
        and isinstance(unresolved_history_occurrence_count, int)
        and not isinstance(unresolved_history_occurrence_count, bool)
        and mapped_occurrence_count
        + skipped_occurrence_count
        + history_context_only_occurrence_count
        + unresolved_history_occurrence_count
        == occurrence_count
        and all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (rating_replay_count, history_context_only_count, unresolved_history_count)
        )
        and rating_replay_count + history_context_only_count + unresolved_history_count == mapping_count
    )
    records = (
        tuple(
            session.scalars(
                select(SheetImportRecord)
                .where(SheetImportRecord.import_run_id == run.id)
                .order_by(SheetImportRecord.source_row_number)
            )
        )
        if run is not None
        else ()
    )
    record_names: set[str] = set()
    record_disposition_counts: Counter[str] = Counter()
    record_disposition_occurrences: Counter[str] = Counter()
    record_error_count = 0
    for record in records:
        detail = record.detail_json if isinstance(record.detail_json, dict) else {}
        normalized_name = detail.get("normalized_player_name")
        disposition = detail.get("disposition")
        approved_game_account_id = detail.get("approved_game_account_id")
        approved_persona_id = detail.get("approved_persona_id")
        relationship_snapshot = detail.get("relationship_snapshot")
        rating_replay_mode = detail.get("rating_replay_mode")
        operator_note = detail.get("operator_note")
        affected_occurrence_count = detail.get("affected_occurrence_count")
        replay_target_valid = (
            disposition == RATING_REPLAY
            and isinstance(approved_game_account_id, int)
            and not isinstance(approved_game_account_id, bool)
            and approved_game_account_id > 0
            and record.target_entity_type == "game_account"
            and record.target_entity_id == approved_game_account_id
            and isinstance(relationship_snapshot, dict)
            and rating_replay_mode in RATING_REPLAY_MODES
        )
        non_replay_target_valid = (
            disposition in {HISTORY_CONTEXT_ONLY, UNRESOLVED_HISTORY}
            and approved_game_account_id is None
            and approved_persona_id is None
            and record.target_entity_type is None
            and record.target_entity_id is None
            and relationship_snapshot is None
            and rating_replay_mode is None
            and isinstance(operator_note, str)
            and bool(operator_note.strip())
        )
        valid = (
            record.record_type == LEGACY_IDENTITY_MAPPING_RECORD_TYPE
            and record.status == "applied"
            and (replay_target_valid or non_replay_target_valid)
            and detail.get("source_checksum") == source_checksum
            and detail.get("mapping_manifest_version") == MAPPING_MANIFEST_VERSION
            and detail.get("mapping_checksum") == mapping_checksum
            and detail.get("decision_checksum") == decision_checksum
            and isinstance(normalized_name, str)
            and bool(normalized_name)
            and normalized_name not in record_names
            and detail.get("source_chain_key")
            == _legacy_identity_source_chain_key(
                source_identifier=source_identifier,
                normalized_player_name=normalized_name if isinstance(normalized_name, str) else "",
            )
            and disposition in LEGACY_IDENTITY_DISPOSITIONS
            and (approved_persona_id is None or isinstance(approved_persona_id, str))
            and isinstance(affected_occurrence_count, int)
            and not isinstance(affected_occurrence_count, bool)
            and affected_occurrence_count > 0
        )
        if valid:
            record_names.add(normalized_name)
            record_disposition_counts[str(disposition)] += 1
            record_disposition_occurrences[str(disposition)] += affected_occurrence_count
        else:
            record_error_count += 1
    if not isinstance(mapping_count, int) or isinstance(mapping_count, bool) or len(records) != mapping_count:
        record_error_count += 1
    if record_disposition_counts != Counter(
        {
            RATING_REPLAY: rating_replay_count,
            HISTORY_CONTEXT_ONLY: history_context_only_count,
            UNRESOLVED_HISTORY: unresolved_history_count,
        }
    ):
        record_error_count += 1
    if record_disposition_occurrences[HISTORY_CONTEXT_ONLY] != history_context_only_occurrence_count:
        record_error_count += 1
    if record_disposition_occurrences[UNRESOLVED_HISTORY] != unresolved_history_occurrence_count:
        record_error_count += 1
    if record_disposition_occurrences[RATING_REPLAY] != replay_occurrence_total:
        record_error_count += 1

    reviewed_by_value = summary.get("reviewed_by")
    reviewed_at = summary.get("reviewed_at")
    evidence_note = summary.get("evidence_note")
    reviewed_by = (
        tuple(reviewed_by_value)
        if isinstance(reviewed_by_value, (list, tuple))
        and all(
            isinstance(reviewer, str) and reviewer and reviewer == reviewer.strip() for reviewer in reviewed_by_value
        )
        else ()
    )
    review_matches = (
        len(reviewed_by) >= 2
        and reviewed_by == tuple(sorted(set(reviewed_by)))
        and _is_timezone_aware_iso(reviewed_at)
        and (evidence_note is None or isinstance(evidence_note, str))
    )
    if review_matches:
        decisions = [
            {
                "normalized_player_name": str(record.detail_json["normalized_player_name"]),
                "source_chain_key": record.detail_json.get("source_chain_key"),
                "approved_game_account_id": record.detail_json.get("approved_game_account_id"),
                "approved_persona_id": record.detail_json.get("approved_persona_id"),
                "disposition": record.detail_json.get("disposition"),
                "rating_replay_mode": record.detail_json.get("rating_replay_mode"),
                "relationship_snapshot": record.detail_json.get("relationship_snapshot"),
                "operator_note": record.detail_json.get("operator_note"),
            }
            for record in records
            if isinstance(record.detail_json, dict)
            and isinstance(record.detail_json.get("normalized_player_name"), str)
            and record.detail_json.get("disposition") in LEGACY_IDENTITY_DISPOSITIONS
            and (
                record.detail_json.get("operator_note") is None
                or isinstance(record.detail_json.get("operator_note"), str)
            )
        ]
        decisions.sort(key=lambda decision: decision["normalized_player_name"])
        decision_payload = {
            "manifest_version": MAPPING_MANIFEST_VERSION,
            "source_identifier": source_identifier,
            "source_checksum": source_checksum,
            "mapping_checksum": mapping_checksum,
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
            "evidence_note": evidence_note,
            "decisions": decisions,
        }
        recomputed_decision_checksum = sha256(
            json.dumps(decision_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        review_matches = len(decisions) == mapping_count and recomputed_decision_checksum == decision_checksum
    summary_matches = summary_matches and review_matches

    source_owner_rows = tuple(
        session.execute(
            select(RaceEntry.player_name, RaceEntry.game_account_id, RaceResult.game_account_id)
            .join(Race, Race.id == RaceEntry.race_id)
            .join(
                RaceResult,
                (RaceResult.race_id == RaceEntry.race_id) & (RaceResult.entry_number == RaceEntry.entry_number),
            )
            .where(Race.external_source == source_identifier, Race.race_kind == "room_match")
        )
    )
    record_details_by_name = {
        str(record.detail_json["normalized_player_name"]): record.detail_json
        for record in records
        if isinstance(record.detail_json, dict) and isinstance(record.detail_json.get("normalized_player_name"), str)
    }
    owner_link_error_count = 0
    source_names: set[str] = set()
    for player_name, entry_account_id, result_account_id in source_owner_rows:
        normalized_name = normalize_player_name_strict(player_name or "")
        source_names.add(normalized_name)
        detail = record_details_by_name.get(normalized_name)
        disposition = detail.get("disposition") if detail is not None else None
        approved_game_account_id = detail.get("approved_game_account_id") if detail is not None else None
        if disposition == RATING_REPLAY:
            if not (entry_account_id is not None and entry_account_id == result_account_id == approved_game_account_id):
                owner_link_error_count += 1
        elif disposition in {HISTORY_CONTEXT_ONLY, UNRESOLVED_HISTORY}:
            if entry_account_id is not None or result_account_id is not None:
                owner_link_error_count += 1
        else:
            owner_link_error_count += 1
    if source_names != set(record_details_by_name):
        record_error_count += 1

    represented_account_ids = {
        int(detail["approved_game_account_id"])
        for detail in record_details_by_name.values()
        if detail.get("disposition") == RATING_REPLAY
        and isinstance(detail.get("approved_game_account_id"), int)
        and not isinstance(detail.get("approved_game_account_id"), bool)
    }
    accounts = (
        {
            account.id: account
            for account in session.scalars(select(GameAccount).where(GameAccount.id.in_(represented_account_ids)))
        }
        if represented_account_ids
        else {}
    )
    persona_ids = {account.persona_id for account in accounts.values() if account.persona_id is not None}
    peer_ids_by_persona = {
        persona_id: tuple(sorted(session.scalars(select(GameAccount.id).where(GameAccount.persona_id == persona_id))))
        for persona_id in persona_ids
    }
    ownership_error_count = 0
    for detail in record_details_by_name.values():
        if detail.get("disposition") != RATING_REPLAY:
            continue
        account_id = detail.get("approved_game_account_id")
        account = accounts.get(account_id) if isinstance(account_id, int) else None
        expected_relationship = (
            {
                "game_account_id": account.id,
                "persona_id": account.persona_id,
                "persona_peer_game_account_ids": list(peer_ids_by_persona.get(account.persona_id, ())),
                "identity_status": account.identity_status,
                "is_source_only": account.persona_id is None,
            }
            if account is not None
            else None
        )
        approved_persona_id = detail.get("approved_persona_id")
        if (
            account is None
            or (approved_persona_id is not None and account.persona_id != approved_persona_id)
            or detail.get("relationship_snapshot") != expected_relationship
        ):
            ownership_error_count += 1

    not_started = (
        not all_runs
        and len(source_owner_rows) == source_result_count
        and all(
            entry_account_id is None and result_account_id is None
            for _player_name, entry_account_id, result_account_id in source_owner_rows
        )
    )
    if not_started:
        record_error_count = 0

    errors: list[str] = []
    if not not_started:
        if len(all_runs) != 1 or len(completed_runs) != 1:
            errors.append("identity_mapping_run_count")
        if not summary_matches or record_error_count:
            errors.append("identity_mapping_audit_mismatch")
        if len(source_owner_rows) != source_result_count or owner_link_error_count:
            errors.append("identity_mapping_incomplete")
        if ownership_error_count:
            errors.append("identity_mapping_ownership_mismatch")
        if unresolved_history_count:
            errors.append("identity_mapping_unresolved_history")
    return (
        {
            "import_run_id": run.id if run is not None else None,
            "run_count": len(all_runs),
            "completed_run_count": len(completed_runs),
            "not_started": not_started,
            "mapping_checksum": mapping_checksum,
            "live_mapping_checksum": live_mapping_checksum,
            "decision_checksum": decision_checksum,
            "mapping_count": mapping_count,
            "occurrence_count": occurrence_count,
            "rating_replay_count": rating_replay_count,
            "history_context_only_count": history_context_only_count,
            "unresolved_history_count": unresolved_history_count,
            "record_count": len(records),
            "record_error_count": record_error_count,
            "source_owner_row_count": len(source_owner_rows),
            "owner_link_error_count": owner_link_error_count,
            "represented_account_count": len(represented_account_ids),
            "ownership_error_count": ownership_error_count,
            "reviewer_count": len(reviewed_by),
            "review_matches": review_matches,
            "summary_matches": summary_matches,
        },
        tuple(errors),
    )


def _rating_report(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
) -> tuple[dict[str, object], tuple[str, ...]]:
    rating_versions = tuple(
        session.scalars(select(RatingRuleVersion).where(RatingRuleVersion.source_checksum == source_checksum))
    )
    rating_version_id = rating_versions[0].id if len(rating_versions) == 1 else None
    expected_rows = tuple(
        session.execute(
            select(RaceResult.id, RaceResult.race_id, RaceResult.game_account_id)
            .join(Race, Race.id == RaceResult.race_id)
            .join(RaceCondition, RaceCondition.race_id == Race.id)
            .where(
                Race.external_source == source_identifier,
                Race.race_kind == "room_match",
                func.upper(RaceCondition.grade) != "OP",
                RaceResult.is_rating_excluded.is_(False),
                RaceResult.is_result_void.is_(False),
            )
            .order_by(RaceResult.id)
        )
    )
    eligible_by_result = {int(row.id): (int(row.race_id), row.game_account_id) for row in expected_rows}
    expected_context_race_ids = {race_id for race_id, _game_account_id in eligible_by_result.values()}
    source_events = tuple(
        session.scalars(
            select(RatingEvent)
            .join(Race, Race.id == RatingEvent.race_id)
            .where(Race.external_source == source_identifier)
            .order_by(RatingEvent.id)
        )
    )
    total_event_count = int(session.scalar(select(func.count()).select_from(RatingEvent)) or 0)
    source_contexts = tuple(
        session.scalars(
            select(RaceRatingContext)
            .join(Race, Race.id == RaceRatingContext.race_id)
            .where(Race.external_source == source_identifier)
            .order_by(RaceRatingContext.race_id)
        )
    )
    total_context_count = int(session.scalar(select(func.count()).select_from(RaceRatingContext)) or 0)
    not_started = total_event_count == 0 and total_context_count == 0
    disposition_run_count = int(
        session.scalar(
            select(func.count())
            .select_from(SheetImportRun)
            .where(
                SheetImportRun.import_kind == LEGACY_RATING_CHAIN_DISPOSITION_IMPORT_KIND,
                SheetImportRun.source_type == LEGACY_RATING_CHAIN_DISPOSITION_SOURCE_TYPE,
                SheetImportRun.source_identifier == source_identifier,
            )
        )
        or 0
    )
    disposition_audit = None
    disposition_audit_error = False
    if disposition_run_count:
        try:
            disposition_audit = load_applied_legacy_rating_chain_dispositions(
                session,
                source_identifier=source_identifier,
                source_checksum=source_checksum,
            )
        except LegacyImportError:
            disposition_audit_error = True
    else:
        disposition_audit_error = True

    if disposition_audit is not None:
        replay_result_ids = set(disposition_audit.replay_result_ids)
        context_only_result_ids = set(disposition_audit.history_context_only_result_ids)
        unresolved_result_ids = set(disposition_audit.unresolved_history_result_ids)
    elif not_started:
        replay_result_ids = set()
        context_only_result_ids = set()
        unresolved_result_ids = set()
    else:
        replay_result_ids = set(eligible_by_result)
        context_only_result_ids = set()
        unresolved_result_ids = {
            result_id for result_id, (_race_id, account_id) in eligible_by_result.items() if account_id is None
        }
    disposition_coverage_mismatch_count = (
        len((replay_result_ids | context_only_result_ids | unresolved_result_ids) ^ set(eligible_by_result))
        + len(
            (replay_result_ids & context_only_result_ids)
            | (replay_result_ids & unresolved_result_ids)
            | (context_only_result_ids & unresolved_result_ids)
        )
        if disposition_audit is not None or not not_started
        else 0
    )

    event_result_counts = Counter(event.race_result_id for event in source_events)
    actual_result_ids = set(event_result_counts)
    duplicate_result_count = sum(count - 1 for count in event_result_counts.values() if count > 1)
    missing_result_count = len(replay_result_ids - actual_result_ids)
    unexpected_result_count = len(actual_result_ids - replay_result_ids)
    linkage_mismatch_count = sum(
        1
        for event in source_events
        if event.race_result_id in eligible_by_result
        and (event.race_id, event.game_account_id) != eligible_by_result[event.race_result_id]
    )
    unresolved_result_count = len(unresolved_result_ids)
    mismatched_version_count = (
        sum(event.rating_rule_version_id != rating_version_id for event in source_events)
        if rating_version_id is not None
        else len(source_events)
    )

    actual_context_race_ids = {context.race_id for context in source_contexts}
    missing_context_count = len(expected_context_race_ids - actual_context_race_ids)
    unexpected_context_count = len(actual_context_race_ids - expected_context_race_ids)
    context_version_mismatch_count = sum(
        not isinstance(context.raw_context_json, dict)
        or context.raw_context_json.get("rating_rule_version_id") != rating_version_id
        for context in source_contexts
    )
    context_disposition_mismatch_count = 0
    if disposition_audit is not None:
        for context in source_contexts:
            raw = context.raw_context_json if isinstance(context.raw_context_json, dict) else {}
            expected_replay = sorted(
                result_id
                for result_id in replay_result_ids
                if eligible_by_result.get(result_id, (None, None))[0] == context.race_id
            )
            expected_context_only = sorted(
                result_id
                for result_id in context_only_result_ids
                if eligible_by_result.get(result_id, (None, None))[0] == context.race_id
            )
            if (
                raw.get("rating_replay_result_ids") != expected_replay
                or raw.get("history_context_only_result_ids") != expected_context_only
            ):
                context_disposition_mismatch_count += 1

    snapshot_mismatch_count = 0
    if not not_started:
        contexts_by_race_id = {context.race_id: context for context in source_contexts}
        for race_id in sorted(expected_context_race_ids):
            try:
                raw = (
                    contexts_by_race_id[race_id].raw_context_json
                    if race_id in contexts_by_race_id
                    and isinstance(contexts_by_race_id[race_id].raw_context_json, dict)
                    else {}
                )
                verify_source_rating_snapshot(
                    session,
                    race_id=race_id,
                    expected_recalculation_decision_checksum=(
                        disposition_audit.decision_checksum
                        if disposition_audit is not None and raw.get("mode") == "historical_recalculation"
                        else None
                    ),
                )
            except MatchRatingConflictError:
                snapshot_mismatch_count += 1

    represented_account_ids = {
        int(eligible_by_result[result_id][1])
        for result_id in replay_result_ids
        if result_id in eligible_by_result and eligible_by_result[result_id][1] is not None
    }
    represented_accounts = (
        {
            account.id: account
            for account in session.scalars(select(GameAccount).where(GameAccount.id.in_(represented_account_ids)))
        }
        if represented_account_ids
        else {}
    )
    represented_persona_ids = {account.persona_id for account in represented_accounts.values() if account.persona_id}
    identity_ownership_error_count = sum(
        account_id not in represented_accounts for account_id in represented_account_ids
    )
    identity_ownership_error_count += sum(
        eligible_by_result[result_id][1] is None for result_id in replay_result_ids if result_id in eligible_by_result
    )
    identity_ownership_error_count += sum(
        eligible_by_result[result_id][1] is not None
        for result_id in context_only_result_ids
        if result_id in eligible_by_result
    )

    errors: list[str] = []
    if len(rating_versions) > 1 or (not not_started and len(rating_versions) != 1):
        errors.append("rating_rule_version_count")
    if disposition_audit_error or disposition_coverage_mismatch_count:
        errors.append("rating_chain_disposition_audit_mismatch")
    if unresolved_result_count:
        errors.append("unresolved_rating_identity")
    if not not_started:
        if expected_rows and (not source_contexts or (replay_result_ids and not source_events)):
            errors.append("missing_rating_backfill")
        if len(source_events) != total_event_count:
            errors.append("unexpected_rating_event_history")
        if len(source_contexts) != total_context_count:
            errors.append("unexpected_rating_context_history")
        if len(source_events) != len(replay_result_ids):
            errors.append("rating_event_count_mismatch")
        if duplicate_result_count or missing_result_count or unexpected_result_count:
            errors.append("rating_event_result_set_mismatch")
        if linkage_mismatch_count:
            errors.append("rating_event_linkage_mismatch")
        if missing_context_count or unexpected_context_count:
            errors.append("rating_context_race_set_mismatch")
        if identity_ownership_error_count:
            errors.append("rating_identity_ownership_mismatch")
        if mismatched_version_count:
            errors.append("rating_rule_version_mismatch")
        if context_version_mismatch_count:
            errors.append("rating_context_version_mismatch")
        if context_disposition_mismatch_count:
            errors.append("rating_context_disposition_mismatch")
        if snapshot_mismatch_count:
            errors.append("rating_source_snapshot_mismatch")
    return (
        {
            "rating_rule_version_id": rating_version_id,
            "rating_rule_version_count": len(rating_versions),
            "not_started": not_started,
            "disposition_import_run_id": disposition_audit.import_run_id if disposition_audit is not None else None,
            "disposition_decision_checksum": (
                disposition_audit.decision_checksum if disposition_audit is not None else None
            ),
            "disposition_run_count": disposition_run_count,
            "disposition_audit_error": disposition_audit_error,
            "disposition_coverage_mismatch_count": disposition_coverage_mismatch_count,
            "source_event_count": len(source_events),
            "source_context_count": len(source_contexts),
            "expected_event_count": len(replay_result_ids),
            "expected_context_only_result_count": len(context_only_result_ids),
            "expected_context_count": len(expected_context_race_ids),
            "duplicate_result_count": duplicate_result_count,
            "missing_result_count": missing_result_count,
            "unexpected_result_count": unexpected_result_count,
            "linkage_mismatch_count": linkage_mismatch_count,
            "missing_context_count": missing_context_count,
            "unexpected_context_count": unexpected_context_count,
            "unresolved_result_count": unresolved_result_count,
            "mismatched_version_count": mismatched_version_count,
            "context_version_mismatch_count": context_version_mismatch_count,
            "context_disposition_mismatch_count": context_disposition_mismatch_count,
            "snapshot_mismatch_count": snapshot_mismatch_count,
            "represented_account_count": len(represented_account_ids),
            "represented_persona_count": len(represented_persona_ids),
            "identity_ownership_error_count": identity_ownership_error_count,
        },
        tuple(errors),
    )


def _participant_checks(
    session: Session,
    *,
    participant_ids: tuple[str, ...],
) -> tuple[tuple[dict[str, object], ...], tuple[str, ...], tuple[str, ...]]:
    checks: list[dict[str, object]] = []
    errors: list[str] = []
    warnings: list[str] = []
    participant_persona_ids: set[str] = set()
    for discord_user_id in participant_ids:
        discord = session.scalar(select(DiscordAccount).where(DiscordAccount.discord_user_id == discord_user_id))
        persona = session.get(Persona, discord.persona_id) if discord is not None and discord.persona_id else None
        accounts = (
            tuple(
                session.scalars(
                    select(GameAccount).where(GameAccount.persona_id == persona.id).order_by(GameAccount.id)
                )
            )
            if persona is not None
            else ()
        )
        confirmed_accounts: list[GameAccount] = []
        invalid_confirmed_account_ids: list[int] = []
        for account in accounts:
            if account.identity_status != IdentityStatus.CONFIRMED.value:
                continue
            if account.uma_pid is None:
                invalid_confirmed_account_ids.append(account.id)
                continue
            try:
                validate_registration_pid(account.uma_pid)
            except InvalidUmaPidError:
                invalid_confirmed_account_ids.append(account.id)
            else:
                confirmed_accounts.append(account)
        wallet = (
            session.scalar(select(CirclePointAccount).where(CirclePointAccount.persona_id == persona.id))
            if persona is not None
            else None
        )
        error: str | None = None
        if discord is None:
            error = "discord_account_missing"
        elif persona is None or persona.status != "active":
            error = "active_persona_missing"
        elif persona.id in participant_persona_ids:
            error = "participant_persona_duplicate"
        elif not confirmed_accounts:
            error = (
                "confirmed_game_account_invalid_pid"
                if invalid_confirmed_account_ids
                else "confirmed_game_account_missing"
            )
        if error is None and wallet is None:
            error = "participant_point_account_missing"
        if persona is not None:
            participant_persona_ids.add(persona.id)
        checks.append(
            {
                "discord_user_id": discord_user_id,
                "persona_id": persona.id if persona is not None else None,
                "game_account_ids": tuple(account.id for account in accounts),
                "game_account_count": len(accounts),
                "confirmed_game_account_ids": tuple(account.id for account in confirmed_accounts),
                "confirmed_game_account_count": len(confirmed_accounts),
                "invalid_confirmed_game_account_ids": tuple(invalid_confirmed_account_ids),
                "ready": error is None,
                "error": error,
            }
        )
        if invalid_confirmed_account_ids:
            account_ids = ",".join(str(account_id) for account_id in invalid_confirmed_account_ids)
            warnings.append(f"participant:{discord_user_id}:invalid_confirmed_game_accounts:{account_ids}")
        if error is not None:
            errors.append(f"participant:{discord_user_id}:{error}")
    return tuple(checks), tuple(errors), tuple(warnings)


def _normalize_participant_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        raise LegacyImportError("fresh WIN5 launch preflight requires at least one participant")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise LegacyImportError("participant Discord user ID must be text")
        item = value.strip()
        if not item.isascii() or not item.isdigit() or not 1 <= len(item) <= 32:
            raise LegacyImportError("participant Discord user ID must contain 1 to 32 ASCII digits")
        if item in normalized:
            raise LegacyImportError("participant Discord user IDs must be unique")
        normalized.add(item)
    return tuple(sorted(normalized))


def _participant_checksum(participant_ids: tuple[str, ...]) -> str:
    payload = "fresh-win5-participants-v1\n" + "\n".join(participant_ids)
    return sha256(payload.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _legacy_identity_source_chain_key(*, source_identifier: str, normalized_player_name: str) -> str:
    payload = f"{LEGACY_IDENTITY_MAPPING_IMPORT_KIND}\0{source_identifier}\0{normalized_player_name}"
    return sha256(payload.encode("utf-8")).hexdigest()


def _is_timezone_aware_iso(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _normalize_bot_commit(value: str) -> str:
    if not isinstance(value, str):
        raise LegacyImportError("bot commit must be text")
    normalized = value.strip().lower()
    if _BOT_COMMIT_PATTERN.fullmatch(normalized) is None:
        raise LegacyImportError("bot commit must be a full 40 or 64 character hexadecimal revision")
    return normalized
