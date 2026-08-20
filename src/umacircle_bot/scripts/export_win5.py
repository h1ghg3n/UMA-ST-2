from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.config import get_settings
from umacircle_bot.db.session import dispose_session_engine
from umacircle_bot.domain.errors import DomainError
from umacircle_bot.services.win5_export import execute_win5_season_export


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export one canonical WIN5 season snapshot to XLSX")
    parser.add_argument("--season-id", required=True, type=int, help="Canonical WIN5 season ID")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Host or bind-mounted directory for the generated XLSX; defaults to EXPORT_DIR",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir or get_settings().export_dir
    try:
        result = execute_win5_season_export(
            season_id=args.season_id,
            output_dir=output_dir,
        )
    except (DomainError, OSError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("database operation failed")
    finally:
        dispose_session_engine()

    print(
        json.dumps(
            {
                "output_name": result.output_path.name,
                "season_id": result.season_id,
                "season_number": result.season_number,
                "season_name": result.season_name,
                "round_count": result.round_count,
                "submission_count": result.submission_count,
                "participant_count": result.participant_count,
                "hall_of_fame_count": result.hall_of_fame_count,
                "sha256_checksum": result.sha256_checksum,
                "export_run_id": result.export_run_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
