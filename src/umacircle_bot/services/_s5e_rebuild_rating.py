from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Race,
    RaceRatingContext,
    RaceResult,
    RatingEvent,
    RatingRuleVersion,
    SheetImportRecord,
)
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.services.legacy_identity_mapping import (
    HISTORY_CONTEXT_ONLY,
    RATING_REPLAY,
    load_applied_legacy_identity_mapping_audit,
)
from umacircle_bot.services.legacy_rating_backfill import verify_source_rating_snapshot
from umacircle_bot.services.legacy_rating_chain_disposition import (
    load_applied_legacy_rating_chain_dispositions,
)
from umacircle_bot.services.legacy_source_account_seed import load_applied_legacy_source_account_seed
from umacircle_bot.sheets.s5e_rebuild_manifest import S5ERebuildManifest, canonical_checksum


def inspect_upstream_artifacts(
    session: Session,
    *,
    manifest: S5ERebuildManifest,
) -> tuple[tuple[str, ...], str | None]:
    errors: list[str] = []
    source = manifest.source_lineage.source_identifier
    checksum = manifest.source_lineage.latest_workbook_checksum
    try:
        seed = load_applied_legacy_source_account_seed(
            session,
            source_identifier=source,
            source_checksum=checksum,
        )
        if seed is None or (
            seed.seed_basis_checksum != manifest.reviewed_artifacts.source_seed_basis_checksum
            or seed.decision_checksum != manifest.reviewed_artifacts.source_seed_decision_checksum
        ):
            errors.append("source_seed_artifact_mismatch")
    except LegacyImportError:
        errors.append("source_seed_artifact_mismatch")

    mapping = None
    try:
        mapping = load_applied_legacy_identity_mapping_audit(
            session,
            source_identifier=source,
            source_checksum=checksum,
            confirmed_decision_checksum=manifest.reviewed_artifacts.mapping_decision_checksum,
        )
        if mapping.mapping_checksum != manifest.reviewed_artifacts.mapping_checksum:
            errors.append("mapping_artifact_mismatch")
    except LegacyImportError:
        errors.append("mapping_artifact_mismatch")

    dispositions = None
    try:
        dispositions = load_applied_legacy_rating_chain_dispositions(
            session,
            source_identifier=source,
            source_checksum=checksum,
            confirmed_decision_checksum=manifest.reviewed_artifacts.rating_decision_checksum,
        )
        if (
            dispositions.mapping_checksum != manifest.reviewed_artifacts.mapping_checksum
            or dispositions.mapping_decision_checksum != manifest.reviewed_artifacts.mapping_decision_checksum
            or dispositions.disposition_checksum != manifest.reviewed_artifacts.rating_disposition_checksum
            or dispositions.unresolved_history_result_ids
        ):
            errors.append("rating_disposition_artifact_mismatch")
    except LegacyImportError:
        errors.append("rating_disposition_artifact_mismatch")

    rating_signature: str | None = None
    if mapping is not None and dispositions is not None:
        try:
            rating_signature = rating_signature_for_state(session, mapping=mapping, dispositions=dispositions)
        except LegacyImportError:
            errors.append("rating_signature_invalid")
        if rating_signature != manifest.reviewed_artifacts.rating_signature_checksum:
            errors.append("rating_signature_mismatch")
    return tuple(dict.fromkeys(errors)), rating_signature


def rating_signature_for_state(session: Session, *, mapping, dispositions) -> str:
    decisions_by_source_key = {
        source_key: decision for decision in mapping.decisions for source_key in decision.source_record_keys
    }
    results = tuple(
        session.scalars(
            select(RaceResult)
            .join(Race, Race.id == RaceResult.race_id)
            .where(Race.external_source == mapping.source_identifier, Race.race_kind == "room_match")
            .order_by(Race.starts_at, Race.external_race_id, RaceResult.entry_number)
        )
    )
    records = {
        record.id: record
        for record in session.scalars(
            select(SheetImportRecord).where(
                SheetImportRecord.id.in_(
                    [row.source_import_record_id for row in results if row.source_import_record_id is not None]
                )
            )
        )
    }
    events = tuple(session.scalars(select(RatingEvent).order_by(RatingEvent.id)))
    event_by_result: dict[int, RatingEvent] = {}
    for event in events:
        if event.race_result_id in event_by_result:
            raise LegacyImportError("S5E Rating signature found duplicate forward events")
        event_by_result[event.race_result_id] = event
    if set(event_by_result) != set(dispositions.replay_result_ids):
        raise LegacyImportError("S5E Rating event coverage differs from the reviewed disposition")

    rows: list[dict[str, object]] = []
    for result in results:
        record = records.get(result.source_import_record_id) if result.source_import_record_id is not None else None
        decision = decisions_by_source_key.get(record.source_key) if record is not None else None
        if record is None or decision is None:
            raise LegacyImportError("S5E Rating signature source attribution is missing")
        event = event_by_result.get(result.id)
        if result.id in dispositions.replay_result_ids:
            disposition = RATING_REPLAY
            if event is None or event.game_account_id != result.game_account_id:
                raise LegacyImportError("S5E Rating event GameAccount provenance changed")
        elif result.id in dispositions.history_context_only_result_ids:
            disposition = HISTORY_CONTEXT_ONLY
            if event is not None:
                raise LegacyImportError("S5E history-context-only Result has a Rating event")
        else:
            continue
        race = session.get(Race, result.race_id)
        if race is None:
            raise LegacyImportError("S5E Rating Result Race is missing")
        rows.append(
            {
                "source_record_key": record.source_key,
                "source_chain_key": decision.source_chain_key,
                "disposition": disposition,
                "race_external_id": race.external_race_id,
                "entry_number": result.entry_number,
                "rank": result.rank,
                "event": None if event is None else rating_event_business_row(event),
            }
        )

    contexts = tuple(
        session.scalars(
            select(RaceRatingContext)
            .join(Race, Race.id == RaceRatingContext.race_id)
            .where(Race.external_source == mapping.source_identifier, Race.race_kind == "room_match")
            .order_by(Race.starts_at, Race.external_race_id)
        )
    )
    context_rows: list[dict[str, object]] = []
    for context in contexts:
        raw = context.raw_context_json if isinstance(context.raw_context_json, dict) else {}
        recalculation_checksum = raw.get("recalculation_decision_checksum")
        verify_source_rating_snapshot(
            session,
            race_id=context.race_id,
            expected_recalculation_decision_checksum=(
                recalculation_checksum if isinstance(recalculation_checksum, str) else None
            ),
        )
        race = session.get(Race, context.race_id)
        if race is None:
            raise LegacyImportError("S5E Rating context Race is missing")
        context_rows.append(
            {
                "race_external_id": race.external_race_id,
                "average_rating_before": decimal_string(context.average_rating_before),
                "participant_count": context.participant_count,
                "grade": context.grade,
                "mode": raw.get("mode"),
                "recalculation_decision_checksum": recalculation_checksum,
            }
        )
    versions = tuple(session.scalars(select(RatingRuleVersion).order_by(RatingRuleVersion.version_number)))
    return canonical_checksum(
        {
            "source_identifier": mapping.source_identifier,
            "source_checksum": mapping.source_checksum,
            "mapping_decision_checksum": mapping.decision_checksum,
            "rating_disposition_decision_checksum": dispositions.decision_checksum,
            "rating_rule_versions": [
                {
                    "version_number": row.version_number,
                    "source_identifier": row.source_identifier,
                    "source_checksum": row.source_checksum,
                    "source_sheet_name": row.source_sheet_name,
                    "source_range": row.source_range,
                    "rule_set_checksum": row.rule_set_checksum,
                    "rule_count": row.rule_count,
                }
                for row in versions
            ],
            "contexts": context_rows,
            "results": rows,
        }
    )


def rating_event_business_row(event: RatingEvent) -> dict[str, object]:
    return {
        "grade": event.grade,
        "rank": event.rank,
        "converted_rank": event.converted_rank,
        "rating_before": decimal_string(event.rating_before),
        "base_delta": decimal_string(event.base_delta),
        "adjustment_delta": decimal_string(event.adjustment_delta),
        "total_delta": decimal_string(event.total_delta),
        "rating_after": decimal_string(event.rating_after),
    }


def decimal_string(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
