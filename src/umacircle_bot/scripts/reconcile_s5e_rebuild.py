from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.domain.errors import DomainError
from umacircle_bot.domain.imports import normalize_sha256_hex
from umacircle_bot.services.mariadb_consistent_read import open_mariadb_consistent_read_session
from umacircle_bot.services.s5e_rebuild_reconciliation import (
    apply_s5e_rebuild_current_win5,
    preview_s5e_rebuild,
)
from umacircle_bot.sheets.current_win5_replay_manifest import load_current_win5_graph_manifest
from umacircle_bot.sheets.s5e_rebuild_manifest import load_s5e_rebuild_manifest

IMAGE_SOURCE_COMMIT_PATH = Path("/app/.source-commit")
_consistent_read_session = open_mariadb_consistent_read_session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile the reviewed S5E baseline-first rebuild and apply bounded current WIN5"
    )
    parser.add_argument("manifest", type=Path, help="Protected S5E rebuild envelope")
    parser.add_argument(
        "--win5-manifest",
        required=True,
        type=Path,
        help="Protected bounded current WIN5 graph manifest referenced by the envelope",
    )
    parser.add_argument(
        "--e2-artifact",
        required=True,
        type=Path,
        help="Reviewed E2 authority JSON artifact referenced by the envelope",
    )
    parser.add_argument("--bot-commit", required=True, help="Exact bot source/image commit")
    parser.add_argument("--apply", action="store_true", help="Apply Phase D and persist reconciliation provenance")
    parser.add_argument(
        "--confirm-manifest-checksum",
        help="Required with --apply; must equal the reviewed S5E semantic checksum",
    )
    parser.add_argument(
        "--confirm-file-checksum",
        help="Required with --apply; must equal the protected S5E file SHA-256",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_s5e_rebuild_manifest(args.manifest)
        win5_manifest = load_current_win5_graph_manifest(args.win5_manifest)
        _validate_confirmation_args(
            args, manifest_checksum=manifest.manifest_checksum, file_checksum=manifest.file_checksum
        )
        e2_artifact_file_checksum = _file_checksum(args.e2_artifact, label="E2 authority artifact")
        runtime_bot_commit = _read_image_source_commit()

        if args.apply:
            configure_session()
            session_context = SessionLocal()
        else:
            session_context = _consistent_read_session()
        with session_context as session:
            preview = preview_s5e_rebuild(
                session,
                manifest=manifest,
                win5_manifest=win5_manifest,
                bot_commit=args.bot_commit,
                runtime_bot_commit=runtime_bot_commit,
                e2_artifact_file_checksum=e2_artifact_file_checksum,
            )
            payload: dict[str, object] = {
                "mode": "apply" if args.apply else "dry-run",
                "committed": False,
                "manifest_checksum": manifest.manifest_checksum,
                "file_checksum": manifest.file_checksum,
                "preview": preview.as_dict(),
            }
            if not preview.can_apply:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                return 2
            if not args.apply:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                return 0

            result = apply_s5e_rebuild_current_win5(
                session,
                manifest=manifest,
                win5_manifest=win5_manifest,
                bot_commit=args.bot_commit,
                runtime_bot_commit=runtime_bot_commit,
                e2_artifact_file_checksum=e2_artifact_file_checksum,
            )
            session.commit()
            payload.update({"committed": True, "apply": result.as_dict()})
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
    except (DomainError, OSError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("S5E rebuild reconciliation database operation failed")
    finally:
        dispose_session_engine()


def _validate_confirmation_args(
    args: argparse.Namespace,
    *,
    manifest_checksum: str,
    file_checksum: str,
) -> None:
    if not args.apply:
        if args.confirm_manifest_checksum is not None or args.confirm_file_checksum is not None:
            raise ValueError("checksum confirmations are only valid with --apply")
        return
    if args.confirm_manifest_checksum is None or args.confirm_file_checksum is None:
        raise ValueError("--apply requires both S5E manifest and file checksum confirmations")
    confirmed_manifest = normalize_sha256_hex(
        args.confirm_manifest_checksum,
        field_name="confirmed S5E manifest checksum",
    )
    confirmed_file = normalize_sha256_hex(
        args.confirm_file_checksum,
        field_name="confirmed S5E file checksum",
    )
    if confirmed_manifest != manifest_checksum:
        raise ValueError("confirmed manifest checksum does not match the reviewed S5E content")
    if confirmed_file != file_checksum:
        raise ValueError("confirmed file checksum does not match the protected S5E manifest file")


def _file_checksum(path: Path, *, label: str) -> str:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"{label} cannot be read") from exc


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
