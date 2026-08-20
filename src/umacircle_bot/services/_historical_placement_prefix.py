from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Bet,
    BetJudgement,
    CirclePointTransaction,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.errors import LegacyImportConflictError
from umacircle_bot.services._historical_placement_manifest import _canonical_checksum
from umacircle_bot.services.legacy_ledger_import import (
    LEDGER_TRANSACTION_SOURCE,
    LEGACY_LEDGER_IMPORT_KIND,
)
from umacircle_bot.services.legacy_payout_correction import (
    LEGACY_PAYOUT_CORRECTION_IMPORT_KIND,
    LEGACY_PAYOUT_CORRECTION_SOURCE,
)

_E2_IMPORT_KINDS = frozenset({LEGACY_LEDGER_IMPORT_KIND, LEGACY_PAYOUT_CORRECTION_IMPORT_KIND})
_E2_TRANSACTION_SOURCES = frozenset({LEDGER_TRANSACTION_SOURCE, LEGACY_PAYOUT_CORRECTION_SOURCE})


def _require_e2_prefix_runs(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
    lock_rows: bool,
) -> tuple[SheetImportRun, ...]:
    statement = select(SheetImportRun).where(SheetImportRun.import_kind.in_(_E2_IMPORT_KINDS))
    if lock_rows:
        statement = statement.with_for_update()
    runs = tuple(session.scalars(statement))
    by_kind: dict[str, list[SheetImportRun]] = defaultdict(list)
    for run in runs:
        by_kind[run.import_kind].append(run)
    if set(by_kind) != _E2_IMPORT_KINDS or any(len(by_kind[kind]) != 1 for kind in _E2_IMPORT_KINDS):
        raise LegacyImportConflictError("historical placement correction requires the complete E2 prefix audit")
    if any(
        run.source_identifier != source_identifier
        or run.source_checksum != source_checksum
        or run.status != "completed"
        or run.finished_at is None
        for run in runs
    ):
        raise LegacyImportConflictError("historical placement correction E2 prefix provenance changed")
    return runs


def _e2_prefix_state_checksum(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
    lock_rows: bool,
) -> str:
    runs = _require_e2_prefix_runs(
        session,
        source_identifier=source_identifier,
        source_checksum=source_checksum,
        lock_rows=lock_rows,
    )
    run_ids = [run.id for run in runs]
    record_statement = (
        select(SheetImportRecord)
        .where(SheetImportRecord.import_run_id.in_(run_ids))
        .order_by(SheetImportRecord.source_key)
    )
    transaction_statement = (
        select(CirclePointTransaction)
        .where(CirclePointTransaction.source.in_(_E2_TRANSACTION_SOURCES))
        .order_by(CirclePointTransaction.id)
    )
    if lock_rows:
        record_statement = record_statement.with_for_update()
        transaction_statement = transaction_statement.with_for_update()
    records = tuple(session.scalars(record_statement))
    transactions = tuple(session.scalars(transaction_statement))
    bet_ids = sorted(
        {
            int(record.target_entity_id)
            for record in records
            if record.target_entity_type == "bet" and record.target_entity_id is not None
        }
    )
    bet_statement = select(Bet).where(Bet.id.in_(bet_ids)).order_by(Bet.id)
    judgement_statement = select(BetJudgement).where(BetJudgement.bet_id.in_(bet_ids)).order_by(BetJudgement.id)
    if lock_rows:
        bet_statement = bet_statement.with_for_update()
        judgement_statement = judgement_statement.with_for_update()
    bets = tuple(session.scalars(bet_statement))
    judgements = tuple(session.scalars(judgement_statement))
    prefix_by_persona: dict[str, int] = defaultdict(int)
    for transaction in transactions:
        prefix_by_persona[transaction.persona_id] += transaction.amount
    return _canonical_checksum(
        {
            "runs": [
                {
                    "id": run.id,
                    "kind": run.import_kind,
                    "source_identifier": run.source_identifier,
                    "source_checksum": run.source_checksum,
                    "status": run.status,
                    "summary": run.summary_json,
                }
                for run in sorted(runs, key=lambda row: row.import_kind)
            ],
            "records": [
                {
                    "id": record.id,
                    "run_id": record.import_run_id,
                    "source_key": record.source_key,
                    "row_fingerprint": record.row_fingerprint,
                    "record_type": record.record_type,
                    "status": record.status,
                    "target_entity_type": record.target_entity_type,
                    "target_entity_id": record.target_entity_id,
                    "detail": record.detail_json,
                }
                for record in records
            ],
            "transactions": [
                {
                    "id": row.id,
                    "persona_id": row.persona_id,
                    "game_account_id": row.game_account_id,
                    "type": row.type,
                    "amount": row.amount,
                    "source": row.source,
                    "related_bet_id": row.related_bet_id,
                    "related_race_result_id": row.related_race_result_id,
                    "idempotency_key": row.idempotency_key,
                }
                for row in transactions
            ],
            "bets": [
                {
                    column.name: getattr(row, column.name)
                    for column in Bet.__table__.columns
                    if column.name != "created_at"
                }
                for row in bets
            ],
            "judgements": [
                {
                    column.name: getattr(row, column.name)
                    for column in BetJudgement.__table__.columns
                    if column.name != "created_at"
                }
                for row in judgements
            ],
            "prefix_by_persona": sorted(prefix_by_persona.items()),
        }
    )
