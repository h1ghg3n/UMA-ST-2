from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import Bet, Race, SheetImportRecord, SheetImportRun
from umacircle_bot.services.legacy_ledger_import import (
    LEGACY_LEDGER_IMPORT_KIND,
    LEGACY_LEDGER_RECORD_TYPE,
)


def count_historical_bet_owner_audit_mismatches(session: Session) -> int:
    """Count imported Bets whose immutable Persona owner is not independently audited."""

    historical_bets: dict[int, str] = {}
    for bet_id, persona_id in session.execute(
        select(Bet.id, Bet.persona_id)
        .join(Race, Race.id == Bet.race_id)
        .where(Race.external_source.is_not(None))
        .order_by(Bet.id)
    ):
        historical_bets[bet_id] = persona_id
    audit_records_by_bet_id: dict[int, list[SheetImportRecord]] = defaultdict(list)
    invalid_record_ids: set[int] = set()
    records = session.scalars(
        select(SheetImportRecord)
        .join(SheetImportRun, SheetImportRun.id == SheetImportRecord.import_run_id)
        .where(
            SheetImportRun.import_kind == LEGACY_LEDGER_IMPORT_KIND,
            SheetImportRun.status == "completed",
            SheetImportRecord.record_type == LEGACY_LEDGER_RECORD_TYPE,
            SheetImportRecord.target_entity_type == "bet",
        )
        .order_by(SheetImportRecord.id)
    )
    for record in records:
        target_id = record.target_entity_id
        if not isinstance(target_id, int) or isinstance(target_id, bool) or target_id not in historical_bets:
            invalid_record_ids.add(record.id)
            continue
        audit_records_by_bet_id[target_id].append(record)

    mismatch_count = len(invalid_record_ids)
    for bet_id, persona_id in historical_bets.items():
        records_for_bet = audit_records_by_bet_id.get(bet_id, ())
        if len(records_for_bet) != 1:
            mismatch_count += 1
            continue
        record = records_for_bet[0]
        detail = record.detail_json if isinstance(record.detail_json, dict) else {}
        if record.status != "applied" or detail.get("persona_id") != persona_id:
            mismatch_count += 1
    return mismatch_count
