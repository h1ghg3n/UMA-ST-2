from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.imports import normalize_sha256_hex
from umacircle_bot.runtime_preflight import EXPECTED_ROOM_POINT_SCALE
from umacircle_bot.services.legacy_payout_correction import (
    LegacyPayoutCorrectionPreview,
    apply_legacy_payout_correction,
    build_legacy_payout_correction_plan,
    preview_legacy_payout_correction,
)
from umacircle_bot.sheets.legacy_room_report import LegacyRoomDryRunReport, build_legacy_room_dry_run_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run or apply legacy room payout corrections")
    parser.add_argument("workbook", type=Path, help="Path to the legacy room-match XLSX workbook")
    parser.add_argument(
        "--source-identifier",
        required=True,
        help="Stable logical source ID; must match the completed ledger import",
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
        report = build_legacy_room_dry_run_report(args.workbook)
        if args.apply:
            if args.confirm_checksum is None:
                return _write_error("--apply requires --confirm-checksum")
            confirmed_checksum = normalize_sha256_hex(args.confirm_checksum, field_name="confirmed checksum")
            if confirmed_checksum != report.source_checksum:
                return _write_error("confirmed checksum does not match the inspected workbook")
        elif args.confirm_checksum is not None:
            return _write_error("--confirm-checksum is only valid with --apply")

        plan = build_legacy_payout_correction_plan(report, source_identifier=args.source_identifier)
        configure_session()
        with SessionLocal() as session:
            preview = preview_legacy_payout_correction(
                session,
                plan=plan,
                source_checksum=report.source_checksum,
            )
            summary = _build_summary(report, preview)
            if not preview.can_apply:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return 2
            if not args.apply:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return 0

            result = apply_legacy_payout_correction(
                session,
                plan=plan,
                source_checksum=report.source_checksum,
            )
            session.commit()
            summary["mode"] = "apply"
            summary["import_run_id"] = result.import_run_id
            summary["created_count"] = result.created_count
            summary["skipped_count"] = result.skipped_count
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return 0
    except (LegacyImportError, OSError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("database operation failed")
    finally:
        dispose_session_engine()


def _build_summary(
    report: LegacyRoomDryRunReport,
    preview: LegacyPayoutCorrectionPreview,
) -> dict[str, object]:
    return {
        "mode": "dry-run",
        "source_checksum": report.source_checksum,
        "payout_mismatch_count": len(report.payout_mismatches),
        "source_payout_correction_total": preview.source_correction_total,
        "target_payout_correction_total": preview.target_correction_total,
        "room_point_scale": EXPECTED_ROOM_POINT_SCALE,
        "new_count": preview.new_count,
        "skipped_count": preview.skipped_count,
        "conflicts": [
            {"row_number": conflict.source_row_number, "code": conflict.code} for conflict in preview.conflicts[:20]
        ],
    }


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
