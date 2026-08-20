from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.services.legacy_source_account_seed import (
    apply_legacy_source_account_seed,
    preview_legacy_source_account_seed,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or apply a reviewed source-only GameAccount seed")
    parser.add_argument("manifest", type=Path, help="Two-reviewer JSON decision manifest")
    parser.add_argument("--apply", action="store_true", help="Create reviewed pending source-only GameAccounts")
    parser.add_argument("--confirm-source-checksum", help="Exact XLSX SHA-256 printed by preview")
    parser.add_argument("--confirm-decision-checksum", help="Exact approved decision SHA-256 printed by preview")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and (args.confirm_source_checksum is None or args.confirm_decision_checksum is None):
        return _write_error("--apply requires --confirm-source-checksum and --confirm-decision-checksum")
    try:
        manifest = _load_manifest(args.manifest)
        configure_session()
        with SessionLocal() as session:
            if args.apply:
                result = apply_legacy_source_account_seed(
                    session,
                    decision_manifest=manifest,
                    confirmed_source_checksum=args.confirm_source_checksum,
                    confirmed_decision_checksum=args.confirm_decision_checksum,
                )
                session.commit()
                payload = {
                    "mode": "exact_retry" if result.exact_retry else "apply",
                    "import_run_id": result.import_run_id,
                    "source_identifier": result.source_identifier,
                    "source_checksum": result.source_checksum,
                    "seed_basis_checksum": result.seed_basis_checksum,
                    "decision_checksum": result.decision_checksum,
                    "decision_count": result.decision_count,
                    "created_count": result.created_count,
                    "no_seed_count": result.no_seed_count,
                }
            else:
                preview = preview_legacy_source_account_seed(session, decision_manifest=manifest)
                payload = {
                    "mode": "preview",
                    "source_identifier": preview.source_identifier,
                    "source_checksum": preview.source_checksum,
                    "seed_basis_checksum": preview.seed_basis_checksum,
                    "decision_checksum": preview.decision_checksum,
                    "decision_count": preview.decision_count,
                    "create_count": preview.create_count,
                    "no_seed_count": preview.no_seed_count,
                    "already_applied": preview.already_applied,
                    "can_apply": True,
                }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except json.JSONDecodeError:
        return _write_error("legacy source-account seed manifest is not valid JSON")
    except (LegacyImportError, OSError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("legacy source-account seed command failed")
    finally:
        dispose_session_engine()


def _load_manifest(path: Path) -> Mapping[str, object]:
    payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise LegacyImportError("legacy source-account seed manifest must be a JSON object")
    return payload


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
