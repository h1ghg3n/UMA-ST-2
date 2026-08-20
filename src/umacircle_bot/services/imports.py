from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import SheetImportRecord, SheetImportRun
from umacircle_bot.domain.errors import (
    ImportApplicationError,
    ImportFingerprintConflictError,
    ImportRunStateError,
)
from umacircle_bot.domain.imports import build_sheet_import_source_key, normalize_sha256_hex


@dataclass(frozen=True)
class ImportTarget:
    entity_type: str
    entity_id: int
    detail: dict[str, Any] | None = None
    link_record: Callable[[SheetImportRecord], None] | None = None


@dataclass(frozen=True)
class ImportApplyResult:
    record: SheetImportRecord
    applied: bool


def apply_sheet_import_record(
    session: Session,
    *,
    import_run_id: int,
    source_identifier: str,
    sheet_name: str,
    row_number: int,
    row_fingerprint: str,
    record_type: str,
    apply_target: Callable[[], ImportTarget],
) -> ImportApplyResult:
    """Apply one source row and its audit record atomically.

    A global source key makes reruns idempotent across import runs. A repeated
    source location with different content is rejected instead of overwritten.
    The callback executes inside the same savepoint as the audit record.
    """

    normalized_run_id = _normalize_positive_id(import_run_id, field_name="import run ID")
    normalized_fingerprint = normalize_sha256_hex(row_fingerprint, field_name="row fingerprint")
    normalized_record_type = _normalize_printable_text(record_type, field_name="record type", max_length=64)
    source_key = build_sheet_import_source_key(
        source_identifier=source_identifier,
        sheet_name=sheet_name,
        row_number=row_number,
    )

    with session.begin_nested():
        import_run = session.scalar(
            select(SheetImportRun).where(SheetImportRun.id == normalized_run_id).with_for_update()
        )
        if import_run is None:
            raise ImportRunStateError("import run not found")
        if import_run.status != "running":
            raise ImportRunStateError("import run is not accepting rows")
        if import_run.source_identifier != source_identifier.strip():
            raise ImportRunStateError("source identifier does not match import run")

        existing = session.scalar(
            select(SheetImportRecord).where(SheetImportRecord.source_key == source_key).with_for_update()
        )
        if existing is not None:
            if existing.row_fingerprint != normalized_fingerprint:
                raise ImportFingerprintConflictError("source row content changed after a previous import")
            if existing.status != "applied":
                raise ImportApplicationError("source row has a non-applied import record")
            return ImportApplyResult(record=existing, applied=False)

        target = apply_target()
        normalized_entity_type = _normalize_printable_text(
            target.entity_type,
            field_name="target entity type",
            max_length=64,
        )
        normalized_entity_id = _normalize_positive_id(target.entity_id, field_name="target entity ID")
        record = SheetImportRecord(
            import_run_id=import_run.id,
            source_key=source_key,
            row_fingerprint=normalized_fingerprint,
            source_sheet_name=sheet_name.strip(),
            source_row_number=row_number,
            record_type=normalized_record_type,
            status="applied",
            target_entity_type=normalized_entity_type,
            target_entity_id=normalized_entity_id,
            detail_json=target.detail,
        )
        session.add(record)
        session.flush()
        if target.link_record is not None:
            target.link_record(record)
            session.flush()

    return ImportApplyResult(record=record, applied=True)


def _normalize_positive_id(value: int, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ImportApplicationError(f"{field_name} must be a positive integer")
    return value


def _normalize_printable_text(value: str, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ImportApplicationError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or any(ord(character) < 32 for character in normalized):
        raise ImportApplicationError(f"{field_name} must contain 1 to {max_length} printable characters")
    return normalized
