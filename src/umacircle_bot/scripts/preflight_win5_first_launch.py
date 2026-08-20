from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.domain.errors import DomainError
from umacircle_bot.services.mariadb_consistent_read import open_mariadb_consistent_read_session
from umacircle_bot.services.win5_first_preflight import build_win5_first_launch_preflight
from umacircle_bot.sheets.current_win5_replay_manifest import load_current_win5_replay_manifest

IMAGE_SOURCE_COMMIT_PATH = Path("/app/.source-commit")
_consistent_read_session = open_mariadb_consistent_read_session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the read-only WIN5-first pre-epoch launch manifest")
    parser.add_argument("--replay-manifest", required=True, type=Path, help="Protected current WIN5 replay manifest")
    parser.add_argument("--bot-commit", required=True, help="Exact bot source/image commit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_current_win5_replay_manifest(args.replay_manifest)
        runtime_bot_commit = _read_image_source_commit()
        with _consistent_read_session() as session:
            report = build_win5_first_launch_preflight(
                session,
                manifest=manifest,
                bot_commit=args.bot_commit,
                runtime_bot_commit=runtime_bot_commit,
            )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["ready_to_launch"] else 2
    except (DomainError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("WIN5-first launch preflight failed")


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
