from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.config import get_settings
from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.services.circle_point_export import export_circle_point_snapshot, remove_circle_point_export_artifact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export the current Circle Point snapshot to XLSX")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Host or bind-mounted directory for the generated XLSX; defaults to EXPORT_DIR",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir or get_settings().export_dir
    result = None
    try:
        configure_session()
        with SessionLocal() as session:
            result = export_circle_point_snapshot(session, output_dir=output_dir)
            session.commit()
        print(
            json.dumps(
                {
                    "output_name": result.output_path.name,
                    "row_count": result.row_count,
                    "sha256_checksum": result.sha256_checksum,
                    "export_run_id": result.export_run_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError) as exc:
        if result is not None:
            remove_circle_point_export_artifact(result)
        return _write_error(str(exc))
    except SQLAlchemyError:
        if result is not None:
            remove_circle_point_export_artifact(result)
        return _write_error("database operation failed")
    finally:
        dispose_session_engine()


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
