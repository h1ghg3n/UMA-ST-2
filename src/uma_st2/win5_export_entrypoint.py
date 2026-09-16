"""Executable one-shot boundary for the V2 WIN5 Season export CLI."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from uma_st2.adapters.cli import (
    SavedWin5SeasonExport,
    Win5ExportCliError,
    Win5SeasonExportCliAdapter,
    Win5SeasonExportCliRequest,
    parse_win5_export_cli_request,
)
from uma_st2.application.exporting import Win5SeasonExportError
from uma_st2.compose import compose_win5_season_exports
from uma_st2.config import DatabaseSettings
from uma_st2.infrastructure.database import DatabaseRuntime

logger = logging.getLogger(__name__)


def run_win5_export(
    settings: DatabaseSettings,
    request: Win5SeasonExportCliRequest,
    *,
    output_directory: Path,
) -> SavedWin5SeasonExport:
    """Compose one Engine, deliver one artifact, and always dispose resources."""

    database_runtime = DatabaseRuntime.from_url(
        settings.database_url_value,
        pool_pre_ping=True,
    )
    try:
        adapter = Win5SeasonExportCliAdapter(
            exports=compose_win5_season_exports(database_runtime),
        )
        return adapter.execute(request, output_directory=output_directory)
    finally:
        database_runtime.dispose()


def _print_success(receipt: SavedWin5SeasonExport) -> None:
    print(f"WIN5 Season XLSX created: {receipt.path}")
    print(f"Rows: {receipt.artifact.row_count}")
    print(f"SHA-256: {receipt.artifact.sha256_hex}")


def main(argv: Sequence[str] | None = None) -> None:
    """Run the confirmed CLI surface with bounded operator-facing failures."""

    request = parse_win5_export_cli_request(argv)
    try:
        settings = DatabaseSettings()
        receipt = run_win5_export(
            settings,
            request,
            output_directory=Path.cwd(),
        )
    except Win5SeasonExportError:
        print(
            "WIN5 Season export was rejected: check the selected Season and stored data.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except Win5ExportCliError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        logger.critical(
            "WIN5 Season export failed error_type=%s",
            type(exc).__name__,
        )
        print("WIN5 Season export failed due to an internal error.", file=sys.stderr)
        raise SystemExit(1) from None

    _print_success(receipt)


if __name__ == "__main__":
    main()
