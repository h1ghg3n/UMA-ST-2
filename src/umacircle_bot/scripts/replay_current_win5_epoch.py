from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.domain.errors import DomainError
from umacircle_bot.domain.imports import normalize_sha256_hex
from umacircle_bot.services.current_win5_replay import (
    CurrentWin5ReplayPreview,
    apply_current_win5_epoch_replay,
    preview_current_win5_epoch_replay,
)
from umacircle_bot.services.mariadb_consistent_read import open_mariadb_consistent_read_session
from umacircle_bot.sheets.current_win5_replay_manifest import (
    CurrentWin5ReplayManifest,
    load_current_win5_replay_manifest,
)

IMAGE_SOURCE_COMMIT_PATH = Path("/app/.source-commit")
_consistent_read_session = open_mariadb_consistent_read_session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run or apply the reviewed three-participant current WIN5 epoch replay"
    )
    parser.add_argument("manifest", type=Path, help="Path to the protected reviewed current-epoch manifest")
    parser.add_argument("--bot-commit", required=True, help="Exact bot source/image commit")
    parser.add_argument("--apply", action="store_true", help="Apply through native WIN5 services")
    parser.add_argument(
        "--confirm-manifest-checksum",
        help="Required with --apply; must equal the reviewed semantic manifest checksum",
    )
    parser.add_argument(
        "--confirm-file-checksum",
        help="Required with --apply; must equal the protected manifest file SHA-256",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_current_win5_replay_manifest(args.manifest)
        _validate_confirmation_args(args, manifest=manifest)
        runtime_bot_commit = _read_image_source_commit()
        if args.apply:
            configure_session()
            session_context = SessionLocal()
        else:
            session_context = _consistent_read_session()
        with session_context as session:
            preview = preview_current_win5_epoch_replay(
                session,
                manifest=manifest,
                bot_commit=args.bot_commit,
                runtime_bot_commit=runtime_bot_commit,
            )
            summary = _preview_summary(manifest, preview)
            if not preview.can_apply:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return 2
            if not args.apply:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return 0

            result = apply_current_win5_epoch_replay(
                session,
                manifest=manifest,
                bot_commit=args.bot_commit,
                runtime_bot_commit=runtime_bot_commit,
            )
            session.commit()
            summary.update(
                {
                    "mode": result.mode,
                    "replay_import_run_id": result.replay_import_run_id,
                    "season_id": result.season_id,
                    "round_id": result.round_id,
                    "final_report": result.final_report,
                }
            )
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return 0
    except (DomainError, OSError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("current WIN5 epoch replay database operation failed")
    finally:
        dispose_session_engine()


def _validate_confirmation_args(args: argparse.Namespace, *, manifest: CurrentWin5ReplayManifest) -> None:
    if not args.apply:
        if args.confirm_manifest_checksum is not None or args.confirm_file_checksum is not None:
            raise ValueError("checksum confirmations are only valid with --apply")
        return
    if args.confirm_manifest_checksum is None or args.confirm_file_checksum is None:
        raise ValueError("--apply requires both manifest and file checksum confirmations")
    confirmed_manifest = normalize_sha256_hex(
        args.confirm_manifest_checksum,
        field_name="confirmed manifest checksum",
    )
    confirmed_file = normalize_sha256_hex(args.confirm_file_checksum, field_name="confirmed file checksum")
    if confirmed_manifest != manifest.manifest_checksum:
        raise ValueError("confirmed manifest checksum does not match the reviewed content")
    if confirmed_file != manifest.file_checksum:
        raise ValueError("confirmed file checksum does not match the protected manifest file")


def _preview_summary(
    manifest: CurrentWin5ReplayManifest,
    preview: CurrentWin5ReplayPreview,
) -> dict[str, object]:
    preflight = preview.preflight if isinstance(preview.preflight, dict) else {}
    baseline = preflight.get("baseline") if isinstance(preflight.get("baseline"), dict) else {}
    room_points = preflight.get("room_points") if isinstance(preflight.get("room_points"), dict) else {}
    return {
        "mode": preview.mode,
        "can_apply": preview.can_apply,
        "manifest_checksum": manifest.manifest_checksum,
        "file_checksum": manifest.file_checksum,
        "room_match_source_identifier": manifest.room_match_source_identifier,
        "room_match_source_checksum": manifest.room_match_source_checksum,
        "participant_count": len(manifest.participants),
        "participant_checksum": preview.participant_checksum,
        "expected_pre_replay_point_total": manifest.expected_pre_replay_point_total,
        "expected_final_point_total": manifest.target_point_totals.current,
        "expected_win5_reward_total": manifest.target_point_totals.win5_reward,
        "expected_season_score": manifest.expected_season_score,
        "expected_top1_score": manifest.expected_top1_score,
        "baseline_import_run_id": baseline.get("baseline_import_run_id"),
        "preflight_wallet_total": room_points.get("wallet_total"),
        "preflight_transaction_total": room_points.get("transaction_total"),
        "final_report": preview.final_report,
        "errors": tuple(_redact_error(error) for error in preview.errors),
    }


def _redact_error(error: str) -> str:
    if error.startswith("participant:"):
        return "participant_state_error:" + error.rsplit(":", maxsplit=1)[-1]
    return error


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
