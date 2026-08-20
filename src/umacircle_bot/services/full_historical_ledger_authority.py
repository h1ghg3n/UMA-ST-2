from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Bet,
    BetJudgement,
    CirclePointAccount,
    CirclePointTransaction,
    GameAccount,
    Persona,
    Race,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.errors import LegacyImportConflictError, LegacyImportError
from umacircle_bot.domain.imports import normalize_import_source_identifier, normalize_sha256_hex
from umacircle_bot.runtime_preflight import EXPECTED_ALEMBIC_HEAD
from umacircle_bot.services.legacy_ledger_import import (
    LEDGER_TRANSACTION_SOURCE,
    LEGACY_LEDGER_IMPORT_KIND,
    LEGACY_OPENING_RECORD_TYPE,
    LegacyLedgerImportPlan,
    apply_legacy_ledger_import,
    preview_legacy_ledger_import,
)
from umacircle_bot.services.legacy_payout_correction import (
    LEGACY_PAYOUT_CORRECTION_IMPORT_KIND,
    LEGACY_PAYOUT_CORRECTION_SOURCE,
    LEGACY_PAYOUT_CORRECTION_TRANSACTION_TYPE,
    LegacyPayoutCorrectionPlan,
    apply_legacy_payout_correction,
    preview_legacy_payout_correction,
)
from umacircle_bot.services.legacy_point_scale import (
    scale_legacy_circle_point_balance,
    scale_legacy_room_point_amount,
)
from umacircle_bot.services.win5_first_baseline import (
    WIN5_FIRST_BASELINE_IMPORT_KIND,
    WIN5_FIRST_BASELINE_TRANSACTION_SOURCE,
)
from umacircle_bot.sheets.legacy_room_ledger import LegacyLedgerEntryKind

FULL_HISTORICAL_LEDGER_AUTHORITY_VERSION = 1
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class FullHistoricalLedgerCheckpoint:
    wallet_count: int
    ledger_transaction_count: int
    correction_transaction_count: int
    transaction_count: int
    bet_count: int
    judgement_count: int
    circle_point_total: int


V1_FULL_HISTORICAL_LEDGER_CHECKPOINT = FullHistoricalLedgerCheckpoint(
    wallet_count=33,
    ledger_transaction_count=785,
    correction_transaction_count=5,
    transaction_count=790,
    bet_count=335,
    judgement_count=324,
    circle_point_total=18530,
)


@dataclass(frozen=True, slots=True)
class FullHistoricalLedgerApplyAttempt:
    ledger_created_record_count: int
    ledger_skipped_record_count: int
    ledger_created_bet_count: int
    ledger_created_judgement_count: int
    ledger_created_transaction_count: int
    correction_created_record_count: int
    correction_skipped_record_count: int
    correction_created_transaction_count: int

    @property
    def created_transaction_count(self) -> int:
        return self.ledger_created_transaction_count + self.correction_created_transaction_count

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "created_transaction_count": self.created_transaction_count}


@dataclass(frozen=True, slots=True)
class FullHistoricalLedgerAuthorityReport:
    authority_version: int
    source_identifier: str
    source_checksum: str
    source_semantic_checksum: str
    importer_commit: str
    runtime_commit: str
    alembic_head: str
    business_key_prefix_checksum: str
    expected: dict[str, int]
    actual: dict[str, int]
    import_runs: tuple[dict[str, object], ...]
    transaction_type_source_multiset: tuple[dict[str, object], ...]
    transaction_business_key_multiset: tuple[dict[str, object], ...]
    wallet_vectors: tuple[dict[str, object], ...]
    first_apply: FullHistoricalLedgerApplyAttempt | None
    exact_retry: FullHistoricalLedgerApplyAttempt | None
    exact_retry_state_unchanged: bool | None
    errors: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "ready": self.ready,
            "first_apply": None if self.first_apply is None else self.first_apply.as_dict(),
            "exact_retry": None if self.exact_retry is None else self.exact_retry.as_dict(),
        }


def apply_full_historical_ledger_authority(
    session: Session,
    *,
    ledger_plan: LegacyLedgerImportPlan,
    correction_plan: LegacyPayoutCorrectionPlan,
    source_checksum: str,
    importer_commit: str,
    runtime_commit: str,
    alembic_head: str,
    checkpoint: FullHistoricalLedgerCheckpoint = V1_FULL_HISTORICAL_LEDGER_CHECKPOINT,
) -> FullHistoricalLedgerAuthorityReport:
    """Apply and prove the canonical Race 1-51 economic prefix atomically."""

    _require_clean_session(session)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    normalized_importer_commit = _normalize_commit(importer_commit, field="importer commit")
    normalized_runtime_commit = _normalize_commit(runtime_commit, field="runtime commit")
    if normalized_importer_commit != normalized_runtime_commit:
        raise LegacyImportError("historical ledger authority runtime commit does not match importer commit")
    _require_matching_plans(ledger_plan, correction_plan)
    source_errors = _source_checkpoint_errors(ledger_plan, correction_plan, checkpoint=checkpoint)
    if source_errors:
        raise LegacyImportConflictError(f"historical ledger source checkpoint conflict: {source_errors[0]}")
    _require_no_collapsed_baseline(session)

    with session.begin_nested():
        first_apply = _apply_attempt(
            session,
            ledger_plan=ledger_plan,
            correction_plan=correction_plan,
            source_checksum=normalized_checksum,
        )
        first_report = inspect_full_historical_ledger_authority(
            session,
            ledger_plan=ledger_plan,
            correction_plan=correction_plan,
            source_checksum=normalized_checksum,
            importer_commit=normalized_importer_commit,
            runtime_commit=normalized_runtime_commit,
            alembic_head=alembic_head,
            checkpoint=checkpoint,
            first_apply=first_apply,
        )
        if not first_report.ready:
            raise LegacyImportConflictError(
                f"historical ledger authority reconciliation failed: {first_report.errors[0]}"
            )

        before_retry_signature = _retry_state_signature(session)
        exact_retry = _apply_attempt(
            session,
            ledger_plan=ledger_plan,
            correction_plan=correction_plan,
            source_checksum=normalized_checksum,
        )
        after_retry_signature = _retry_state_signature(session)
        retry_state_unchanged = before_retry_signature == after_retry_signature
        retry_errors: list[str] = []
        if exact_retry.created_transaction_count:
            retry_errors.append("exact_retry_created_transactions")
        if exact_retry.ledger_created_record_count or exact_retry.correction_created_record_count:
            retry_errors.append("exact_retry_created_import_records")
        if not retry_state_unchanged:
            retry_errors.append("exact_retry_changed_target_state")

        report = inspect_full_historical_ledger_authority(
            session,
            ledger_plan=ledger_plan,
            correction_plan=correction_plan,
            source_checksum=normalized_checksum,
            importer_commit=normalized_importer_commit,
            runtime_commit=normalized_runtime_commit,
            alembic_head=alembic_head,
            checkpoint=checkpoint,
            first_apply=first_apply,
            exact_retry=exact_retry,
            exact_retry_state_unchanged=retry_state_unchanged,
        )
        if retry_errors:
            report = replace(report, errors=tuple(dict.fromkeys((*report.errors, *retry_errors))))
        if not report.ready:
            raise LegacyImportConflictError(f"historical ledger authority exact-retry gate failed: {report.errors[0]}")
        return report


def inspect_full_historical_ledger_authority(
    session: Session,
    *,
    ledger_plan: LegacyLedgerImportPlan,
    correction_plan: LegacyPayoutCorrectionPlan,
    source_checksum: str,
    importer_commit: str,
    runtime_commit: str,
    alembic_head: str,
    checkpoint: FullHistoricalLedgerCheckpoint = V1_FULL_HISTORICAL_LEDGER_CHECKPOINT,
    first_apply: FullHistoricalLedgerApplyAttempt | None = None,
    exact_retry: FullHistoricalLedgerApplyAttempt | None = None,
    exact_retry_state_unchanged: bool | None = None,
) -> FullHistoricalLedgerAuthorityReport:
    """Build a deterministic, DB-ID-independent E2 reconciliation artifact."""

    _require_clean_session(session)
    source_identifier = normalize_import_source_identifier(ledger_plan.source_identifier)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    normalized_importer_commit = _normalize_commit(importer_commit, field="importer commit")
    normalized_runtime_commit = _normalize_commit(runtime_commit, field="runtime commit")
    _require_matching_plans(ledger_plan, correction_plan)

    errors = list(_source_checkpoint_errors(ledger_plan, correction_plan, checkpoint=checkpoint))
    if normalized_importer_commit != normalized_runtime_commit:
        errors.append("runtime_commit_mismatch")
    if alembic_head != EXPECTED_ALEMBIC_HEAD:
        errors.append("alembic_head_mismatch")

    ledger_preview = preview_legacy_ledger_import(
        session,
        plan=ledger_plan,
        source_checksum=normalized_checksum,
    )
    correction_preview = preview_legacy_payout_correction(
        session,
        plan=correction_plan,
        source_checksum=normalized_checksum,
    )
    errors.extend(f"ledger_{conflict.code}" for conflict in ledger_preview.conflicts)
    errors.extend(f"correction_{conflict.code}" for conflict in correction_preview.conflicts)
    runs, run_errors = _authority_import_runs(
        session,
        source_identifier=source_identifier,
        source_checksum=normalized_checksum,
    )
    errors.extend(run_errors)

    transactions = tuple(session.scalars(select(CirclePointTransaction).order_by(CirclePointTransaction.id)))
    wallets = tuple(session.scalars(select(CirclePointAccount).order_by(CirclePointAccount.id)))
    bets = tuple(session.scalars(select(Bet).order_by(Bet.id)))
    judgements = tuple(session.scalars(select(BetJudgement).order_by(BetJudgement.id)))
    ledger_transactions = tuple(row for row in transactions if row.source == LEDGER_TRANSACTION_SOURCE)
    correction_transactions = tuple(row for row in transactions if row.source == LEGACY_PAYOUT_CORRECTION_SOURCE)
    allowed_transaction_ids = {row.id for row in (*ledger_transactions, *correction_transactions)}
    unexpected_transactions = tuple(row for row in transactions if row.id not in allowed_transaction_ids)

    actual = {
        "wallet_count": len(wallets),
        "ledger_transaction_count": len(ledger_transactions),
        "correction_transaction_count": len(correction_transactions),
        "transaction_count": len(transactions),
        "bet_count": len(bets),
        "judgement_count": len(judgements),
        "circle_point_total": sum(row.balance for row in wallets),
        "transaction_total": sum(row.amount for row in transactions),
        "unexpected_transaction_count": len(unexpected_transactions),
        "placement_transaction_count": sum(
            row.related_race_result_id is not None
            or row.type in {"match_placement_reward", "match_placement_reward_rollback"}
            for row in transactions
        ),
        "registration_transaction_count": sum(
            row.type == "initial_grant" or row.source == "staff_source_game_account_claim" for row in transactions
        ),
        "win5_transaction_count": sum(row.type == "win5_reward" for row in transactions),
        "collapsed_baseline_run_count": _count(
            session,
            SheetImportRun,
            SheetImportRun.import_kind == WIN5_FIRST_BASELINE_IMPORT_KIND,
        ),
        "collapsed_baseline_transaction_count": sum(
            row.source == WIN5_FIRST_BASELINE_TRANSACTION_SOURCE for row in transactions
        ),
        "duplicate_idempotency_key_count": _duplicate_idempotency_key_count(session),
    }
    actual.update(_orphan_counts(session, wallets=wallets, transactions=transactions, bets=bets, judgements=judgements))
    expected = asdict(checkpoint)
    for field, expected_value in expected.items():
        if actual[field] != expected_value:
            errors.append(f"{field}_mismatch")
    if actual["transaction_total"] != actual["circle_point_total"]:
        errors.append("wallet_ledger_total_mismatch")
    for field in (
        "unexpected_transaction_count",
        "placement_transaction_count",
        "registration_transaction_count",
        "win5_transaction_count",
        "collapsed_baseline_run_count",
        "collapsed_baseline_transaction_count",
        "duplicate_idempotency_key_count",
        "orphan_wallet_persona_count",
        "orphan_transaction_persona_count",
        "orphan_transaction_game_account_count",
        "orphan_bet_persona_count",
        "orphan_bet_game_account_count",
        "orphan_bet_race_count",
        "orphan_judgement_bet_count",
        "orphan_judgement_race_count",
    ):
        if actual[field]:
            errors.append(field)

    business_rows = _transaction_business_rows(ledger_plan, correction_plan)
    duplicate_business_keys = sum(
        count - 1 for count in Counter(row["business_key"] for row in business_rows).values() if count > 1
    )
    actual["duplicate_business_key_count"] = duplicate_business_keys
    if duplicate_business_keys:
        errors.append("duplicate_business_key_count")

    wallet_vectors, vector_errors = _wallet_vectors(
        session,
        ledger_plan=ledger_plan,
        correction_plan=correction_plan,
    )
    errors.extend(vector_errors)
    source_semantic_checksum = _source_semantic_checksum(
        ledger_plan,
        correction_plan,
        source_checksum=normalized_checksum,
    )
    prefix_checksum = _business_key_prefix_checksum(
        ledger_plan,
        correction_plan,
        transaction_rows=business_rows,
        wallet_vectors=wallet_vectors,
    )
    type_source_multiset = _type_source_multiset(business_rows)

    return FullHistoricalLedgerAuthorityReport(
        authority_version=FULL_HISTORICAL_LEDGER_AUTHORITY_VERSION,
        source_identifier=source_identifier,
        source_checksum=normalized_checksum,
        source_semantic_checksum=source_semantic_checksum,
        importer_commit=normalized_importer_commit,
        runtime_commit=normalized_runtime_commit,
        alembic_head=alembic_head,
        business_key_prefix_checksum=prefix_checksum,
        expected=expected,
        actual=actual,
        import_runs=runs,
        transaction_type_source_multiset=type_source_multiset,
        transaction_business_key_multiset=business_rows,
        wallet_vectors=wallet_vectors,
        first_apply=first_apply,
        exact_retry=exact_retry,
        exact_retry_state_unchanged=exact_retry_state_unchanged,
        errors=tuple(dict.fromkeys(errors)),
    )


def _apply_attempt(
    session: Session,
    *,
    ledger_plan: LegacyLedgerImportPlan,
    correction_plan: LegacyPayoutCorrectionPlan,
    source_checksum: str,
) -> FullHistoricalLedgerApplyAttempt:
    ledger = apply_legacy_ledger_import(session, plan=ledger_plan, source_checksum=source_checksum)
    correction = apply_legacy_payout_correction(session, plan=correction_plan, source_checksum=source_checksum)
    judgement_count = (
        sum(row.entry_kind is LegacyLedgerEntryKind.BET and not row.is_cancelled for row in ledger_plan.entries)
        if ledger.created_count
        else 0
    )
    return FullHistoricalLedgerApplyAttempt(
        ledger_created_record_count=ledger.created_count,
        ledger_skipped_record_count=ledger.skipped_count,
        ledger_created_bet_count=ledger.bet_count,
        ledger_created_judgement_count=judgement_count,
        ledger_created_transaction_count=ledger.transaction_count,
        correction_created_record_count=correction.created_count,
        correction_skipped_record_count=correction.skipped_count,
        correction_created_transaction_count=correction.created_count,
    )


def _source_checkpoint_errors(
    ledger_plan: LegacyLedgerImportPlan,
    correction_plan: LegacyPayoutCorrectionPlan,
    *,
    checkpoint: FullHistoricalLedgerCheckpoint,
) -> tuple[str, ...]:
    judgement_count = sum(
        row.entry_kind is LegacyLedgerEntryKind.BET and not row.is_cancelled for row in ledger_plan.entries
    )
    source_values = {
        "wallet_count": len(ledger_plan.openings),
        "ledger_transaction_count": ledger_plan.expected_transaction_count,
        "correction_transaction_count": len(correction_plan.rows),
        "transaction_count": ledger_plan.expected_transaction_count + len(correction_plan.rows),
        "bet_count": ledger_plan.expected_bet_count,
        "judgement_count": judgement_count,
        "circle_point_total": scale_legacy_circle_point_balance(
            ledger_plan.expected_balance_total + correction_plan.correction_total,
            field_name="full historical ledger checkpoint total",
        ),
    }
    expected = asdict(checkpoint)
    return tuple(f"source_{field}_mismatch" for field, value in source_values.items() if value != expected[field])


def _require_matching_plans(
    ledger_plan: LegacyLedgerImportPlan,
    correction_plan: LegacyPayoutCorrectionPlan,
) -> None:
    if ledger_plan.source_identifier != correction_plan.source_identifier:
        raise LegacyImportError("historical ledger and payout correction source identifiers differ")


def _require_no_collapsed_baseline(session: Session) -> None:
    run_exists = session.scalar(
        select(SheetImportRun.id).where(SheetImportRun.import_kind == WIN5_FIRST_BASELINE_IMPORT_KIND).limit(1)
    )
    transaction_exists = session.scalar(
        select(CirclePointTransaction.id)
        .where(CirclePointTransaction.source == WIN5_FIRST_BASELINE_TRANSACTION_SOURCE)
        .limit(1)
    )
    if run_exists is not None or transaction_exists is not None:
        raise LegacyImportConflictError("collapsed WIN5-first baseline cannot coexist with full historical ledger")


def _authority_import_runs(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
) -> tuple[tuple[dict[str, object], ...], tuple[str, ...]]:
    errors: list[str] = []
    payload: list[dict[str, object]] = []
    for import_kind in (LEGACY_LEDGER_IMPORT_KIND, LEGACY_PAYOUT_CORRECTION_IMPORT_KIND):
        runs = tuple(session.scalars(select(SheetImportRun).where(SheetImportRun.import_kind == import_kind)))
        if len(runs) != 1:
            errors.append(f"{import_kind}_run_count_mismatch")
            continue
        run = runs[0]
        if (
            run.source_type != "xlsx"
            or run.source_identifier != source_identifier
            or run.source_checksum != source_checksum
            or run.status != "completed"
            or run.finished_at is None
        ):
            errors.append(f"{import_kind}_run_provenance_mismatch")
        payload.append(
            {
                "import_kind": run.import_kind,
                "source_type": run.source_type,
                "source_identifier": run.source_identifier,
                "source_checksum": run.source_checksum,
                "status": run.status,
                "summary": _json_value(run.summary_json),
            }
        )
    return tuple(sorted(payload, key=lambda row: str(row["import_kind"]))), tuple(errors)


def _transaction_business_rows(
    ledger_plan: LegacyLedgerImportPlan,
    correction_plan: LegacyPayoutCorrectionPlan,
) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for opening in ledger_plan.openings:
        rows.append(
            _business_transaction(
                opening.source_key,
                "opening",
                LEDGER_TRANSACTION_SOURCE,
                LEGACY_OPENING_RECORD_TYPE,
                scale_legacy_room_point_amount(
                    opening.opening_balance,
                    field_name=f"legacy opening at source row {opening.source_row_number}",
                ),
            )
        )
    for entry in ledger_plan.entries:
        if entry.entry_kind is LegacyLedgerEntryKind.GRANT:
            rows.append(
                _business_transaction(
                    entry.source_key,
                    "grant",
                    LEDGER_TRANSACTION_SOURCE,
                    "admin_grant",
                    scale_legacy_room_point_amount(
                        entry.amount,
                        field_name=f"legacy grant at source row {entry.source_row_number}",
                    ),
                )
            )
        elif not entry.is_cancelled:
            amount = scale_legacy_room_point_amount(
                entry.amount,
                field_name=f"legacy stake at source row {entry.source_row_number}",
            )
            payout = scale_legacy_room_point_amount(
                entry.payout_amount,
                field_name=f"legacy payout at source row {entry.source_row_number}",
            )
            rows.extend(
                (
                    _business_transaction(
                        entry.source_key,
                        "stake",
                        LEDGER_TRANSACTION_SOURCE,
                        "bet_stake",
                        -amount,
                    ),
                    _business_transaction(
                        entry.source_key,
                        "payout",
                        LEDGER_TRANSACTION_SOURCE,
                        "settlement_reward",
                        payout,
                    ),
                )
            )
    for correction in correction_plan.rows:
        rows.append(
            _business_transaction(
                correction.source_key,
                "correction",
                LEGACY_PAYOUT_CORRECTION_SOURCE,
                LEGACY_PAYOUT_CORRECTION_TRANSACTION_TYPE,
                scale_legacy_room_point_amount(
                    correction.correction_amount,
                    field_name=f"legacy correction at source row {correction.source_row_number}",
                ),
            )
        )
    return tuple(sorted(rows, key=lambda row: str(row["business_key"])))


def _business_transaction(
    source_key: str,
    role: str,
    source: str,
    transaction_type: str,
    amount: int,
) -> dict[str, object]:
    return {
        "business_key": f"{source_key}:{role}",
        "source": source,
        "type": transaction_type,
        "amount": amount,
    }


def _type_source_multiset(rows: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    values: Counter[tuple[object, object]] = Counter((row["source"], row["type"]) for row in rows)
    amounts: Counter[tuple[object, object]] = Counter()
    for row in rows:
        amounts[(row["source"], row["type"])] += int(row["amount"])
    return tuple(
        {
            "source": source,
            "type": transaction_type,
            "count": count,
            "amount_total": amounts[(source, transaction_type)],
        }
        for (source, transaction_type), count in sorted(values.items(), key=lambda item: str(item[0]))
    )


def _wallet_vectors(
    session: Session,
    *,
    ledger_plan: LegacyLedgerImportPlan,
    correction_plan: LegacyPayoutCorrectionPlan,
) -> tuple[tuple[dict[str, object], ...], tuple[str, ...]]:
    vectors: dict[str, dict[str, int | str]] = {}
    source_key_by_discord_id: dict[str, str] = {}
    for opening in ledger_plan.openings:
        source_key_by_discord_id[opening.discord_user_id] = opening.identity_source_key
        vectors[opening.discord_user_id] = {
            "wallet_source_key": opening.identity_source_key,
            "opening": scale_legacy_room_point_amount(
                opening.opening_balance,
                field_name=f"legacy opening at source row {opening.source_row_number}",
            ),
            "grant": 0,
            "stake": 0,
            "payout": 0,
            "correction": 0,
        }
    for entry in ledger_plan.entries:
        vector = vectors[entry.discord_user_id]
        if entry.entry_kind is LegacyLedgerEntryKind.GRANT:
            vector["grant"] = int(vector["grant"]) + scale_legacy_room_point_amount(
                entry.amount,
                field_name=f"legacy grant at source row {entry.source_row_number}",
            )
        elif not entry.is_cancelled:
            vector["stake"] = int(vector["stake"]) - scale_legacy_room_point_amount(
                entry.amount,
                field_name=f"legacy stake at source row {entry.source_row_number}",
            )
            vector["payout"] = int(vector["payout"]) + scale_legacy_room_point_amount(
                entry.payout_amount,
                field_name=f"legacy payout at source row {entry.source_row_number}",
            )
    for correction in correction_plan.rows:
        vector = vectors.get(correction.discord_user_id)
        if vector is None:
            continue
        vector["correction"] = int(vector["correction"]) + scale_legacy_room_point_amount(
            correction.correction_amount,
            field_name=f"legacy correction at source row {correction.source_row_number}",
        )

    errors: list[str] = []
    output: list[dict[str, object]] = []
    records = {
        record.source_key: record
        for record in session.scalars(
            select(SheetImportRecord).where(
                SheetImportRecord.source_key.in_([opening.source_key for opening in ledger_plan.openings])
            )
        )
    }
    for opening in ledger_plan.openings:
        expected = vectors[opening.discord_user_id]
        expected_closing = sum(int(expected[field]) for field in ("opening", "grant", "stake", "payout", "correction"))
        actual = {"opening": 0, "grant": 0, "stake": 0, "payout": 0, "correction": 0, "closing": 0}
        record = records.get(opening.source_key)
        transaction = (
            session.get(CirclePointTransaction, record.target_entity_id)
            if record is not None and record.target_entity_id is not None
            else None
        )
        if transaction is None:
            errors.append(f"wallet_vector_target_missing:{opening.identity_source_key}")
        else:
            account_transactions = tuple(
                session.scalars(
                    select(CirclePointTransaction).where(
                        CirclePointTransaction.game_account_id == transaction.game_account_id,
                        CirclePointTransaction.source.in_((LEDGER_TRANSACTION_SOURCE, LEGACY_PAYOUT_CORRECTION_SOURCE)),
                    )
                )
            )
            for row in account_transactions:
                if row.source == LEGACY_PAYOUT_CORRECTION_SOURCE:
                    actual["correction"] += row.amount
                elif row.type == LEGACY_OPENING_RECORD_TYPE:
                    actual["opening"] += row.amount
                elif row.type == "admin_grant":
                    actual["grant"] += row.amount
                elif row.type == "bet_stake":
                    actual["stake"] += row.amount
                elif row.type == "settlement_reward":
                    actual["payout"] += row.amount
            wallet = session.scalar(
                select(CirclePointAccount).where(CirclePointAccount.persona_id == transaction.persona_id)
            )
            if wallet is None:
                errors.append(f"wallet_vector_wallet_missing:{opening.identity_source_key}")
            else:
                actual["closing"] = wallet.balance
        expected_payload = {
            field: int(expected[field]) for field in ("opening", "grant", "stake", "payout", "correction")
        }
        expected_payload["closing"] = expected_closing
        if expected_payload != actual:
            errors.append(f"wallet_vector_mismatch:{opening.identity_source_key}")
        output.append(
            {
                "wallet_source_key": source_key_by_discord_id[opening.discord_user_id],
                "expected": expected_payload,
                "actual": actual,
                "matches": expected_payload == actual,
            }
        )
    return tuple(sorted(output, key=lambda row: str(row["wallet_source_key"]))), tuple(errors)


def _source_semantic_checksum(
    ledger_plan: LegacyLedgerImportPlan,
    correction_plan: LegacyPayoutCorrectionPlan,
    *,
    source_checksum: str,
) -> str:
    return _canonical_sha256(
        {
            "authority_version": FULL_HISTORICAL_LEDGER_AUTHORITY_VERSION,
            "source_identifier": ledger_plan.source_identifier,
            "source_checksum": source_checksum,
            "ledger": [
                [row.source_key, row.row_fingerprint]
                for row in sorted((*ledger_plan.openings, *ledger_plan.entries), key=lambda item: item.source_key)
            ],
            "corrections": [
                [row.source_key, row.row_fingerprint, row.context_fingerprint]
                for row in sorted(correction_plan.rows, key=lambda item: item.source_key)
            ],
        }
    )


def _business_key_prefix_checksum(
    ledger_plan: LegacyLedgerImportPlan,
    correction_plan: LegacyPayoutCorrectionPlan,
    *,
    transaction_rows: Sequence[Mapping[str, object]],
    wallet_vectors: Sequence[Mapping[str, object]],
) -> str:
    return _canonical_sha256(
        {
            "authority_version": FULL_HISTORICAL_LEDGER_AUTHORITY_VERSION,
            "transactions": transaction_rows,
            "wallets": wallet_vectors,
            "bets": [
                {
                    "source_key": row.source_key,
                    "race_source_key": row.race_source_key,
                    "status": "cancelled" if row.is_cancelled else "settled",
                    "bet_type": row.bet_type,
                    "numbers": row.numbers,
                    "amount": scale_legacy_room_point_amount(
                        row.amount,
                        field_name=f"legacy Bet at source row {row.source_row_number}",
                    ),
                }
                for row in ledger_plan.entries
                if row.entry_kind is LegacyLedgerEntryKind.BET
            ],
            "judgements": [
                {
                    "source_key": row.source_key,
                    "is_hit": row.payout_amount > 0,
                    "payout_amount": scale_legacy_room_point_amount(
                        row.payout_amount,
                        field_name=f"legacy judgement at source row {row.source_row_number}",
                    ),
                }
                for row in ledger_plan.entries
                if row.entry_kind is LegacyLedgerEntryKind.BET and not row.is_cancelled
            ],
            "source_identifier": correction_plan.source_identifier,
        }
    )


def _orphan_counts(
    session: Session,
    *,
    wallets: Sequence[CirclePointAccount],
    transactions: Sequence[CirclePointTransaction],
    bets: Sequence[Bet],
    judgements: Sequence[BetJudgement],
) -> dict[str, int]:
    persona_ids = set(session.scalars(select(Persona.id)))
    game_account_ids = set(session.scalars(select(GameAccount.id)))
    race_ids = set(session.scalars(select(Race.id)))
    bet_ids = {row.id for row in bets}
    return {
        "orphan_wallet_persona_count": sum(row.persona_id not in persona_ids for row in wallets),
        "orphan_transaction_persona_count": sum(row.persona_id not in persona_ids for row in transactions),
        "orphan_transaction_game_account_count": sum(
            row.game_account_id not in game_account_ids for row in transactions
        ),
        "orphan_bet_persona_count": sum(row.persona_id not in persona_ids for row in bets),
        "orphan_bet_game_account_count": sum(row.game_account_id not in game_account_ids for row in bets),
        "orphan_bet_race_count": sum(row.race_id not in race_ids for row in bets),
        "orphan_judgement_bet_count": sum(row.bet_id not in bet_ids for row in judgements),
        "orphan_judgement_race_count": sum(row.race_id not in race_ids for row in judgements),
    }


def _duplicate_idempotency_key_count(session: Session) -> int:
    counts = session.execute(
        select(CirclePointTransaction.idempotency_key, func.count())
        .where(CirclePointTransaction.idempotency_key.is_not(None))
        .group_by(CirclePointTransaction.idempotency_key)
        .having(func.count() > 1)
    )
    return sum(int(count) - 1 for _key, count in counts)


def _retry_state_signature(session: Session) -> str:
    return _canonical_sha256(
        {
            model.__tablename__: _model_rows(session, model)
            for model in (
                CirclePointAccount,
                CirclePointTransaction,
                Bet,
                BetJudgement,
                SheetImportRun,
                SheetImportRecord,
            )
        }
    )


def _model_rows(session: Session, model: type[object]) -> list[dict[str, object]]:
    table = model.__table__  # type: ignore[attr-defined]
    columns = tuple(table.columns)
    statement = select(*columns).order_by(*table.primary_key.columns)
    return [
        {column.name: _json_value(value) for column, value in zip(columns, row, strict=True)}
        for row in session.execute(statement)
    ]


def _count(session: Session, model: type[object], *criteria: object) -> int:
    statement = select(func.count()).select_from(model)
    if criteria:
        statement = statement.where(*criteria)
    return int(session.scalar(statement) or 0)


def _normalize_commit(value: object, *, field: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if not _COMMIT_RE.fullmatch(normalized):
        raise LegacyImportError(f"{field} must be a full 40-character Git SHA")
    return normalized


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(_json_value(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return sha256(payload.encode("utf-8")).hexdigest()


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("historical ledger authority requires a clean session")
