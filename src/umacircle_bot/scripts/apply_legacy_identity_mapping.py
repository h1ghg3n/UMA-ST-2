from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.services.legacy_identity_mapping import (
    apply_legacy_identity_mapping,
    preview_legacy_identity_mapping,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or apply an approved legacy identity mapping manifest")
    parser.add_argument("manifest", type=Path, help="Two-reviewer JSON decision manifest")
    parser.add_argument("--apply", action="store_true", help="Atomically map imported Entry and Result owners")
    parser.add_argument("--confirm-source-checksum", help="Exact XLSX SHA-256 printed by preview")
    parser.add_argument("--confirm-decision-checksum", help="Exact approved decision SHA-256 printed by preview")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and (args.confirm_source_checksum is None or args.confirm_decision_checksum is None):
        return _write_error("--apply requires --confirm-source-checksum and --confirm-decision-checksum")
    try:
        decision_manifest = _load_manifest(args.manifest)
        configure_session()
        with SessionLocal() as session:
            if args.apply:
                result = apply_legacy_identity_mapping(
                    session,
                    decision_manifest=decision_manifest,
                    confirmed_source_checksum=args.confirm_source_checksum,
                    confirmed_decision_checksum=args.confirm_decision_checksum,
                )
                session.commit()
                payload = {
                    "mode": "apply",
                    "import_run_id": result.import_run_id,
                    "source_identifier": result.source_identifier,
                    "source_checksum": result.source_checksum,
                    "mapping_checksum": result.mapping_checksum,
                    "decision_checksum": result.decision_checksum,
                    "mapping_count": result.mapping_count,
                    "mapped_occurrence_count": result.mapped_occurrence_count,
                    "skipped_occurrence_count": result.skipped_occurrence_count,
                    "history_context_only_occurrence_count": result.history_context_only_occurrence_count,
                    "unresolved_history_occurrence_count": result.unresolved_history_occurrence_count,
                }
            else:
                preview = preview_legacy_identity_mapping(session, decision_manifest=decision_manifest)
                payload = {
                    "mode": "preview",
                    "source_identifier": preview.source_identifier,
                    "source_checksum": preview.source_checksum,
                    "mapping_checksum": preview.mapping_checksum,
                    "decision_checksum": preview.decision_checksum,
                    "mapping_count": preview.mapping_count,
                    "occurrence_count": preview.occurrence_count,
                    "rating_replay_count": preview.rating_replay_count,
                    "history_context_only_count": preview.history_context_only_count,
                    "unresolved_history_count": preview.unresolved_history_count,
                    "already_applied": preview.already_applied,
                    "can_apply": True,
                }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except json.JSONDecodeError:
        return _write_error("legacy identity mapping manifest is not valid JSON")
    except (LegacyImportError, OSError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("legacy identity mapping command failed")
    finally:
        dispose_session_engine()


def _load_manifest(path: Path) -> Mapping[str, object]:
    payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise LegacyImportError("legacy identity mapping manifest must be a JSON object")
    return payload


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
