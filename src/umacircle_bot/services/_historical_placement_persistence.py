from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    CirclePointAccount,
    CirclePointTransaction,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.betting import MAX_CIRCLE_POINT_BALANCE
from umacircle_bot.domain.errors import LegacyImportConflictError
from umacircle_bot.domain.imports import normalize_sha256_hex
from umacircle_bot.services._historical_placement_manifest import (
    HISTORICAL_PLACEMENT_CORRECTION_IMPORT_KIND,
    HISTORICAL_PLACEMENT_CORRECTION_RECORD_TYPE,
    HISTORICAL_PLACEMENT_CORRECTION_SHEET_NAME,
    HISTORICAL_PLACEMENT_CORRECTION_SOURCE_TYPE,
    HISTORICAL_PLACEMENT_CORRECTION_VERSION,
    HISTORICAL_PLACEMENT_TRANSACTION_SOURCE,
    HISTORICAL_PLACEMENT_TRANSACTION_TYPE,
    HistoricalPlacementCorrectionApplyResult,
    HistoricalPlacementCorrectionManifest,
    HistoricalPlacementCorrectionPlan,
    _canonical_checksum,
    _HistoricalPlacementSelection,
    _require_clean_session,
)
from umacircle_bot.services._historical_placement_prefix import (
    _E2_TRANSACTION_SOURCES,
    _e2_prefix_state_checksum,
)


def apply_historical_placement_correction(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
    confirmed_mapping_decision_checksum: str,
    e2_business_key_prefix_checksum: str,
    confirmed_manifest_checksum: str,
    build_plan: Callable[..., HistoricalPlacementCorrectionPlan],
) -> HistoricalPlacementCorrectionApplyResult:
    """Append the reviewed correction or validate an exact retry."""

    _require_clean_session(session)
    confirmed_manifest = normalize_sha256_hex(
        confirmed_manifest_checksum,
        field_name="confirmed placement manifest checksum",
    )
    with session.begin_nested():
        plan = build_plan(
            session,
            source_identifier=source_identifier,
            source_checksum=source_checksum,
            confirmed_mapping_decision_checksum=confirmed_mapping_decision_checksum,
            e2_business_key_prefix_checksum=e2_business_key_prefix_checksum,
            lock_rows=True,
        )
        if not plan.manifest.ready:
            raise LegacyImportConflictError(
                f"historical placement correction has unresolved conflicts: {plan.manifest.conflicts[0].code}"
            )
        if plan.manifest.manifest_checksum != confirmed_manifest:
            raise LegacyImportConflictError("confirmed placement manifest checksum does not match the live plan")

        prefix_before = _e2_prefix_state_checksum(
            session,
            source_identifier=plan.manifest.source_identifier,
            source_checksum=plan.manifest.source_checksum,
            lock_rows=True,
        )
        runs = _correction_runs(
            session,
            source_identifier=plan.manifest.source_identifier,
            lock_rows=True,
        )
        if runs:
            if len(runs) != 1:
                raise LegacyImportConflictError("historical placement correction audit is missing or ambiguous")
            _validate_applied_correction(
                session,
                run=runs[0],
                plan=plan,
                e2_prefix_state_checksum=prefix_before,
                lock_rows=True,
            )
            _require_wallet_ledger_balance(session, lock_rows=True)
            return HistoricalPlacementCorrectionApplyResult(
                import_run_id=runs[0].id,
                manifest_checksum=plan.manifest.manifest_checksum,
                expected_total=plan.manifest.expected_total,
                created_transaction_count=0,
                skipped_transaction_count=len(plan.selections),
                already_applied=True,
                e2_prefix_state_unchanged=True,
            )

        if _historical_placement_transactions(session, lock_rows=True):
            raise LegacyImportConflictError("historical placement correction contains a partial untracked apply")
        _require_pre_correction_economy(session)
        _require_wallet_ledger_balance(session, lock_rows=True)
        wallets = _lock_selection_wallets(session, plan.selections)

        run = SheetImportRun(
            import_kind=HISTORICAL_PLACEMENT_CORRECTION_IMPORT_KIND,
            source_type=HISTORICAL_PLACEMENT_CORRECTION_SOURCE_TYPE,
            source_identifier=plan.manifest.source_identifier,
            source_checksum=plan.manifest.source_checksum,
            status="running",
        )
        session.add(run)
        session.flush()
        for row_number, selection in enumerate(plan.selections, start=1):
            wallet = wallets[selection.persona_id]
            new_balance = wallet.balance + selection.amount
            if new_balance > MAX_CIRCLE_POINT_BALANCE:
                raise LegacyImportConflictError("historical placement correction would overflow a wallet")
            wallet.balance = new_balance
            transaction = CirclePointTransaction(
                persona_id=selection.persona_id,
                game_account_id=selection.game_account_id,
                type=HISTORICAL_PLACEMENT_TRANSACTION_TYPE,
                amount=selection.amount,
                reason="Historical Circle Match placement reward correction",
                source=HISTORICAL_PLACEMENT_TRANSACTION_SOURCE,
                related_race_result_id=selection.result_id,
                idempotency_key=_transaction_idempotency_key(plan.manifest, selection),
            )
            session.add(transaction)
            session.flush()
            detail = _selection_detail(plan.manifest, selection)
            session.add(
                SheetImportRecord(
                    import_run_id=run.id,
                    source_key=_record_source_key(plan.manifest, selection),
                    row_fingerprint=_canonical_checksum(detail),
                    source_sheet_name=HISTORICAL_PLACEMENT_CORRECTION_SHEET_NAME,
                    source_row_number=row_number,
                    record_type=HISTORICAL_PLACEMENT_CORRECTION_RECORD_TYPE,
                    status="applied",
                    target_entity_type="room_point_transaction",
                    target_entity_id=transaction.id,
                    detail_json=detail,
                )
            )
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        run.summary_json = _run_summary(plan, e2_prefix_state_checksum=prefix_before)
        session.flush()

        prefix_after = _e2_prefix_state_checksum(
            session,
            source_identifier=plan.manifest.source_identifier,
            source_checksum=plan.manifest.source_checksum,
            lock_rows=True,
        )
        if prefix_after != prefix_before:
            raise LegacyImportConflictError("historical placement correction changed the E2 prefix")
        _require_wallet_ledger_balance(session, lock_rows=True)
        _validate_applied_correction(
            session,
            run=run,
            plan=plan,
            e2_prefix_state_checksum=prefix_before,
            lock_rows=True,
        )
        return HistoricalPlacementCorrectionApplyResult(
            import_run_id=run.id,
            manifest_checksum=plan.manifest.manifest_checksum,
            expected_total=plan.manifest.expected_total,
            created_transaction_count=len(plan.selections),
            skipped_transaction_count=0,
            already_applied=False,
            e2_prefix_state_unchanged=True,
        )


def _lock_selection_wallets(
    session: Session,
    selections: Sequence[_HistoricalPlacementSelection],
) -> dict[str, CirclePointAccount]:
    persona_ids = sorted({selection.persona_id for selection in selections})
    wallets = {
        row.persona_id: row
        for row in session.scalars(
            select(CirclePointAccount)
            .where(CirclePointAccount.persona_id.in_(persona_ids))
            .order_by(CirclePointAccount.persona_id)
            .with_for_update()
        )
    }
    if set(wallets) != set(persona_ids):
        raise LegacyImportConflictError("historical placement correction wallet coverage changed")
    return wallets


def _require_pre_correction_economy(session: Session) -> None:
    unexpected = session.scalar(
        select(CirclePointTransaction.id)
        .where(CirclePointTransaction.source.not_in(_E2_TRANSACTION_SOURCES))
        .limit(1)
        .with_for_update()
    )
    null_source = session.scalar(
        select(CirclePointTransaction.id).where(CirclePointTransaction.source.is_(None)).limit(1).with_for_update()
    )
    if unexpected is not None or null_source is not None:
        raise LegacyImportConflictError("historical placement correction must precede post-prefix economy")


def _require_wallet_ledger_balance(session: Session, *, lock_rows: bool) -> None:
    wallet_statement = select(CirclePointAccount).order_by(CirclePointAccount.persona_id)
    transaction_statement = select(CirclePointTransaction).order_by(CirclePointTransaction.id)
    if lock_rows:
        wallet_statement = wallet_statement.with_for_update()
        transaction_statement = transaction_statement.with_for_update()
    wallets = tuple(session.scalars(wallet_statement))
    transactions = tuple(session.scalars(transaction_statement))
    totals: dict[str, int] = defaultdict(int)
    for transaction in transactions:
        totals[transaction.persona_id] += transaction.amount
    if any(wallet.balance != totals.pop(wallet.persona_id, 0) for wallet in wallets) or totals:
        raise LegacyImportConflictError("Circle Point wallet and ledger totals differ")


def _correction_runs(
    session: Session,
    *,
    source_identifier: str,
    lock_rows: bool,
) -> tuple[SheetImportRun, ...]:
    statement = select(SheetImportRun).where(
        SheetImportRun.import_kind == HISTORICAL_PLACEMENT_CORRECTION_IMPORT_KIND,
        SheetImportRun.source_identifier == source_identifier,
    )
    if lock_rows:
        statement = statement.with_for_update()
    return tuple(session.scalars(statement))


def _historical_placement_transactions(
    session: Session,
    *,
    lock_rows: bool,
) -> tuple[CirclePointTransaction, ...]:
    statement = select(CirclePointTransaction).where(
        (CirclePointTransaction.source == HISTORICAL_PLACEMENT_TRANSACTION_SOURCE)
        | (CirclePointTransaction.type == HISTORICAL_PLACEMENT_TRANSACTION_TYPE)
    )
    if lock_rows:
        statement = statement.with_for_update()
    return tuple(session.scalars(statement))


def _validate_applied_correction(
    session: Session,
    *,
    run: SheetImportRun,
    plan: HistoricalPlacementCorrectionPlan,
    e2_prefix_state_checksum: str,
    lock_rows: bool,
) -> None:
    if (
        run.source_type != HISTORICAL_PLACEMENT_CORRECTION_SOURCE_TYPE
        or run.source_identifier != plan.manifest.source_identifier
        or run.source_checksum != plan.manifest.source_checksum
        or run.status != "completed"
        or run.finished_at is None
        or run.summary_json != _run_summary(plan, e2_prefix_state_checksum=e2_prefix_state_checksum)
    ):
        raise LegacyImportConflictError("historical placement correction audit summary changed")
    statement = (
        select(SheetImportRecord)
        .where(SheetImportRecord.import_run_id == run.id)
        .order_by(SheetImportRecord.source_row_number)
    )
    if lock_rows:
        statement = statement.with_for_update()
    records = tuple(session.scalars(statement))
    if len(records) != len(plan.selections):
        raise LegacyImportConflictError("historical placement correction audit coverage changed")
    records_by_key = {record.source_key: record for record in records}
    expected_transaction_ids: set[int] = set()
    for row_number, selection in enumerate(plan.selections, start=1):
        source_key = _record_source_key(plan.manifest, selection)
        record = records_by_key.get(source_key)
        detail = _selection_detail(plan.manifest, selection)
        transaction = (
            session.get(CirclePointTransaction, record.target_entity_id)
            if record is not None and record.target_entity_id is not None
            else None
        )
        if (
            record is None
            or record.source_row_number != row_number
            or record.row_fingerprint != _canonical_checksum(detail)
            or record.source_sheet_name != HISTORICAL_PLACEMENT_CORRECTION_SHEET_NAME
            or record.record_type != HISTORICAL_PLACEMENT_CORRECTION_RECORD_TYPE
            or record.status != "applied"
            or record.target_entity_type != "room_point_transaction"
            or record.detail_json != detail
            or transaction is None
            or transaction.persona_id != selection.persona_id
            or transaction.game_account_id != selection.game_account_id
            or transaction.type != HISTORICAL_PLACEMENT_TRANSACTION_TYPE
            or transaction.amount != selection.amount
            or transaction.source != HISTORICAL_PLACEMENT_TRANSACTION_SOURCE
            or transaction.related_bet_id is not None
            or transaction.related_race_result_id != selection.result_id
            or transaction.idempotency_key != _transaction_idempotency_key(plan.manifest, selection)
        ):
            raise LegacyImportConflictError("historical placement correction transaction audit changed")
        expected_transaction_ids.add(transaction.id)
    actual_transactions = _historical_placement_transactions(session, lock_rows=lock_rows)
    if {row.id for row in actual_transactions} != expected_transaction_ids:
        raise LegacyImportConflictError("historical placement correction transaction set changed")


def _run_summary(
    plan: HistoricalPlacementCorrectionPlan,
    *,
    e2_prefix_state_checksum: str,
) -> dict[str, object]:
    return {
        "manifest_version": HISTORICAL_PLACEMENT_CORRECTION_VERSION,
        "manifest_checksum": plan.manifest.manifest_checksum,
        "manifest": plan.manifest.as_dict(),
        "e2_prefix_state_checksum": e2_prefix_state_checksum,
        "selected_result_count": len(plan.selections),
        "suppressed_result_count": sum(len(row.suppressed_result_business_keys) for row in plan.selections),
        "expected_total": plan.manifest.expected_total,
    }


def _selection_detail(
    manifest: HistoricalPlacementCorrectionManifest,
    selection: _HistoricalPlacementSelection,
) -> dict[str, object]:
    return {
        "manifest_version": HISTORICAL_PLACEMENT_CORRECTION_VERSION,
        "manifest_checksum": manifest.manifest_checksum,
        "source_checksum": manifest.source_checksum,
        "reward_policy_checksum": manifest.reward_policy_checksum,
        "race_business_key": selection.race_business_key,
        "result_business_key": selection.result_business_key,
        "game_account_business_key": selection.game_account_business_key,
        "owner_business_key": selection.owner_business_key,
        "amount": selection.amount,
        "suppressed_result_business_keys": list(selection.suppressed_result_business_keys),
    }


def _record_source_key(
    manifest: HistoricalPlacementCorrectionManifest,
    selection: _HistoricalPlacementSelection,
) -> str:
    return _canonical_checksum(
        {
            "kind": HISTORICAL_PLACEMENT_CORRECTION_IMPORT_KIND,
            "manifest_checksum": manifest.manifest_checksum,
            "result_business_key": selection.result_business_key,
        }
    )


def _transaction_idempotency_key(
    manifest: HistoricalPlacementCorrectionManifest,
    selection: _HistoricalPlacementSelection,
) -> str:
    basis = _canonical_checksum(
        {
            "operation": HISTORICAL_PLACEMENT_TRANSACTION_SOURCE,
            "source_checksum": manifest.source_checksum,
            "reward_policy_checksum": manifest.reward_policy_checksum,
            "result_business_key": selection.result_business_key,
        }
    )
    return f"historical-placement:{basis}"
