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
from umacircle_bot.services.win5_first_baseline import (
    Win5FirstBaselinePlan,
    Win5FirstBaselinePreview,
    apply_win5_first_baseline,
    build_win5_first_baseline_plan,
    preview_win5_first_baseline,
)
from umacircle_bot.sheets.legacy_identity_point_workbook import prepare_legacy_identity_point_workbook
from umacircle_bot.sheets.legacy_room_report import build_legacy_room_dry_run_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run or apply the authoritative WIN5-first identity and Circle Point opening baseline"
    )
    parser.add_argument("workbook", type=Path, help="Path to the frozen Room Match XLSX workbook")
    parser.add_argument(
        "--source-identifier",
        required=True,
        help="Stable logical source ID; do not use an environment-specific absolute path",
    )
    parser.add_argument("--apply", action="store_true", help="Apply after every dry-run check passes")
    parser.add_argument(
        "--confirm-checksum",
        help="Required with --apply; must equal the inspected workbook SHA-256",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        preparation = prepare_legacy_identity_point_workbook(
            args.workbook,
            source_identifier=args.source_identifier,
        )
        report = build_legacy_room_dry_run_report(preparation.workbook_path)
        if report.source_checksum != preparation.source_checksum:
            return _write_error("Room Match reconciliation checksum does not match the inspected workbook")
        baseline_plan = build_win5_first_baseline_plan(preparation.plan, report)
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
            preview = preview_win5_first_baseline(
                session,
                identity_plan=preparation.plan,
                baseline_plan=baseline_plan,
                source_checksum=preparation.source_checksum,
            )
            summary = _build_summary(preparation.source_checksum, baseline_plan, preview)
            if not preview.can_apply:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return 2
            if not args.apply:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return 0

            result = apply_win5_first_baseline(
                session,
                identity_plan=preparation.plan,
                baseline_plan=baseline_plan,
                source_checksum=preparation.source_checksum,
            )
            session.commit()
            summary.update(
                {
                    "mode": "apply",
                    "identity_import_run_id": result.identity_import_run_id,
                    "baseline_import_run_id": result.baseline_import_run_id,
                    "identity_created_count": result.identity_created_count,
                    "identity_skipped_count": result.identity_skipped_count,
                    "baseline_created_count": result.baseline_created_count,
                    "baseline_skipped_count": result.baseline_skipped_count,
                }
            )
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return 0
    except (LegacyImportError, OSError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("WIN5-first baseline database operation failed")
    finally:
        dispose_session_engine()


def _build_summary(
    source_checksum: str,
    plan: Win5FirstBaselinePlan,
    preview: Win5FirstBaselinePreview,
) -> dict[str, object]:
    return {
        "mode": "dry-run",
        "source_checksum": source_checksum,
        "source_identity_count": len(plan.rows),
        "source_current_total": plan.source_current_total,
        "source_correction_total": plan.source_correction_total,
        "source_corrected_total": plan.source_corrected_total,
        "target_opening_total": plan.target_total,
        "baseline_checksum": plan.baseline_checksum,
        "identity_new_count": preview.identity_preview.new_count,
        "identity_skipped_count": preview.identity_preview.skipped_count,
        "baseline_new_count": preview.new_count,
        "baseline_skipped_count": preview.skipped_count,
        "conflicts": [
            {"row_number": conflict.source_row_number, "code": conflict.code}
            for conflict in (*preview.identity_preview.conflicts, *preview.conflicts)[:20]
        ],
    }


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
