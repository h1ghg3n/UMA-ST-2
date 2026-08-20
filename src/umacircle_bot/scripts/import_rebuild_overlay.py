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
from umacircle_bot.services.rebuild_overlay_import import (
    RebuildOverlayImportError,
    RebuildOverlayRemapPlan,
    build_rebuild_overlay_remap_plan,
    public_rebuild_overlay_summary,
)
from umacircle_bot.services.rebuild_source_export import DEFAULT_EXPECTED_SOURCE_REVISION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and remap a protected rebuild overlay bundle without writing to the target database"
    )
    parser.add_argument("bundle_directory", type=Path, help="Protected format-v2 rebuild bundle directory")
    parser.add_argument(
        "--expect-manifest-sha256",
        required=True,
        help="Externally recorded SHA-256 of bundle_manifest.json",
    )
    parser.add_argument(
        "--expect-source-revision",
        default=DEFAULT_EXPECTED_SOURCE_REVISION,
        help="Fail unless every bundle payload has this exact source revision",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Required safety acknowledgement; apply is intentionally not implemented",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.dry_run:
        return _write_error("dry_run_required", "--dry-run is required; apply is not implemented")
    try:
        plan = _build_plan_from_database(
            bundle_directory=args.bundle_directory,
            expected_manifest_sha256=args.expect_manifest_sha256,
            expected_source_revision=args.expect_source_revision,
        )
        _write_stdout(public_rebuild_overlay_summary(plan))
        return 0 if plan.conflict_free_within_implemented_coverage else 2
    except ValidationError:
        return _write_error("configuration_validation_failed", "configuration validation failed")
    except RebuildOverlayImportError as exc:
        return _write_error(exc.code, str(exc))
    except SQLAlchemyError:
        return _write_error("database_dry_run_failed", "database read-only dry-run failed")
    except OSError:
        return _write_error("bundle_operation_failed", "rebuild bundle operation failed")


def _build_plan_from_database(
    *,
    bundle_directory: Path,
    expected_manifest_sha256: str,
    expected_source_revision: str,
) -> RebuildOverlayRemapPlan:
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        future=True,
        poolclass=NullPool,
    )
    try:
        if engine.dialect.name not in {"mysql", "mariadb"}:
            raise RebuildOverlayImportError("target_dialect_mismatch", "rebuild overlay dry-run requires MariaDB")
        with engine.connect() as connection:
            connection.exec_driver_sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            connection.commit()
            try:
                read_only = connection.execute(text("SELECT @@tx_read_only")).scalar_one()
                if int(read_only) != 1:
                    raise RebuildOverlayImportError(
                        "target_session_not_read_only",
                        "target database session is not read-only",
                    )
                return build_rebuild_overlay_remap_plan(
                    connection,
                    bundle_directory=bundle_directory,
                    expected_manifest_sha256=expected_manifest_sha256,
                    expected_source_revision=expected_source_revision,
                )
            finally:
                connection.rollback()
    finally:
        engine.dispose()


def _write_stdout(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _write_error(code: str, message: str) -> int:
    print(json.dumps({"code": code, "error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
