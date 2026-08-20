from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.config import get_settings
from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.imports import normalize_sha256_hex
from umacircle_bot.services.full_historical_ledger_authority import (
    apply_full_historical_ledger_authority,
)
from umacircle_bot.services.legacy_ledger_import import build_legacy_ledger_import_plan
from umacircle_bot.services.legacy_payout_correction import build_legacy_payout_correction_plan
from umacircle_bot.sheets.legacy_identity_point_workbook import prepare_legacy_identity_point_workbook
from umacircle_bot.sheets.legacy_room_report import build_legacy_room_dry_run_report

IMAGE_SOURCE_COMMIT_PATH = Path("/app/.source-commit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run or apply the canonical Race 1-51 full historical Circle Point prefix"
    )
    parser.add_argument("workbook", type=Path, help="Path to the frozen canonical Circle Match XLSX")
    parser.add_argument("--source-identifier", required=True, help="Stable logical source identifier")
    parser.add_argument("--bot-commit", required=True, help="Exact source commit baked into the runtime image")
    parser.add_argument("--apply", action="store_true", help="Commit after the complete E2 authority gate passes")
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
        report = build_legacy_room_dry_run_report(args.workbook)
        if report.source_checksum != preparation.source_checksum:
            raise ValueError("workbook preparations produced different source checksums")
        if args.apply:
            if args.confirm_checksum is None:
                raise ValueError("--apply requires --confirm-checksum")
            confirmed_checksum = normalize_sha256_hex(args.confirm_checksum, field_name="confirmed checksum")
            if confirmed_checksum != preparation.source_checksum:
                raise ValueError("confirmed checksum does not match the inspected workbook")
        elif args.confirm_checksum is not None:
            raise ValueError("--confirm-checksum is only valid with --apply")

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
            alembic_head = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            authority = apply_full_historical_ledger_authority(
                session,
                ledger_plan=ledger_plan,
                correction_plan=correction_plan,
                source_checksum=preparation.source_checksum,
                importer_commit=args.bot_commit,
                runtime_commit=runtime_commit,
                alembic_head=str(alembic_head),
            )
            payload = {
                "mode": "apply" if args.apply else "dry-run",
                "committed": bool(args.apply),
                **authority.as_dict(),
            }
            rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if args.apply:
                session.commit()
            else:
                session.rollback()
            print(rendered)
        return 0
    except (LegacyImportError, OSError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("full historical ledger authority database operation failed")
    finally:
        dispose_session_engine()


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
