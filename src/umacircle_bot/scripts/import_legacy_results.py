from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from umacircle_bot.config import get_settings
from umacircle_bot.db.models import Race, SheetImportRecord, SheetImportRun
from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.imports import normalize_sha256_hex
from umacircle_bot.services.legacy_race_import import (
    LEGACY_RACE_IMPORT_KIND,
    LEGACY_RACE_RECORD_TYPE,
    build_legacy_race_import_plan,
)
from umacircle_bot.services.legacy_result_import import (
    LEGACY_RESULT_PREVIOUS_SOURCE_CHECKSUM,
    LEGACY_ROOM_RESULT_IMPORT_MANIFEST,
    LEGACY_ROOM_RESULT_PREVIOUS_IMPORT_MANIFEST,
    LegacyImportedRaceReference,
    LegacyRoomResultImportPreview,
    apply_legacy_room_result_import,
    map_legacy_room_result_plan,
    preview_legacy_room_result_import,
)
from umacircle_bot.sheets.legacy_identity_point_workbook import (
    LegacyRoomResultWorkbookPreparation,
    prepare_legacy_room_result_workbook,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run or apply canonical legacy room-match results")
    parser.add_argument("workbook", type=Path, help="Path to the legacy room-match XLSX workbook")
    parser.add_argument(
        "--source-identifier",
        required=True,
        help="Stable logical source ID; must match the completed legacy race import",
    )
    parser.add_argument("--apply", action="store_true", help="Apply after all dry-run checks pass")
    parser.add_argument(
        "--confirm-checksum",
        help="Required with --apply; must equal the inspected workbook SHA-256",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source_utc_offset_minutes = get_settings().app_utc_offset_minutes
        preparation = prepare_legacy_room_result_workbook(
            args.workbook,
            source_identifier=args.source_identifier,
            source_utc_offset_minutes=source_utc_offset_minutes,
        )
        if args.apply:
            if args.confirm_checksum is None:
                return _write_error("--apply requires --confirm-checksum")
            confirmed_checksum = normalize_sha256_hex(args.confirm_checksum, field_name="confirmed checksum")
            if confirmed_checksum != preparation.source_checksum:
                return _write_error("confirmed checksum does not match the inspected workbook")
        elif args.confirm_checksum is not None:
            return _write_error("--confirm-checksum is only valid with --apply")

        configure_session()
        with SessionLocal() as session:
            persistence_plan = map_legacy_room_result_plan(
                preparation.plan,
                _load_imported_race_references(
                    session,
                    preparation=preparation,
                    source_utc_offset_minutes=source_utc_offset_minutes,
                    lock_rows=False,
                ),
            )
            preview = preview_legacy_room_result_import(
                session,
                plan=persistence_plan,
                source_checksum=preparation.source_checksum,
            )
            summary = _build_summary(preparation, preview)
            if not preview.can_apply:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return 2
            if not args.apply:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return 0

            locked_persistence_plan = map_legacy_room_result_plan(
                preparation.plan,
                _load_imported_race_references(
                    session,
                    preparation=preparation,
                    source_utc_offset_minutes=source_utc_offset_minutes,
                    lock_rows=True,
                ),
            )
            result = apply_legacy_room_result_import(
                session,
                plan=locked_persistence_plan,
                source_checksum=preparation.source_checksum,
            )
            session.commit()
            summary.update(
                {
                    "mode": "apply",
                    "import_run_id": result.import_run_id,
                    "created_count": result.created_count,
                    "skipped_count": result.skipped_count,
                }
            )
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return 0
    except LegacyImportError as exc:
        logger.info("legacy result import rejected: %s", exc)
        return _write_error(str(exc))
    except OSError:
        logger.exception("legacy result workbook could not be accessed")
        return _write_error("workbook could not be accessed")
    except ValueError:
        logger.exception("legacy result import request is invalid")
        return _write_error("invalid import request")
    except SQLAlchemyError:
        logger.exception("legacy result import database operation failed")
        return _write_error("database operation failed")
    except Exception:
        logger.exception("unexpected legacy result import failure")
        return _write_error("unexpected internal error")
    finally:
        dispose_session_engine()


def _load_imported_race_references(
    session: Session,
    *,
    preparation: LegacyRoomResultWorkbookPreparation,
    source_utc_offset_minutes: int,
    lock_rows: bool,
) -> tuple[LegacyImportedRaceReference, ...]:
    race_plan = build_legacy_race_import_plan(
        preparation.race_rows,
        source_identifier=preparation.plan.source_identifier,
        source_utc_offset_minutes=source_utc_offset_minutes,
    )
    supports_append_extension = (
        len(preparation.plan.rows) == LEGACY_ROOM_RESULT_IMPORT_MANIFEST.row_count
        and preparation.plan.race_count == LEGACY_ROOM_RESULT_IMPORT_MANIFEST.race_count
        and preparation.plan.authoritative_entry_number_count
        == LEGACY_ROOM_RESULT_IMPORT_MANIFEST.authoritative_entry_number_count
        and preparation.plan.synthetic_entry_number_count
        == LEGACY_ROOM_RESULT_IMPORT_MANIFEST.synthetic_entry_number_count
        and sum(row.is_rating_excluded for row in preparation.plan.rows)
        == LEGACY_ROOM_RESULT_IMPORT_MANIFEST.rating_excluded_count
    )
    prior_source_keys = {
        row.source_key for row in race_plan.rows[: LEGACY_ROOM_RESULT_PREVIOUS_IMPORT_MANIFEST.race_count]
    }
    expected_rows = {row.external_race_id: row for row in race_plan.rows}
    race_statement = (
        select(Race)
        .where(
            Race.external_source == preparation.plan.source_identifier,
            Race.external_race_id.in_(expected_rows),
        )
        .order_by(Race.id)
    )
    if lock_rows:
        race_statement = race_statement.with_for_update().execution_options(populate_existing=True)
    races = list(session.scalars(race_statement))
    races_by_external_id = {race.external_race_id: race for race in races if race.external_race_id is not None}
    if len(races_by_external_id) != len(expected_rows):
        raise LegacyImportError("completed legacy race import provenance is missing or changed")

    record_statement = (
        select(SheetImportRecord)
        .where(SheetImportRecord.source_key.in_(row.source_key for row in race_plan.rows))
        .order_by(SheetImportRecord.source_key)
    )
    if lock_rows:
        record_statement = record_statement.with_for_update().execution_options(populate_existing=True)
    records_by_source_key = {record.source_key: record for record in session.scalars(record_statement)}
    if len(records_by_source_key) != len(expected_rows):
        raise LegacyImportError("completed legacy race import provenance is missing or changed")

    import_run_ids = sorted({record.import_run_id for record in records_by_source_key.values()})
    run_statement = select(SheetImportRun).where(SheetImportRun.id.in_(import_run_ids)).order_by(SheetImportRun.id)
    if lock_rows:
        run_statement = run_statement.with_for_update().execution_options(populate_existing=True)
    runs_by_id = {import_run.id: import_run for import_run in session.scalars(run_statement)}
    if len(runs_by_id) != len(import_run_ids):
        raise LegacyImportError("completed legacy race import provenance is missing or changed")

    references: list[LegacyImportedRaceReference] = []
    for expected in race_plan.rows:
        race = races_by_external_id.get(expected.external_race_id)
        record = records_by_source_key.get(expected.source_key)
        import_run = runs_by_id.get(record.import_run_id) if record is not None else None
        if (
            race is None
            or record is None
            or import_run is None
            or not _has_completed_race_import_provenance(
                race=race,
                record=record,
                import_run=import_run,
                expected_source_key=expected.source_key,
                expected_row_fingerprint=expected.row_fingerprint,
                expected_sheet_name=race_plan.source_sheet_name,
                expected_row_number=expected.source_row_number,
                source_identifier=preparation.plan.source_identifier,
                source_checksum=preparation.source_checksum,
                allow_prior_source_checksum=(supports_append_extension and expected.source_key in prior_source_keys),
            )
        ):
            raise LegacyImportError("completed legacy race import provenance is missing or changed")
        references.append(
            LegacyImportedRaceReference(
                race_id=race.id,
                external_source=race.external_source,
                external_race_id=race.external_race_id,
                race_kind=race.race_kind,
            )
        )
    return tuple(references)


def _has_completed_race_import_provenance(
    *,
    race: Race,
    record: SheetImportRecord,
    import_run: SheetImportRun,
    expected_source_key: str,
    expected_row_fingerprint: str,
    expected_sheet_name: str,
    expected_row_number: int,
    source_identifier: str,
    source_checksum: str,
    allow_prior_source_checksum: bool = False,
) -> bool:
    return (
        record.source_key == expected_source_key
        and record.row_fingerprint == expected_row_fingerprint
        and record.source_sheet_name == expected_sheet_name
        and record.source_row_number == expected_row_number
        and record.record_type == LEGACY_RACE_RECORD_TYPE
        and record.status == "applied"
        and record.target_entity_type == "race"
        and record.target_entity_id == race.id
        and import_run.import_kind == LEGACY_RACE_IMPORT_KIND
        and import_run.source_type == "xlsx"
        and import_run.source_identifier == source_identifier
        and (
            import_run.source_checksum == source_checksum
            or (allow_prior_source_checksum and import_run.source_checksum == LEGACY_RESULT_PREVIOUS_SOURCE_CHECKSUM)
        )
        and import_run.status == "completed"
        and import_run.finished_at is not None
    )


def _build_summary(
    preparation: LegacyRoomResultWorkbookPreparation,
    preview: LegacyRoomResultImportPreview,
) -> dict[str, object]:
    return {
        "mode": "dry-run",
        "source_checksum": preparation.source_checksum,
        "source_row_count": preparation.data_row_count,
        "race_count": preview.race_count,
        "authoritative_entry_number_count": preview.authoritative_entry_number_count,
        "synthetic_entry_number_count": preview.synthetic_entry_number_count,
        "rating_excluded_count": preview.rating_excluded_count,
        "new_count": preview.new_count,
        "skipped_count": preview.skipped_count,
        "conflict_count": preview.conflict_count,
        "conflicts": [
            {"row_number": conflict.source_row_number, "code": conflict.code} for conflict in preview.conflicts[:20]
        ],
    }


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
