from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.config import get_settings
from umacircle_bot.db.models import SheetImportRun
from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.imports import normalize_sha256_hex
from umacircle_bot.services.full_historical_ledger_authority import (
    inspect_full_historical_ledger_authority,
)
from umacircle_bot.services.historical_placement_correction import (
    HISTORICAL_PLACEMENT_CORRECTION_IMPORT_KIND,
    apply_historical_placement_correction,
    build_historical_placement_correction_plan,
)
from umacircle_bot.services.legacy_ledger_import import build_legacy_ledger_import_plan
from umacircle_bot.services.legacy_payout_correction import build_legacy_payout_correction_plan
from umacircle_bot.sheets.legacy_identity_point_workbook import prepare_legacy_identity_point_workbook
from umacircle_bot.sheets.legacy_room_report import build_legacy_room_dry_run_report

IMAGE_SOURCE_COMMIT_PATH = Path("/app/.source-commit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run or apply the reviewed Race 52-56 historical placement correction"
    )
    parser.add_argument("workbook", type=Path, help="Path to the frozen canonical Circle Match XLSX")
    parser.add_argument("--source-identifier", required=True, help="Stable logical source identifier")
    parser.add_argument("--bot-commit", required=True, help="Exact source commit baked into the runtime image")
    parser.add_argument(
        "--confirm-mapping-decision-checksum",
        required=True,
        help="Applied S2 identity mapping decision checksum",
    )
    parser.add_argument(
        "--confirm-e2-prefix-checksum",
        required=True,
        help="Reviewed E2 business-key prefix checksum",
    )
    parser.add_argument("--apply", action="store_true", help="Commit the reviewed correction manifest")
    parser.add_argument(
        "--confirm-checksum",
        help="Required with --apply; must equal the live E3 manifest checksum",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.apply and args.confirm_checksum is None:
            raise ValueError("--apply requires --confirm-checksum")
        if not args.apply and args.confirm_checksum is not None:
            raise ValueError("--confirm-checksum is only valid with --apply")
        confirmed_manifest_checksum = (
            normalize_sha256_hex(args.confirm_checksum, field_name="confirmed placement manifest checksum")
            if args.confirm_checksum is not None
            else None
        )
        confirmed_mapping_decision_checksum = normalize_sha256_hex(
            args.confirm_mapping_decision_checksum,
            field_name="confirmed mapping decision checksum",
        )
        confirmed_e2_prefix_checksum = normalize_sha256_hex(
            args.confirm_e2_prefix_checksum,
            field_name="confirmed E2 prefix checksum",
        )

        preparation = prepare_legacy_identity_point_workbook(
            args.workbook,
            source_identifier=args.source_identifier,
        )
        report = build_legacy_room_dry_run_report(args.workbook)
        if report.source_checksum != preparation.source_checksum:
            raise ValueError("workbook preparations produced different source checksums")
        settings = get_settings()
        ledger_plan = build_legacy_ledger_import_plan(
            preparation.ledger_rows,
            preparation.point_rows,
            preparation.race_rows,
            preparation.reconciliation,
            source_identifier=args.source_identifier,
            source_utc_offset_minutes=settings.app_utc_offset_minutes,
        )
        correction_plan = build_legacy_payout_correction_plan(
            report,
            source_identifier=args.source_identifier,
        )
        runtime_commit = _read_image_source_commit()

        configure_session()
        with SessionLocal() as session:
            existing_run_count = _existing_run_count(session, source_identifier=args.source_identifier)
            if not existing_run_count:
                alembic_head = str(session.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
                e2_report = inspect_full_historical_ledger_authority(
                    session,
                    ledger_plan=ledger_plan,
                    correction_plan=correction_plan,
                    source_checksum=preparation.source_checksum,
                    importer_commit=args.bot_commit,
                    runtime_commit=runtime_commit,
                    alembic_head=alembic_head,
                )
                _require_reviewed_e2_prefix(
                    e2_report,
                    confirmed_prefix_checksum=confirmed_e2_prefix_checksum,
                )
            elif existing_run_count != 1:
                raise LegacyImportError("historical placement correction audit is ambiguous")

            plan = build_historical_placement_correction_plan(
                session,
                source_identifier=args.source_identifier,
                source_checksum=preparation.source_checksum,
                confirmed_mapping_decision_checksum=confirmed_mapping_decision_checksum,
                e2_business_key_prefix_checksum=confirmed_e2_prefix_checksum,
            )
            payload: dict[str, object] = {
                "mode": "apply" if args.apply else "dry-run",
                "committed": bool(args.apply),
                **plan.manifest.as_dict(),
            }
            if not plan.manifest.ready:
                session.rollback()
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                return 2
            if args.apply:
                assert confirmed_manifest_checksum is not None
                result = apply_historical_placement_correction(
                    session,
                    source_identifier=args.source_identifier,
                    source_checksum=preparation.source_checksum,
                    confirmed_mapping_decision_checksum=confirmed_mapping_decision_checksum,
                    e2_business_key_prefix_checksum=confirmed_e2_prefix_checksum,
                    confirmed_manifest_checksum=confirmed_manifest_checksum,
                )
                payload["apply"] = result.as_dict()
                session.commit()
            else:
                session.rollback()
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
    except (LegacyImportError, OSError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("historical placement correction database operation failed")
    finally:
        dispose_session_engine()


def _existing_run_count(session, *, source_identifier: str) -> int:
    return len(
        tuple(
            session.scalars(
                select(SheetImportRun.id).where(
                    SheetImportRun.import_kind == HISTORICAL_PLACEMENT_CORRECTION_IMPORT_KIND,
                    SheetImportRun.source_identifier == source_identifier,
                )
            )
        )
    )


def _require_reviewed_e2_prefix(report, *, confirmed_prefix_checksum: str) -> None:
    if report.business_key_prefix_checksum != confirmed_prefix_checksum:
        raise LegacyImportError("confirmed E2 prefix checksum does not match the live prefix")
    errors = set(report.errors)
    if not errors:
        return
    if errors != {"wallet_count_mismatch"}:
        raise LegacyImportError(f"E2 prefix verification failed: {sorted(errors)[0]}")
    expected_wallet_count = int(report.expected["wallet_count"])
    actual_wallet_count = int(report.actual["wallet_count"])
    if actual_wallet_count < expected_wallet_count:
        raise LegacyImportError("E2 prefix wallet coverage is incomplete")


def _read_image_source_commit() -> str:
    try:
        value = IMAGE_SOURCE_COMMIT_PATH.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise ValueError("runtime image source commit file is unavailable") from exc
    if not value:
        raise ValueError("runtime image source commit file is empty")
    return value


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
