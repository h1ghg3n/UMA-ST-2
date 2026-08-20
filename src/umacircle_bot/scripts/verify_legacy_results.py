from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from umacircle_bot.config import get_settings
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.services.legacy_result_verification import (
    LegacyResultVerificationReport,
    verify_legacy_room_result_plan,
)
from umacircle_bot.sheets.legacy_identity_point_workbook import prepare_legacy_room_result_workbook

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify legacy room-match result calculations without database writes")
    parser.add_argument("workbook", type=Path, help="Path to the legacy room-match XLSX workbook")
    parser.add_argument("--source-identifier", required=True, help="Stable logical source ID for source provenance")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        preparation = prepare_legacy_room_result_workbook(
            args.workbook,
            source_identifier=args.source_identifier,
            source_utc_offset_minutes=get_settings().app_utc_offset_minutes,
        )
        report = verify_legacy_room_result_plan(preparation.plan)
        print(json.dumps(_summary(preparation.source_checksum, report), ensure_ascii=False, sort_keys=True))
        return 2 if report.has_calculation_issues else 0
    except LegacyImportError as exc:
        logger.info("legacy result verification rejected: %s", exc)
        return _write_error(str(exc))
    except OSError:
        logger.exception("legacy result workbook could not be accessed")
        return _write_error("workbook could not be accessed")
    except ValueError:
        logger.exception("legacy result verification request is invalid")
        return _write_error("invalid verification request")
    except Exception:
        logger.exception("unexpected legacy result verification failure")
        return _write_error("unexpected internal error")


def _summary(source_checksum: str, report: LegacyResultVerificationReport) -> dict[str, object]:
    return {
        "source_checksum": source_checksum,
        "observed_manifest": {
            "row_count": report.row_count,
            "race_count": report.race_count,
            "authoritative_entry_number_count": report.authoritative_entry_number_count,
            "synthetic_entry_number_count": report.synthetic_entry_number_count,
            "rating_excluded_count": report.rating_excluded_count,
        },
        "canonical_manifest": {
            "row_count": report.expected_row_count,
            "race_count": report.expected_race_count,
            "authoritative_entry_number_count": report.expected_authoritative_entry_number_count,
            "synthetic_entry_number_count": report.expected_synthetic_entry_number_count,
            "rating_excluded_count": report.expected_rating_excluded_count,
        },
        "matches_canonical_manifest": report.matches_canonical_manifest,
        "arithmetic_issue_count": len(report.arithmetic_issues),
        "continuity_issue_count": len(report.continuity_issues),
        "requires_operator_review": report.requires_operator_review,
    }


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
