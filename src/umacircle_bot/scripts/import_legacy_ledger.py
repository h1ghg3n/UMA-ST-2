from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.config import get_settings
from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.imports import normalize_sha256_hex
from umacircle_bot.runtime_preflight import EXPECTED_ROOM_POINT_SCALE
from umacircle_bot.services.legacy_ledger_import import (
    LegacyLedgerImportPreview,
    apply_legacy_ledger_import,
    build_legacy_ledger_import_plan,
    preview_legacy_ledger_import,
)
from umacircle_bot.sheets.legacy_identity_point_workbook import (
    LegacyIdentityPointWorkbookPreparation,
    prepare_legacy_identity_point_workbook,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run or apply the legacy room betting ledger")
    parser.add_argument("workbook", type=Path, help="Path to the legacy room-match XLSX workbook")
    parser.add_argument(
        "--source-identifier",
        required=True,
        help="Stable logical source ID; must match the identity and race imports",
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
        preparation = prepare_legacy_identity_point_workbook(
            args.workbook,
            source_identifier=args.source_identifier,
        )
        if args.apply:
            if args.confirm_checksum is None:
                return _write_error("--apply requires --confirm-checksum")
            confirmed_checksum = normalize_sha256_hex(args.confirm_checksum, field_name="confirmed checksum")
            if confirmed_checksum != preparation.source_checksum:
                return _write_error("confirmed checksum does not match the inspected workbook")
        elif args.confirm_checksum is not None:
            return _write_error("--confirm-checksum is only valid with --apply")

        plan = build_legacy_ledger_import_plan(
            preparation.ledger_rows,
            preparation.point_rows,
            preparation.race_rows,
            preparation.reconciliation,
            source_identifier=args.source_identifier,
            source_utc_offset_minutes=get_settings().app_utc_offset_minutes,
        )
        configure_session()
        with SessionLocal() as session:
            preview = preview_legacy_ledger_import(
                session,
                plan=plan,
                source_checksum=preparation.source_checksum,
            )
            summary = _build_summary(preparation, preview)
            if not preview.can_apply:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return 2
            if not args.apply:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return 0

            result = apply_legacy_ledger_import(
                session,
                plan=plan,
                source_checksum=preparation.source_checksum,
            )
            session.commit()
            summary["mode"] = "apply"
            summary["import_run_id"] = result.import_run_id
            summary["created_count"] = result.created_count
            summary["skipped_count"] = result.skipped_count
            summary["created_bet_count"] = result.bet_count
            summary["created_transaction_count"] = result.transaction_count
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return 0
    except (LegacyImportError, OSError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("database operation failed")
    finally:
        dispose_session_engine()


def _build_summary(
    preparation: LegacyIdentityPointWorkbookPreparation,
    preview: LegacyLedgerImportPreview,
) -> dict[str, object]:
    return {
        "mode": "dry-run",
        "source_checksum": preparation.source_checksum,
        "source_record_count": preview.new_count + preview.skipped_count,
        "expected_bet_count": preview.expected_bet_count,
        "expected_transaction_count": preview.expected_transaction_count,
        "source_expected_balance_total": preview.source_expected_balance_total,
        "target_expected_balance_total": preview.target_expected_balance_total,
        "room_point_scale": EXPECTED_ROOM_POINT_SCALE,
        "payout_warning_count": preview.payout_warning_count,
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
