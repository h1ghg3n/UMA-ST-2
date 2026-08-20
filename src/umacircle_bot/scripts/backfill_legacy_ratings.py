from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.domain.errors import LegacyImportError, MatchRatingError
from umacircle_bot.services.legacy_rating_backfill import backfill_imported_match_ratings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply one-shot legacy Room Match Rating backfill")
    parser.add_argument("--source-identifier", required=True, help="Stable Room Match XLSX source identifier")
    parser.add_argument("--confirm-source-checksum", required=True, help="Confirmed Room Match XLSX SHA-256")
    parser.add_argument(
        "--confirm-chain-disposition-checksum",
        required=True,
        help="Applied two-reviewer Rating-chain decision SHA-256",
    )
    parser.add_argument("--rating-rule-version-id", required=True, type=int, help="Approved RatingRuleVersion ID")
    parser.add_argument("--apply", action="store_true", help="Apply the atomic Rating-only backfill")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.apply:
        return _write_error("Rating backfill requires --apply and must first pass on a disposable rehearsal DB")
    try:
        configure_session()
        with SessionLocal() as session:
            result = backfill_imported_match_ratings(
                session,
                source_identifier=args.source_identifier,
                source_checksum=args.confirm_source_checksum,
                confirmed_disposition_checksum=args.confirm_chain_disposition_checksum,
                rating_rule_version_id=args.rating_rule_version_id,
            )
            session.commit()
        print(
            json.dumps(
                {
                    "mode": "apply",
                    "source_identifier": result.source_identifier,
                    "source_checksum": result.source_checksum,
                    "disposition_decision_checksum": result.disposition_decision_checksum,
                    "rating_rule_version_id": result.rating_rule_version_id,
                    "race_count": len(result.race_ids),
                    "rated_race_count": result.rated_race_count,
                    "excluded_race_count": result.excluded_race_count,
                    "event_count": result.event_count,
                    "history_context_only_result_count": result.history_context_only_result_count,
                    "recalculation_start_race_id": result.recalculation_start_race_id,
                    "recalculated_race_count": result.recalculated_race_count,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (LegacyImportError, MatchRatingError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("legacy Rating backfill failed")
    finally:
        dispose_session_engine()


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
