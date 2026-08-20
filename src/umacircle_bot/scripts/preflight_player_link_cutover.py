from __future__ import annotations

import json
import sys
from collections.abc import Sequence

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    CirclePointAccount,
    GameAccount,
    IdentityBackfillTask,
    PlayerLinkRequest,
    SheetImportRun,
)
from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.services.legacy_import import LEGACY_IDENTITY_POINT_IMPORT_KIND
from umacircle_bot.services.legacy_ledger_import import LEGACY_LEDGER_IMPORT_KIND

EXPECTED_ALEMBIC_REVISION = "20260802_0021"
_CANDIDATE_STATUSES = ("pending_identity", "identity_conflict")
_ACTIVE_REQUEST_STATUSES = ("pending", "review_required")


def build_cutover_preflight(session: Session) -> dict[str, object]:
    """Return a read-only readiness report for enabling player-link commands."""
    current_revision = session.scalar(text("SELECT version_num FROM alembic_version"))
    completed_identity_import_count = _completed_import_count(session, LEGACY_IDENTITY_POINT_IMPORT_KIND)
    completed_ledger_import_count = _completed_import_count(session, LEGACY_LEDGER_IMPORT_KIND)

    candidate_count = session.scalar(
        select(func.count()).select_from(GameAccount).where(GameAccount.identity_status.in_(_CANDIDATE_STATUSES))
    )
    candidate_missing_task_count = session.scalar(
        select(func.count())
        .select_from(GameAccount)
        .outerjoin(IdentityBackfillTask, IdentityBackfillTask.game_account_id == GameAccount.id)
        .where(
            GameAccount.identity_status.in_(_CANDIDATE_STATUSES),
            (
                IdentityBackfillTask.id.is_(None)
                | ~IdentityBackfillTask.status.in_(("pending", "conflict"))
                | IdentityBackfillTask.source_import_record_id.is_(None)
            ),
        )
    )
    candidate_missing_point_account_count = session.scalar(
        select(func.count())
        .select_from(GameAccount)
        .outerjoin(CirclePointAccount, CirclePointAccount.persona_id == GameAccount.persona_id)
        .where(
            GameAccount.identity_status.in_(_CANDIDATE_STATUSES),
            CirclePointAccount.id.is_(None),
        )
    )
    active_request_count = session.scalar(
        select(func.count())
        .select_from(PlayerLinkRequest)
        .where(PlayerLinkRequest.status.in_(_ACTIVE_REQUEST_STATUSES))
    )

    report = {
        "expected_alembic_revision": EXPECTED_ALEMBIC_REVISION,
        "current_alembic_revision": current_revision,
        "completed_identity_import_count": int(completed_identity_import_count or 0),
        "completed_ledger_import_count": int(completed_ledger_import_count or 0),
        "legacy_candidate_count": int(candidate_count or 0),
        "candidate_missing_task_or_provenance_count": int(candidate_missing_task_count or 0),
        "candidate_missing_point_account_count": int(candidate_missing_point_account_count or 0),
        "active_request_count": int(active_request_count or 0),
    }
    errors = _readiness_errors(report)
    return {**report, "ready_to_enable": not errors, "errors": errors}


def _completed_import_count(session: Session, import_kind: str) -> int:
    count = session.scalar(
        select(func.count())
        .select_from(SheetImportRun)
        .where(
            SheetImportRun.import_kind == import_kind,
            SheetImportRun.status == "completed",
            SheetImportRun.finished_at.is_not(None),
        )
    )
    return int(count or 0)


def _readiness_errors(report: dict[str, object]) -> tuple[str, ...]:
    errors: list[str] = []
    if report["current_alembic_revision"] != EXPECTED_ALEMBIC_REVISION:
        errors.append("unexpected_alembic_revision")
    if report["completed_identity_import_count"] == 0:
        errors.append("missing_completed_identity_import")
    if report["completed_ledger_import_count"] == 0:
        errors.append("missing_completed_ledger_import")
    if report["legacy_candidate_count"] == 0:
        errors.append("missing_legacy_candidates")
    if report["candidate_missing_task_or_provenance_count"]:
        errors.append("candidate_backfill_or_provenance_mismatch")
    if report["candidate_missing_point_account_count"]:
        errors.append("candidate_point_account_mismatch")
    if report["active_request_count"]:
        errors.append("active_player_link_requests_present")
    return tuple(errors)


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        return _write_error("this read-only command accepts no arguments")
    try:
        configure_session()
        with SessionLocal() as session:
            report = build_cutover_preflight(session)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["ready_to_enable"] else 2
    except SQLAlchemyError:
        return _write_error("database preflight failed")
    finally:
        dispose_session_engine()


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
