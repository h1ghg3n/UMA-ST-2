from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from umacircle_bot.domain.betting import MAX_POINT_AMOUNT, validate_circle_point_balance
from umacircle_bot.domain.errors import BettingRuleError, LegacyImportError
from umacircle_bot.runtime_preflight import EXPECTED_ALEMBIC_HEAD, EXPECTED_ROOM_POINT_SCALE

MIN_SIGNED_BIGINT = -(MAX_POINT_AMOUNT + 1)
LEGACY_ROOM_POINT_AUDIT_CONTRACT_V1 = 1
LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION = 2
INVALID_LEGACY_ROOM_POINT_AUDIT_CONTRACT = 0


def classify_legacy_room_point_audit_contract(summary_json: object) -> int:
    """Classify a historical v1 run or a complete current v2 run contract."""

    summary = summary_json if isinstance(summary_json, dict) else {}
    version = summary.get("audit_contract_version")
    if version is None:
        return LEGACY_ROOM_POINT_AUDIT_CONTRACT_V1
    if (
        version == LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION
        and summary.get("room_point_scale") == EXPECTED_ROOM_POINT_SCALE
    ):
        return LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION
    return INVALID_LEGACY_ROOM_POINT_AUDIT_CONTRACT


def require_current_legacy_room_point_import_target(session: Session) -> None:
    """Reject monetary imports unless a deployed DB declares the current unit contract."""
    if session.get_bind().dialect.name == "sqlite":
        # SQLite is used only for isolated service/CLI tests; MariaDB is the supported deployment target.
        return
    try:
        revisions = tuple(session.scalars(text("SELECT version_num FROM alembic_version")))
        scale_rows = tuple(
            session.execute(text("SELECT id, scale_version FROM room_point_scale_state ORDER BY id")).tuples()
        )
    except SQLAlchemyError as exc:
        raise LegacyImportError("legacy Circle Point import target schema contract is unavailable") from exc
    if revisions != (EXPECTED_ALEMBIC_HEAD,):
        found = ", ".join(str(revision) for revision in revisions) or "none"
        raise LegacyImportError(
            f"legacy Circle Point import requires Alembic revision {EXPECTED_ALEMBIC_HEAD}; found {found}"
        )
    if scale_rows != ((1, EXPECTED_ROOM_POINT_SCALE),):
        raise LegacyImportError(
            f"legacy Circle Point import requires room_point_scale_state={EXPECTED_ROOM_POINT_SCALE}"
        )


def scale_legacy_room_point_amount(value: int, *, field_name: str) -> int:
    """Convert one legacy Circle Point amount to the current persisted unit."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise LegacyImportError(f"{field_name} must be an integer")
    scaled = value * EXPECTED_ROOM_POINT_SCALE
    if not MIN_SIGNED_BIGINT <= scaled <= MAX_POINT_AMOUNT:
        raise LegacyImportError(f"{field_name} exceeds the current Circle Point range after scaling")
    return scaled


def scale_legacy_circle_point_balance(value: int, *, field_name: str) -> int:
    scaled = scale_legacy_room_point_amount(value, field_name=field_name)
    try:
        validate_circle_point_balance(scaled, field_name=field_name)
    except BettingRuleError as exc:
        raise LegacyImportError(
            f"{field_name} is outside the current Circle Point balance range after scaling"
        ) from exc
    return scaled
