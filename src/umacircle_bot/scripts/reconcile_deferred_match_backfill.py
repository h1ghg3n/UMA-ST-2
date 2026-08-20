from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.db.models import Race
from umacircle_bot.domain.errors import DomainError
from umacircle_bot.services.deferred_match_backfill_reconciliation import (
    build_deferred_match_backfill_reconciliation,
    open_deferred_match_reconciliation_session,
)
from umacircle_bot.services.legacy_race_import import build_legacy_race_import_plan
from umacircle_bot.services.legacy_result_import import LegacyImportedRaceReference, map_legacy_room_result_plan
from umacircle_bot.sheets.current_win5_replay_manifest import load_current_win5_replay_manifest
from umacircle_bot.sheets.legacy_identity_point_workbook import prepare_legacy_room_result_workbook

IMAGE_SOURCE_COMMIT_PATH = Path("/app/.source-commit")
_consistent_read_session = open_deferred_match_reconciliation_session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Snapshot or verify the non-posting deferred Circle Match backfill boundary"
    )
    parser.add_argument("manifest", type=Path, help="Protected current WIN5 epoch replay manifest")
    parser.add_argument("--room-match-workbook", type=Path, help="Frozen canonical Circle Match XLSX")
    parser.add_argument("--source-utc-offset-minutes", type=int)
    parser.add_argument("--replay-bot-commit", required=True, help="Commit recorded by current WIN5 replay")
    parser.add_argument("--backfill-bot-commit", required=True, help="Exact commit baked into this backfill image")
    parser.add_argument("--expected-circle-point-signature")
    parser.add_argument("--expected-win5-signature")
    parser.add_argument("--confirm-mapping-decision-checksum")
    parser.add_argument("--confirm-chain-disposition-checksum")
    parser.add_argument("--rating-rule-version-id", type=int)
    parser.add_argument(
        "--confirm-writes-quiesced",
        action="store_true",
        help="Confirm bot/runtime writes remain stopped across the before/backfill/after evidence window",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_current_win5_replay_manifest(args.manifest)
        if not args.confirm_writes_quiesced:
            raise ValueError("deferred Match reconciliation requires --confirm-writes-quiesced")
        runtime_commit = _read_image_source_commit()
        with _consistent_read_session() as session:
            source_race_plan = None
            source_result_plan = None
            if args.room_match_workbook is not None:
                if args.source_utc_offset_minutes is None:
                    raise ValueError("complete reconciliation requires --source-utc-offset-minutes")
                preparation = prepare_legacy_room_result_workbook(
                    args.room_match_workbook,
                    source_identifier=manifest.room_match_source_identifier,
                    source_utc_offset_minutes=args.source_utc_offset_minutes,
                )
                if preparation.source_checksum != manifest.room_match_source_checksum:
                    raise ValueError("Circle Match workbook checksum does not match replay manifest")
                source_race_plan = build_legacy_race_import_plan(
                    preparation.race_rows,
                    source_identifier=manifest.room_match_source_identifier,
                    source_utc_offset_minutes=args.source_utc_offset_minutes,
                )
                imported_races = tuple(
                    LegacyImportedRaceReference(
                        race_id=race.id,
                        external_source=race.external_source,
                        external_race_id=race.external_race_id,
                        race_kind=race.race_kind,
                    )
                    for race in session.scalars(
                        select(Race).where(
                            Race.external_source == manifest.room_match_source_identifier,
                            Race.race_kind == "room_match",
                        )
                    )
                    if race.external_race_id is not None
                )
                source_result_plan = map_legacy_room_result_plan(preparation.plan, imported_races)
            result = build_deferred_match_backfill_reconciliation(
                session,
                manifest=manifest,
                replay_bot_commit=args.replay_bot_commit,
                backfill_bot_commit=args.backfill_bot_commit,
                runtime_backfill_bot_commit=runtime_commit,
                writes_quiesced=True,
                source_race_plan=source_race_plan,
                source_result_plan=source_result_plan,
                expected_circle_point_signature=args.expected_circle_point_signature,
                expected_win5_signature=args.expected_win5_signature,
                confirmed_mapping_decision_checksum=args.confirm_mapping_decision_checksum,
                confirmed_disposition_decision_checksum=args.confirm_chain_disposition_checksum,
                rating_rule_version_id=args.rating_rule_version_id,
            )
        print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
        return 0 if result.ready else 2
    except (DomainError, OSError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("deferred Circle Match backfill reconciliation database operation failed")


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
