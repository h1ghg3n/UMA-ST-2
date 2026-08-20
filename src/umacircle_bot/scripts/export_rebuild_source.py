from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from umacircle_bot.config import get_settings
from umacircle_bot.services.rebuild_source_export import (
    DEFAULT_EXPECTED_SOURCE_REVISION,
    SUPPORTED_SOURCE_REVISIONS,
    RebuildSourceExportError,
    RebuildSourceFile,
    RebuildSourceSnapshot,
    collect_rebuild_source_snapshot,
    public_rebuild_source_summary,
    write_rebuild_source_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export read-only source and operational overlays for a clean canonical rebuild"
    )
    parser.add_argument(
        "--expect-revision",
        choices=sorted(SUPPORTED_SOURCE_REVISIONS),
        default=DEFAULT_EXPECTED_SOURCE_REVISION,
        help="fail unless the source database has this exact Alembic revision",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="parent directory for a new protected bundle; defaults to BACKUP_DIR/rebuild-sources",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="collect and print only a PII-free reconciliation summary without writing files",
    )
    parser.add_argument(
        "--room-workbook",
        type=Path,
        help="Room Match source workbook; required for bundle export when completed workbook provenance exists",
    )
    parser.add_argument("--room-source-id", help="stable source identifier for --room-workbook")
    parser.add_argument(
        "--win5-workbook",
        type=Path,
        help="WIN5 source workbook; required for bundle export when completed workbook provenance exists",
    )
    parser.add_argument("--win5-source-id", help="stable source identifier for --win5-workbook")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source_files = _source_files(args)
        if args.validate_only and args.output_dir is not None:
            raise ValueError("--output-dir cannot be used with --validate-only")
        snapshot = _collect_snapshot_from_database(
            expected_revision=args.expect_revision,
            source_files=source_files,
        )
        if args.validate_only:
            _write_stdout({"mode": "validate-only", **public_rebuild_source_summary(snapshot)})
            return 0

        settings = get_settings()
        output_parent = args.output_dir or settings.backup_dir / "rebuild-sources"
        result = write_rebuild_source_bundle(snapshot, output_parent=output_parent)
        _write_stdout(
            {
                "mode": "export",
                "source_revision": result.source_revision,
                "output_name": result.output_directory.name,
                "manifest_sha256": result.manifest_sha256,
                "artifact_sha256": result.artifact_sha256,
            }
        )
        return 0
    except ValidationError:
        return _write_error("configuration validation failed")
    except (RebuildSourceExportError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("database read-only export failed")
    except OSError:
        return _write_error("rebuild artifact operation failed")


def _collect_snapshot_from_database(
    *,
    expected_revision: str,
    source_files: Sequence[RebuildSourceFile],
) -> RebuildSourceSnapshot:
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        future=True,
        poolclass=NullPool,
    )
    try:
        if engine.dialect.name not in {"mysql", "mariadb"}:
            raise RebuildSourceExportError("rebuild source export requires MariaDB")
        with engine.connect() as connection:
            connection.exec_driver_sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            connection.commit()
            try:
                read_only = connection.execute(text("SELECT @@tx_read_only")).scalar_one()
                if int(read_only) != 1:
                    raise RebuildSourceExportError("database session is not read-only")
                return collect_rebuild_source_snapshot(
                    connection,
                    expected_revision=expected_revision,
                    source_files=source_files,
                )
            finally:
                connection.rollback()
    finally:
        engine.dispose()


def _source_files(args: argparse.Namespace) -> tuple[RebuildSourceFile, ...]:
    pairs = (
        ("room_match", args.room_workbook, args.room_source_id, "room"),
        ("win5", args.win5_workbook, args.win5_source_id, "win5"),
    )
    sources: list[RebuildSourceFile] = []
    for kind, path, source_identifier, option_name in pairs:
        if (path is None) != (source_identifier is None):
            raise ValueError(f"--{option_name}-workbook and --{option_name}-source-id must be used together")
        if path is not None and source_identifier is not None:
            sources.append(
                RebuildSourceFile(
                    kind=kind,
                    source_identifier=source_identifier,
                    path=path,
                )
            )
    return tuple(sources)


def _write_stdout(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
