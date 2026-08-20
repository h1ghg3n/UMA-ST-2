from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.services.legacy_source_account_seed import build_legacy_source_account_seed_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a reviewed source-only GameAccount seed template")
    parser.add_argument("--source-identifier", required=True, help="Stable Circle Match XLSX source identifier")
    parser.add_argument("--source-checksum", required=True, help="Confirmed Circle Match XLSX SHA-256")
    parser.add_argument("--output", type=Path, help="Optional UTF-8 JSON output path; stdout is always emitted")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing output file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.overwrite and args.output is None:
        return _write_error("--overwrite requires --output")
    try:
        configure_session()
        with SessionLocal() as session:
            manifest = build_legacy_source_account_seed_manifest(
                session,
                source_identifier=args.source_identifier,
                source_checksum=args.source_checksum,
            )
        payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            output_path = args.output.expanduser().resolve()
            if output_path.exists() and not args.overwrite:
                return _write_error("output file already exists; pass --overwrite to replace it")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(payload, encoding="utf-8")
        print(payload, end="")
        return 0
    except (LegacyImportError, OSError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("legacy source-account seed manifest failed")
    finally:
        dispose_session_engine()


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
