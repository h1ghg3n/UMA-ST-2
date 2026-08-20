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
from umacircle_bot.services.rating_rule_versions import (
    apply_rating_rule_seed,
    build_rating_rule_seed_plan,
    preview_rating_rule_seed,
)
from umacircle_bot.sheets.rating_rule_workbook import prepare_rating_rule_workbook


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run or seed an immutable workbook-derived RatingRule version")
    parser.add_argument("workbook", type=Path, help="Path to the room-match XLSX workbook")
    parser.add_argument("--source-identifier", required=True, help="Stable logical workbook source ID")
    parser.add_argument("--sheet-name", default="Rate 기준표", help="Rating rule worksheet name")
    parser.add_argument("--apply", action="store_true", help="Insert a new version after dry-run confirmation")
    parser.add_argument("--confirm-checksum", help="Required with --apply; inspected workbook SHA-256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        preparation = prepare_rating_rule_workbook(args.workbook, sheet_name=args.sheet_name)
        if args.apply:
            if args.confirm_checksum is None:
                return _write_error("--apply requires --confirm-checksum")
            if (
                normalize_sha256_hex(args.confirm_checksum, field_name="confirmed checksum")
                != preparation.source_checksum
            ):
                return _write_error("confirmed checksum does not match the inspected workbook")
        elif args.confirm_checksum is not None:
            return _write_error("--confirm-checksum is only valid with --apply")
        plan = build_rating_rule_seed_plan(preparation, source_identifier=args.source_identifier)
        configure_session()
        with SessionLocal() as session:
            preview = preview_rating_rule_seed(session, plan=plan)
            summary: dict[str, object] = {
                "mode": "dry-run",
                "source_checksum": plan.source_checksum,
                "source_sheet_name": plan.source_sheet_name,
                "source_range": plan.source_range,
                "rule_set_checksum": plan.rule_set_checksum,
                "rule_count": preview.rule_count,
                "existing_version_number": preview.existing_version_number,
                "next_version_number": preview.next_version_number,
            }
            if not args.apply:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return 0
            result = apply_rating_rule_seed(session, plan=plan)
            session.commit()
            summary.update(
                mode="apply",
                version_id=result.version_id,
                version_number=result.version_number,
                created=result.created,
            )
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return 0
    except (LegacyImportError, OSError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("database operation failed")
    finally:
        dispose_session_engine()


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
