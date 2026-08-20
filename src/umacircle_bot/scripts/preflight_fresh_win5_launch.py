from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.services.fresh_win5_launch import build_fresh_win5_launch_preflight

IMAGE_SOURCE_COMMIT_PATH = Path("/app/.source-commit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a read-only fresh WIN5 first-Season launch manifest")
    parser.add_argument("--source-identifier", required=True, help="Stable Room Match XLSX source identifier")
    parser.add_argument("--source-checksum", required=True, help="Confirmed Room Match XLSX SHA-256")
    parser.add_argument("--bot-commit", required=True, help="Exact bot source/image commit")
    parser.add_argument(
        "--participant-discord-user-id",
        action="append",
        required=True,
        dest="participant_discord_user_ids",
        help="First-Season participant Discord user ID; repeat for every participant",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runtime_bot_commit = _read_image_source_commit()
        configure_session()
        with SessionLocal() as session:
            report = build_fresh_win5_launch_preflight(
                session,
                source_identifier=args.source_identifier,
                source_checksum=args.source_checksum,
                bot_commit=args.bot_commit,
                runtime_bot_commit=runtime_bot_commit,
                participant_discord_user_ids=tuple(args.participant_discord_user_ids),
            )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["ready_to_launch"] else 2
    except (LegacyImportError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("fresh WIN5 launch preflight failed")
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
