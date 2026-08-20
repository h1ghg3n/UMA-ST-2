from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Bet,
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    Persona,
    SheetImportRun,
)
from umacircle_bot.domain.circle_point_provenance import (
    ACCOUNT_REGISTRATION_APPROVAL_TRANSACTION_SOURCE,
    ACCOUNT_REGISTRATION_INITIAL_GRANT_TRANSACTION_TYPE,
)
from umacircle_bot.services.circle_point_integrity import count_historical_bet_owner_audit_mismatches
from umacircle_bot.services.legacy_import import LEGACY_IDENTITY_POINT_IMPORT_KIND
from umacircle_bot.services.legacy_ledger_import import LEGACY_LEDGER_IMPORT_KIND
from umacircle_bot.services.legacy_payout_correction import LEGACY_PAYOUT_CORRECTION_IMPORT_KIND
from umacircle_bot.services.win5_first_baseline import (
    WIN5_FIRST_BASELINE_IMPORT_KIND,
    WIN5_FIRST_BASELINE_TRANSACTION_SOURCE,
    WIN5_FIRST_BASELINE_TRANSACTION_TYPE,
)
from umacircle_bot.sheets.current_win5_replay_manifest import CurrentWin5ReplayManifest


def build_current_win5_replay_point_report(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
    participant_persona_ids: set[str],
) -> tuple[dict[str, object], tuple[str, ...]]:
    errors: list[str] = []
    baseline_runs = tuple(
        session.scalars(
            select(SheetImportRun).where(
                SheetImportRun.import_kind == WIN5_FIRST_BASELINE_IMPORT_KIND,
                SheetImportRun.source_type == "xlsx",
                SheetImportRun.source_identifier == manifest.room_match_source_identifier,
                SheetImportRun.source_checksum == manifest.room_match_source_checksum,
                SheetImportRun.status == "completed",
                SheetImportRun.finished_at.is_not(None),
            )
        )
    )
    identity_run_count = int(
        session.scalar(
            select(func.count())
            .select_from(SheetImportRun)
            .where(
                SheetImportRun.import_kind == LEGACY_IDENTITY_POINT_IMPORT_KIND,
                SheetImportRun.source_identifier == manifest.room_match_source_identifier,
                SheetImportRun.source_checksum == manifest.room_match_source_checksum,
                SheetImportRun.status == "completed",
            )
        )
        or 0
    )
    if identity_run_count != 1 or len(baseline_runs) != 1:
        errors.append("baseline_import_provenance_mismatch")
    baseline_summary = baseline_runs[0].summary_json if len(baseline_runs) == 1 else None
    if not isinstance(baseline_summary, dict) or (
        baseline_summary.get("baseline_checksum") != manifest.win5_first_baseline_checksum
        or baseline_summary.get("identity_count") != manifest.expected_source_identity_count
        or baseline_summary.get("target_total") != manifest.target_point_totals.room_match_baseline
    ):
        errors.append("baseline_summary_mismatch")
    forbidden_import_count = int(
        session.scalar(
            select(func.count())
            .select_from(SheetImportRun)
            .where(SheetImportRun.import_kind.in_((LEGACY_LEDGER_IMPORT_KIND, LEGACY_PAYOUT_CORRECTION_IMPORT_KIND)))
        )
        or 0
    )
    if forbidden_import_count:
        errors.append("historical_monetary_import_present")

    transactions = tuple(session.scalars(select(CirclePointTransaction).order_by(CirclePointTransaction.id)))
    baseline_transactions = tuple(
        transaction
        for transaction in transactions
        if transaction.type == WIN5_FIRST_BASELINE_TRANSACTION_TYPE
        and transaction.source == WIN5_FIRST_BASELINE_TRANSACTION_SOURCE
    )
    registration_transactions = tuple(
        transaction
        for transaction in transactions
        if transaction.type == ACCOUNT_REGISTRATION_INITIAL_GRANT_TRANSACTION_TYPE
        and transaction.source == ACCOUNT_REGISTRATION_APPROVAL_TRANSACTION_SOURCE
    )
    reward_transactions = tuple(
        transaction
        for transaction in transactions
        if transaction.type == "win5_reward" and transaction.source == "win5_score"
    )
    if (
        len(baseline_transactions) != manifest.expected_source_identity_count
        or sum(transaction.amount for transaction in baseline_transactions)
        != manifest.target_point_totals.room_match_baseline
    ):
        errors.append("baseline_transaction_mismatch")
    expected_registration_count = len(manifest.participants) - manifest.expected_source_participant_count
    if (
        len(registration_transactions) != expected_registration_count
        or sum(transaction.amount for transaction in registration_transactions)
        != manifest.target_point_totals.registration
    ):
        errors.append("registration_transaction_mismatch")
    if (
        len(reward_transactions) != manifest.expected_reward_transaction_count
        or sum(transaction.amount for transaction in reward_transactions) != manifest.target_point_totals.win5_reward
    ):
        errors.append("win5_reward_transaction_mismatch")
    classified_ids = {
        transaction.id for transaction in (*baseline_transactions, *registration_transactions, *reward_transactions)
    }
    if classified_ids != {transaction.id for transaction in transactions}:
        errors.append("unexpected_point_transaction")

    wallets = tuple(session.scalars(select(CirclePointAccount).order_by(CirclePointAccount.persona_id)))
    wallet_by_persona = {wallet.persona_id: wallet.balance for wallet in wallets}
    transaction_by_persona = {
        str(row.persona_id): int(row[1] or 0)
        for row in session.execute(
            select(CirclePointTransaction.persona_id, func.sum(CirclePointTransaction.amount))
            .group_by(CirclePointTransaction.persona_id)
            .order_by(CirclePointTransaction.persona_id)
        )
    }
    wallet_total = sum(wallet_by_persona.values())
    transaction_total = sum(transaction.amount for transaction in transactions)
    if (
        wallet_total != manifest.target_point_totals.current
        or transaction_total != manifest.target_point_totals.current
        or wallet_by_persona != transaction_by_persona
    ):
        errors.append("room_point_reconciliation_mismatch")
    transaction_orphan_count = int(
        session.scalar(
            select(func.count())
            .select_from(CirclePointTransaction)
            .outerjoin(Persona, Persona.id == CirclePointTransaction.persona_id)
            .outerjoin(CirclePointAccount, CirclePointAccount.persona_id == CirclePointTransaction.persona_id)
            .outerjoin(GameAccount, GameAccount.id == CirclePointTransaction.game_account_id)
            .where(
                or_(
                    Persona.id.is_(None),
                    CirclePointAccount.id.is_(None),
                    GameAccount.id.is_(None),
                )
            )
        )
        or 0
    )
    bet_orphan_count = int(
        session.scalar(
            select(func.count())
            .select_from(Bet)
            .outerjoin(Persona, Persona.id == Bet.persona_id)
            .outerjoin(CirclePointAccount, CirclePointAccount.persona_id == Bet.persona_id)
            .outerjoin(GameAccount, GameAccount.id == Bet.game_account_id)
            .where(
                or_(
                    Persona.id.is_(None),
                    CirclePointAccount.id.is_(None),
                    GameAccount.id.is_(None),
                )
            )
        )
        or 0
    )
    bet_ledger_provenance_mismatch_count = int(
        session.scalar(
            select(func.count())
            .select_from(CirclePointTransaction)
            .outerjoin(Bet, Bet.id == CirclePointTransaction.related_bet_id)
            .where(
                CirclePointTransaction.related_bet_id.is_not(None),
                or_(
                    Bet.id.is_(None),
                    Bet.persona_id != CirclePointTransaction.persona_id,
                    Bet.game_account_id != CirclePointTransaction.game_account_id,
                ),
            )
        )
        or 0
    )
    historical_bet_owner_audit_mismatch_count = count_historical_bet_owner_audit_mismatches(session)
    provenance_error_count = (
        transaction_orphan_count
        + bet_orphan_count
        + bet_ledger_provenance_mismatch_count
        + historical_bet_owner_audit_mismatch_count
    )
    if provenance_error_count:
        errors.append("point_transaction_owner_mismatch")

    active_persona_ids = set(session.scalars(select(Persona.id).where(Persona.status == "active")))
    expected_population = manifest.expected_source_identity_count + expected_registration_count
    population_counts = (
        int(session.scalar(select(func.count()).select_from(DiscordAccount)) or 0),
        int(session.scalar(select(func.count()).select_from(Persona)) or 0),
        len(wallets),
    )
    if population_counts != (expected_population,) * 3 or active_persona_ids != participant_persona_ids:
        errors.append("identity_population_mismatch")
    return (
        {
            "wallet_total": wallet_total,
            "transaction_total": transaction_total,
            "opening_total": sum(transaction.amount for transaction in baseline_transactions),
            "registration_total": sum(transaction.amount for transaction in registration_transactions),
            "win5_reward_total": sum(transaction.amount for transaction in reward_transactions),
            "transaction_count": len(transactions),
            "win5_reward_transaction_count": len(reward_transactions),
            "owner_mismatch_count": provenance_error_count,
            "transaction_orphan_count": transaction_orphan_count,
            "bet_orphan_count": bet_orphan_count,
            "bet_ledger_provenance_mismatch_count": bet_ledger_provenance_mismatch_count,
            "historical_bet_owner_audit_mismatch_count": historical_bet_owner_audit_mismatch_count,
        },
        tuple(errors),
    )
