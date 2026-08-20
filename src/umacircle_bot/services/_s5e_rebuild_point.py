from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Bet,
    BetJudgement,
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    Persona,
    RaceResult,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.circle_point_provenance import (
    ACCOUNT_REGISTRATION_APPROVAL_TRANSACTION_SOURCE,
    ACCOUNT_REGISTRATION_INITIAL_GRANT_TRANSACTION_TYPE,
)
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.services._historical_placement_prefix import _e2_prefix_state_checksum
from umacircle_bot.services._s5e_rebuild_provenance import build_registration_provenance_checksum
from umacircle_bot.services.historical_placement_correction import (
    HISTORICAL_PLACEMENT_CORRECTION_IMPORT_KIND,
    HISTORICAL_PLACEMENT_CORRECTION_RECORD_TYPE,
    HISTORICAL_PLACEMENT_TRANSACTION_SOURCE,
    HISTORICAL_PLACEMENT_TRANSACTION_TYPE,
)
from umacircle_bot.services.legacy_import import LEGACY_IDENTITY_POINT_RECORD_TYPE
from umacircle_bot.services.legacy_ledger_import import LEDGER_TRANSACTION_SOURCE, LEGACY_LEDGER_IMPORT_KIND
from umacircle_bot.services.legacy_payout_correction import (
    LEGACY_PAYOUT_CORRECTION_IMPORT_KIND,
    LEGACY_PAYOUT_CORRECTION_SOURCE,
    LEGACY_PAYOUT_CORRECTION_TRANSACTION_TYPE,
)
from umacircle_bot.sheets.current_win5_replay_manifest import CurrentWin5ReplayManifest
from umacircle_bot.sheets.s5e_rebuild_manifest import S5EParticipantDelta, S5ERebuildManifest, canonical_checksum

_PHASE_A_LEDGER_TYPES = frozenset({"legacy_opening_balance", "admin_grant", "bet_stake", "settlement_reward"})


def inspect_point_phases(
    session: Session,
    *,
    manifest: S5ERebuildManifest,
    win5_manifest: CurrentWin5ReplayManifest,
    participant_accounts: Mapping[str, GameAccount],
    include_phase_d: bool,
    rating_signature: str | None,
    score_reward_checksum: str,
) -> tuple[dict[str, object], dict[str, object], tuple[str, ...], str | None]:
    errors: list[str] = []
    transactions = tuple(session.scalars(select(CirclePointTransaction).order_by(CirclePointTransaction.id)))
    phase_rows: dict[str, list[CirclePointTransaction]] = {name: [] for name in "ABCD"}
    for row in transactions:
        phase = transaction_phase(row)
        if phase is None:
            errors.append("unclassified_point_transaction")
            continue
        phase_rows[phase].append(row)
    if not include_phase_d and phase_rows["D"]:
        errors.append("win5_reward_present_before_phase_d")

    provenance_errors = point_provenance_errors(session, transactions=transactions)
    errors.extend(provenance_errors)
    phase_a_report, phase_a_errors = _phase_a_report(session, manifest=manifest, rows=phase_rows["A"])
    phase_b_report, phase_b_errors = _phase_b_report(session, manifest=manifest, rows=phase_rows["B"])
    phase_c_report, phase_c_errors = _phase_c_report(
        session, manifest=manifest, win5_manifest=win5_manifest, rows=phase_rows["C"]
    )
    phase_d_report, phase_d_errors = _phase_d_report(
        manifest=manifest,
        win5_manifest=win5_manifest,
        participant_accounts=participant_accounts,
        rows=phase_rows["D"],
        required=include_phase_d,
    )
    errors.extend((*phase_a_errors, *phase_b_errors, *phase_c_errors, *phase_d_errors))
    phase_reports = {"A": phase_a_report, "B": phase_b_report, "C": phase_c_report, "D": phase_d_report}

    wallets = tuple(session.scalars(select(CirclePointAccount).order_by(CirclePointAccount.persona_id)))
    transaction_totals: dict[str, int] = defaultdict(int)
    for row in transactions:
        transaction_totals[row.persona_id] += row.amount
    mismatched_wallet_count = sum(
        wallet.balance != transaction_totals.pop(wallet.persona_id, 0) for wallet in wallets
    ) + len(transaction_totals)
    wallet_total = sum(row.balance for row in wallets)
    transaction_total = sum(row.amount for row in transactions)
    expected_total = (
        manifest.phase_d.expected_cumulative_total if include_phase_d else manifest.phase_c.expected_cumulative_total
    )
    if wallet_total != expected_total or transaction_total != expected_total or mismatched_wallet_count:
        errors.append("wallet_ledger_reconciliation_mismatch")
    point_report = {
        "wallet_total": wallet_total,
        "transaction_total": transaction_total,
        "transaction_count": len(transactions),
        "win5_reward_total": sum(row.amount for row in phase_rows["D"]),
        "win5_reward_transaction_count": len(phase_rows["D"]),
        "mismatched_wallet_count": mismatched_wallet_count,
        "unclassified_transaction_count": sum(transaction_phase(row) is None for row in transactions),
        "owner_mismatch_count": len(provenance_errors),
    }
    business_signature = None
    if not errors and rating_signature is not None:
        business_signature = canonical_checksum(
            {
                "manifest_checksum": manifest.manifest_checksum,
                "phase_reports": phase_reports,
                "point_report": point_report,
                "rating_signature_checksum": rating_signature,
                "win5_manifest_checksum": win5_manifest.manifest_checksum,
                "score_reward_checksum": score_reward_checksum,
            }
        )
    return phase_reports, point_report, tuple(dict.fromkeys(errors)), business_signature


def _phase_a_report(
    session: Session,
    *,
    manifest: S5ERebuildManifest,
    rows: Sequence[CirclePointTransaction],
) -> tuple[dict[str, object], tuple[str, ...]]:
    errors: list[str] = []
    source = manifest.source_lineage.source_identifier
    checksum = manifest.source_lineage.latest_workbook_checksum
    runs = tuple(
        session.scalars(
            select(SheetImportRun).where(
                SheetImportRun.import_kind.in_((LEGACY_LEDGER_IMPORT_KIND, LEGACY_PAYOUT_CORRECTION_IMPORT_KIND))
            )
        )
    )
    if (
        len(runs) != 2
        or {row.import_kind for row in runs} != {LEGACY_LEDGER_IMPORT_KIND, LEGACY_PAYOUT_CORRECTION_IMPORT_KIND}
        or any(
            row.source_identifier != source
            or row.source_checksum != checksum
            or row.status != "completed"
            or row.finished_at is None
            for row in runs
        )
    ):
        errors.append("e2_import_provenance_mismatch")
    if (
        len(rows) != manifest.phase_a.transaction_count
        or sum(row.amount for row in rows) != manifest.phase_a.expected_delta
    ):
        errors.append("phase_a_total_mismatch")
    if int(session.scalar(select(func.count()).select_from(Bet)) or 0) != manifest.phase_a.bet_count:
        errors.append("phase_a_bet_count_mismatch")
    if int(session.scalar(select(func.count()).select_from(BetJudgement)) or 0) != manifest.phase_a.judgement_count:
        errors.append("phase_a_judgement_count_mismatch")

    expected = {row.wallet_source_key: row.delta for row in manifest.phase_a.wallet_deltas}
    actual: dict[str, int] = {}
    covered_persona_ids: set[str] = set()
    for source_key in sorted(expected):
        records = tuple(
            session.scalars(
                select(SheetImportRecord).where(
                    SheetImportRecord.source_key == source_key,
                    SheetImportRecord.record_type == LEGACY_IDENTITY_POINT_RECORD_TYPE,
                    SheetImportRecord.status == "applied",
                )
            )
        )
        account = (
            session.get(GameAccount, records[0].target_entity_id)
            if len(records) == 1 and records[0].target_entity_id is not None
            else None
        )
        if account is None or account.persona_id is None or account.persona_id in covered_persona_ids:
            errors.append("phase_a_wallet_source_resolution_mismatch")
            continue
        covered_persona_ids.add(account.persona_id)
        actual[source_key] = sum(row.amount for row in rows if row.persona_id == account.persona_id)
    if actual != expected or {row.persona_id for row in rows} != covered_persona_ids:
        errors.append("phase_a_wallet_vector_mismatch")
    return (
        {
            "transaction_count": len(rows),
            "delta": sum(row.amount for row in rows),
            "cumulative_total": manifest.phase_a.expected_cumulative_total,
            "wallet_deltas": [{"wallet_source_key": key, "delta": actual[key]} for key in sorted(actual)],
        },
        tuple(dict.fromkeys(errors)),
    )


def _phase_b_report(
    session: Session,
    *,
    manifest: S5ERebuildManifest,
    rows: Sequence[CirclePointTransaction],
) -> tuple[dict[str, object], tuple[str, ...]]:
    errors: list[str] = []
    runs = tuple(
        session.scalars(
            select(SheetImportRun).where(
                SheetImportRun.import_kind == HISTORICAL_PLACEMENT_CORRECTION_IMPORT_KIND,
                SheetImportRun.source_identifier == manifest.source_lineage.source_identifier,
            )
        )
    )
    run = runs[0] if len(runs) == 1 else None
    summary = run.summary_json if run is not None and isinstance(run.summary_json, dict) else {}
    stored_manifest = summary.get("manifest") if isinstance(summary.get("manifest"), dict) else {}
    if (
        run is None
        or run.source_checksum != manifest.source_lineage.latest_workbook_checksum
        or run.status != "completed"
        or run.finished_at is None
        or summary.get("manifest_checksum") != manifest.phase_b.manifest_checksum
        or stored_manifest.get("target_attribution_checksum") != manifest.phase_b.target_attribution_checksum
        or stored_manifest.get("reward_policy_checksum") != manifest.phase_b.reward_policy_checksum
        or stored_manifest.get("mapping_checksum") != manifest.reviewed_artifacts.mapping_checksum
        or stored_manifest.get("mapping_decision_checksum") != manifest.reviewed_artifacts.mapping_decision_checksum
        or stored_manifest.get("e2_business_key_prefix_checksum") != manifest.phase_a.business_key_prefix_checksum
        or stored_manifest.get("expected_total") != manifest.phase_b.expected_delta
    ):
        errors.append("e3_manifest_provenance_mismatch")
    if run is not None:
        try:
            live_prefix_checksum = _e2_prefix_state_checksum(
                session,
                source_identifier=manifest.source_lineage.source_identifier,
                source_checksum=manifest.source_lineage.latest_workbook_checksum,
                lock_rows=False,
            )
            if live_prefix_checksum != summary.get("e2_prefix_state_checksum"):
                errors.append("e2_prefix_state_changed")
        except LegacyImportError:
            errors.append("e2_prefix_state_changed")

    records = (
        tuple(
            session.scalars(
                select(SheetImportRecord)
                .where(
                    SheetImportRecord.import_run_id == run.id,
                    SheetImportRecord.record_type == HISTORICAL_PLACEMENT_CORRECTION_RECORD_TYPE,
                    SheetImportRecord.status == "applied",
                )
                .order_by(SheetImportRecord.source_row_number)
            )
        )
        if run is not None
        else ()
    )
    transactions_by_id = {row.id: row for row in rows}
    actual: dict[str, int] = defaultdict(int)
    covered_transaction_ids: set[int] = set()
    business_rows: list[dict[str, object]] = []
    for record in records:
        detail = record.detail_json if isinstance(record.detail_json, dict) else {}
        transaction = transactions_by_id.get(record.target_entity_id)
        owner_key = detail.get("owner_business_key")
        if transaction is None or not isinstance(owner_key, str):
            errors.append("phase_b_record_provenance_mismatch")
            continue
        actual[owner_key] += transaction.amount
        covered_transaction_ids.add(transaction.id)
        business_rows.append(
            {
                "result_business_key": detail.get("result_business_key"),
                "owner_business_key": owner_key,
                "game_account_business_key": detail.get("game_account_business_key"),
                "amount": transaction.amount,
                "suppressed_result_business_keys": detail.get("suppressed_result_business_keys"),
            }
        )
    expected = {row.owner_business_key: row.delta for row in manifest.phase_b.owner_deltas}
    if (
        dict(actual) != expected
        or covered_transaction_ids != set(transactions_by_id)
        or len(rows) != manifest.phase_b.transaction_count
        or sum(row.amount for row in rows) != manifest.phase_b.expected_delta
    ):
        errors.append("phase_b_owner_vector_mismatch")
    return (
        {
            "transaction_count": len(rows),
            "delta": sum(row.amount for row in rows),
            "cumulative_total": manifest.phase_b.expected_cumulative_total,
            "owner_deltas": [{"owner_business_key": key, "delta": actual[key]} for key in sorted(actual)],
            "business_rows": sorted(business_rows, key=lambda row: str(row["result_business_key"])),
        },
        tuple(dict.fromkeys(errors)),
    )


def _phase_c_report(
    session: Session,
    *,
    manifest: S5ERebuildManifest,
    win5_manifest: CurrentWin5ReplayManifest,
    rows: Sequence[CirclePointTransaction],
) -> tuple[dict[str, object], tuple[str, ...]]:
    errors: list[str] = []
    try:
        provenance_checksum = build_registration_provenance_checksum(session, manifest=win5_manifest)
    except LegacyImportError:
        provenance_checksum = None
        errors.append("registration_provenance_mismatch")
    if provenance_checksum != manifest.phase_c.provenance_checksum:
        errors.append("registration_provenance_checksum_mismatch")
    actual, vector_errors = _participant_delta_vector(
        session,
        expected=manifest.phase_c.participant_deltas,
        rows=rows,
    )
    errors.extend(vector_errors)
    if (
        len(rows) != manifest.phase_c.transaction_count
        or sum(row.amount for row in rows) != manifest.phase_c.expected_delta
    ):
        errors.append("phase_c_total_mismatch")
    return (
        {
            "transaction_count": len(rows),
            "delta": sum(row.amount for row in rows),
            "cumulative_total": manifest.phase_c.expected_cumulative_total,
            "participant_deltas": actual,
            "provenance_checksum": provenance_checksum,
        },
        tuple(dict.fromkeys(errors)),
    )


def _phase_d_report(
    *,
    manifest: S5ERebuildManifest,
    win5_manifest: CurrentWin5ReplayManifest,
    participant_accounts: Mapping[str, GameAccount],
    rows: Sequence[CirclePointTransaction],
    required: bool,
) -> tuple[dict[str, object], tuple[str, ...]]:
    errors: list[str] = []
    actual: list[dict[str, object]] = []
    expected_by_discord = {row.discord_user_id: row for row in manifest.phase_d.participant_deltas}
    covered: set[int] = set()
    for discord_user_id in sorted(expected_by_discord):
        expected = expected_by_discord[discord_user_id]
        account = participant_accounts.get(discord_user_id)
        participant_rows = [row for row in rows if account is not None and row.game_account_id == account.id]
        amount = sum(row.amount for row in participant_rows)
        actual.append({"discord_user_id": discord_user_id, "uma_pid": expected.uma_pid, "delta": amount})
        covered.update(row.id for row in participant_rows)
        if account is None or account.uma_pid != expected.uma_pid or (required and amount != expected.delta):
            errors.append("phase_d_participant_vector_mismatch")
    if required:
        if (
            covered != {row.id for row in rows}
            or len(rows) != manifest.phase_d.transaction_count
            or sum(row.amount for row in rows) != manifest.phase_d.expected_delta
        ):
            errors.append("phase_d_total_mismatch")
    elif rows:
        errors.append("phase_d_present_before_replay")
    return (
        {
            "transaction_count": len(rows),
            "delta": sum(row.amount for row in rows),
            "cumulative_total": (
                manifest.phase_d.expected_cumulative_total if required else manifest.phase_c.expected_cumulative_total
            ),
            "participant_deltas": actual if required else [],
            "win5_manifest_checksum": win5_manifest.manifest_checksum,
        },
        tuple(dict.fromkeys(errors)),
    )


def _participant_delta_vector(
    session: Session,
    *,
    expected: Sequence[S5EParticipantDelta],
    rows: Sequence[CirclePointTransaction],
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    errors: list[str] = []
    output: list[dict[str, object]] = []
    covered: set[int] = set()
    for item in sorted(expected, key=lambda row: row.discord_user_id):
        discord = session.scalar(select(DiscordAccount).where(DiscordAccount.discord_user_id == item.discord_user_id))
        accounts = (
            tuple(
                session.scalars(
                    select(GameAccount).where(
                        GameAccount.persona_id == discord.persona_id,
                        GameAccount.uma_pid == item.uma_pid,
                    )
                )
            )
            if discord is not None and discord.persona_id is not None
            else ()
        )
        account = accounts[0] if len(accounts) == 1 else None
        matches = [
            row
            for row in rows
            if account is not None and row.persona_id == account.persona_id and row.game_account_id == account.id
        ]
        amount = sum(row.amount for row in matches)
        output.append({"discord_user_id": item.discord_user_id, "uma_pid": item.uma_pid, "delta": amount})
        covered.update(row.id for row in matches)
        if account is None or amount != item.delta:
            errors.append("participant_delta_vector_mismatch")
    if covered != {row.id for row in rows}:
        errors.append("participant_delta_vector_coverage_mismatch")
    return output, tuple(dict.fromkeys(errors))


def point_provenance_errors(
    session: Session,
    *,
    transactions: Sequence[CirclePointTransaction],
) -> tuple[str, ...]:
    errors: list[str] = []
    for row in transactions:
        persona = session.get(Persona, row.persona_id)
        wallet = session.scalar(select(CirclePointAccount).where(CirclePointAccount.persona_id == row.persona_id))
        account = session.get(GameAccount, row.game_account_id)
        if persona is None or wallet is None or account is None:
            errors.append("point_owner_missing")
            continue
        phase = transaction_phase(row)
        if phase == "A" and row.related_bet_id is not None:
            bet = session.get(Bet, row.related_bet_id)
            if bet is None or bet.persona_id != row.persona_id or bet.game_account_id != row.game_account_id:
                errors.append("phase_a_bet_provenance_mismatch")
        elif phase == "B":
            result = session.get(RaceResult, row.related_race_result_id)
            if result is None or (
                result.owner_at_event_persona_id != row.persona_id or result.game_account_id != row.game_account_id
            ):
                errors.append("phase_b_result_provenance_mismatch")
        elif phase in {"C", "D"}:
            if account.persona_id != row.persona_id:
                errors.append(f"phase_{phase.lower()}_current_owner_mismatch")
            if row.related_bet_id is not None or row.related_race_result_id is not None:
                errors.append(f"phase_{phase.lower()}_unexpected_relation")
    orphan_count = int(
        session.scalar(
            select(func.count())
            .select_from(CirclePointTransaction)
            .outerjoin(Persona, Persona.id == CirclePointTransaction.persona_id)
            .outerjoin(GameAccount, GameAccount.id == CirclePointTransaction.game_account_id)
            .outerjoin(CirclePointAccount, CirclePointAccount.persona_id == CirclePointTransaction.persona_id)
            .where(or_(Persona.id.is_(None), GameAccount.id.is_(None), CirclePointAccount.id.is_(None)))
        )
        or 0
    )
    if orphan_count:
        errors.append("point_orphan_count")
    return tuple(dict.fromkeys(errors))


def transaction_phase(row: CirclePointTransaction) -> str | None:
    if row.source == LEDGER_TRANSACTION_SOURCE and row.type in _PHASE_A_LEDGER_TYPES:
        return "A"
    if row.source == LEGACY_PAYOUT_CORRECTION_SOURCE and row.type == LEGACY_PAYOUT_CORRECTION_TRANSACTION_TYPE:
        return "A"
    if row.source == HISTORICAL_PLACEMENT_TRANSACTION_SOURCE and row.type == HISTORICAL_PLACEMENT_TRANSACTION_TYPE:
        return "B"
    if (
        row.source == ACCOUNT_REGISTRATION_APPROVAL_TRANSACTION_SOURCE
        and row.type == ACCOUNT_REGISTRATION_INITIAL_GRANT_TRANSACTION_TYPE
    ):
        return "C"
    if row.source == "win5_score" and row.type == "win5_reward":
        return "D"
    return None
