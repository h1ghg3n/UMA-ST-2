from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Bet,
    BetJudgement,
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    Persona,
    Race,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.betting import validate_circle_point_balance
from umacircle_bot.domain.errors import BettingRuleError, LegacyImportConflictError, LegacyImportError
from umacircle_bot.domain.imports import (
    build_import_row_fingerprint,
    build_sheet_import_source_key,
    normalize_import_sheet_name,
    normalize_import_source_identifier,
    normalize_sha256_hex,
)
from umacircle_bot.runtime_preflight import EXPECTED_ROOM_POINT_SCALE
from umacircle_bot.services.legacy_ledger_import import (
    DEFAULT_LEDGER_SHEET_NAME,
    LEGACY_LEDGER_IMPORT_KIND,
    LEGACY_LEDGER_RECORD_TYPE,
    legacy_payout_rate_storage_value,
)
from umacircle_bot.services.legacy_point_scale import (
    LEGACY_ROOM_POINT_AUDIT_CONTRACT_V1,
    LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION,
    classify_legacy_room_point_audit_contract,
    require_current_legacy_room_point_import_target,
    scale_legacy_circle_point_balance,
    scale_legacy_room_point_amount,
)
from umacircle_bot.services.legacy_race_import import LEGACY_RACE_IMPORT_KIND, LEGACY_RACE_RECORD_TYPE
from umacircle_bot.sheets.legacy_room_report import LegacyRoomDryRunReport

LEGACY_PAYOUT_CORRECTION_IMPORT_KIND = "legacy_room_payout_correction"
LEGACY_PAYOUT_CORRECTION_RECORD_TYPE = "legacy_payout_correction"
LEGACY_PAYOUT_CORRECTION_TRANSACTION_TYPE = "legacy_payout_correction"
LEGACY_PAYOUT_CORRECTION_SOURCE = "legacy_payout_correction"
DERIVED_CORRECTION_SHEET_NAME = "__legacy_payout_correction__"


@dataclass(frozen=True)
class LegacyPayoutCorrectionPlanRow:
    source_row_number: int
    source_key: str
    row_fingerprint: str
    discord_user_id: str
    participant_name: str
    race_label: str
    bet_type: str
    numbers: tuple[int, ...]
    stake_amount: int
    cached_payout_rate: Decimal
    expected_payout_rate: Decimal
    expected_initial_balance: int
    correction_amount: int
    cached_payout_amount: int
    expected_payout_amount: int
    context_fingerprint: str

    @property
    def legacy_row_fingerprint(self) -> str:
        """Return the stable production-v1 source fingerprint."""

        return self.row_fingerprint


@dataclass(frozen=True)
class LegacyPayoutCorrectionPlan:
    source_identifier: str
    correction_sheet_name: str
    rows: tuple[LegacyPayoutCorrectionPlanRow, ...]

    @property
    def correction_total(self) -> int:
        return sum(row.correction_amount for row in self.rows)


@dataclass(frozen=True)
class LegacyPayoutCorrectionConflict:
    source_row_number: int
    code: str
    message: str


@dataclass(frozen=True)
class LegacyPayoutCorrectionPreview:
    new_count: int
    skipped_count: int
    correction_total: int
    conflicts: tuple[LegacyPayoutCorrectionConflict, ...]

    @property
    def can_apply(self) -> bool:
        return not self.conflicts

    @property
    def source_correction_total(self) -> int:
        return self.correction_total

    @property
    def target_correction_total(self) -> int:
        return self.correction_total * EXPECTED_ROOM_POINT_SCALE


@dataclass(frozen=True)
class LegacyPayoutCorrectionResult:
    import_run_id: int
    created_count: int
    skipped_count: int
    correction_total: int

    @property
    def source_correction_total(self) -> int:
        return self.correction_total

    @property
    def target_correction_total(self) -> int:
        return self.correction_total * EXPECTED_ROOM_POINT_SCALE


@dataclass(frozen=True)
class _CorrectionContext:
    accounts_by_source_row: dict[int, tuple[GameAccount, CirclePointAccount]]
    bets_by_source_row: dict[int, Bet]


def build_legacy_payout_correction_plan(
    report: LegacyRoomDryRunReport,
    *,
    source_identifier: str,
    correction_sheet_name: str = DERIVED_CORRECTION_SHEET_NAME,
) -> LegacyPayoutCorrectionPlan:
    normalized_source = normalize_import_source_identifier(source_identifier)
    normalized_sheet = normalize_import_sheet_name(correction_sheet_name)
    if report.has_errors:
        raise LegacyImportError("legacy payout correction report contains errors")
    if not report.payout_mismatches:
        raise LegacyImportError("legacy payout correction report contains no mismatches")

    participant_snapshots = _participant_snapshots(report.payout_correction_plan)
    rows: list[LegacyPayoutCorrectionPlanRow] = []
    seen_row_numbers: set[int] = set()
    actual_totals: dict[str, int] = {}
    for mismatch in report.payout_mismatches:
        row_number = _required_positive_int(mismatch, "row_number")
        participant_name = _required_text(mismatch, "participant_name")
        correction_amount = _required_int(mismatch, "payout_shortfall")
        if correction_amount == 0:
            raise LegacyImportError(f"payout mismatch at row {row_number} has no correction amount")
        if row_number in seen_row_numbers:
            raise LegacyImportError(f"duplicate payout mismatch at row {row_number}")
        snapshot = participant_snapshots.get(participant_name)
        if snapshot is None:
            raise LegacyImportError(f"payout mismatch has no point account snapshot: {participant_name}")
        discord_user_id, expected_initial_balance, expected_total = snapshot
        cached_payout_amount = _required_nonnegative_int(mismatch, "cached_payout_amount")
        expected_payout_amount = _required_nonnegative_int(mismatch, "expected_payout_amount")
        race_label = _required_text(mismatch, "race_label")
        bet_type = _required_text(mismatch, "bet_type")
        numbers = _required_numbers(mismatch, "numbers")
        stake_amount = _required_positive_int(mismatch, "amount")
        cached_payout_rate = _required_decimal(mismatch, "cached_payout_rate")
        expected_payout_rate = _required_decimal(mismatch, "expected_payout_rate")
        source_key = build_sheet_import_source_key(
            source_identifier=normalized_source,
            sheet_name=normalized_sheet,
            row_number=row_number,
        )
        stable_row_fingerprint = build_import_row_fingerprint(
            (
                discord_user_id,
                participant_name,
                expected_initial_balance,
                correction_amount,
                cached_payout_amount,
                expected_payout_amount,
            )
        )
        rows.append(
            LegacyPayoutCorrectionPlanRow(
                source_row_number=row_number,
                source_key=source_key,
                row_fingerprint=stable_row_fingerprint,
                context_fingerprint=build_import_row_fingerprint(
                    (
                        discord_user_id,
                        participant_name,
                        race_label,
                        bet_type,
                        ",".join(str(number) for number in numbers),
                        stake_amount,
                        str(cached_payout_rate),
                        str(expected_payout_rate),
                        expected_initial_balance,
                        correction_amount,
                        cached_payout_amount,
                        expected_payout_amount,
                    )
                ),
                discord_user_id=discord_user_id,
                participant_name=participant_name,
                race_label=race_label,
                bet_type=bet_type,
                numbers=numbers,
                stake_amount=stake_amount,
                cached_payout_rate=cached_payout_rate,
                expected_payout_rate=expected_payout_rate,
                expected_initial_balance=expected_initial_balance,
                correction_amount=correction_amount,
                cached_payout_amount=cached_payout_amount,
                expected_payout_amount=expected_payout_amount,
            )
        )
        seen_row_numbers.add(row_number)
        actual_totals[participant_name] = actual_totals.get(participant_name, 0) + correction_amount

    for participant_name, (_, _, expected_total) in participant_snapshots.items():
        if actual_totals.get(participant_name, 0) != expected_total:
            raise LegacyImportError(f"payout correction total does not match point snapshot: {participant_name}")
    if sum(actual_totals.values()) != report.payout_correction_total:
        raise LegacyImportError("payout correction total does not match dry-run report")
    plan = LegacyPayoutCorrectionPlan(
        source_identifier=normalized_source,
        correction_sheet_name=normalized_sheet,
        rows=tuple(sorted(rows, key=lambda row: row.source_row_number)),
    )
    _validate_scalable_plan(plan)
    return plan


def preview_legacy_payout_correction(
    session: Session,
    *,
    plan: LegacyPayoutCorrectionPlan,
    source_checksum: str,
) -> LegacyPayoutCorrectionPreview:
    _require_clean_session(session)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    return _inspect_legacy_payout_correction(
        session,
        plan=plan,
        source_checksum=normalized_checksum,
        lock_rows=False,
    )


def apply_legacy_payout_correction(
    session: Session,
    *,
    plan: LegacyPayoutCorrectionPlan,
    source_checksum: str,
) -> LegacyPayoutCorrectionResult:
    _require_clean_session(session)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    with session.begin_nested():
        preview = _inspect_legacy_payout_correction(
            session,
            plan=plan,
            source_checksum=normalized_checksum,
            lock_rows=True,
        )
        if preview.conflicts:
            first = preview.conflicts[0]
            raise LegacyImportConflictError(
                f"legacy payout correction conflict at row {first.source_row_number}: {first.code}"
            )

        records = _load_records(session, plan=plan, lock_rows=True)
        if records:
            import_run = session.get(SheetImportRun, next(iter(records.values())).import_run_id)
            if import_run is None:
                raise LegacyImportConflictError("legacy payout correction retry import run disappeared")
            created_count = 0
            skipped_count = len(plan.rows)
        else:
            import_run = SheetImportRun(
                import_kind=LEGACY_PAYOUT_CORRECTION_IMPORT_KIND,
                source_type="xlsx",
                source_identifier=plan.source_identifier,
                source_checksum=normalized_checksum,
                status="running",
            )
            session.add(import_run)
            session.flush()
            context = _load_correction_context(
                session,
                plan=plan,
                source_checksum=normalized_checksum,
                lock_rows=True,
            )
            for row in plan.rows:
                game_account, point_account = context.accounts_by_source_row[row.source_row_number]
                bet = context.bets_by_source_row[row.source_row_number]
                scaled_correction_amount = scale_legacy_room_point_amount(
                    row.correction_amount,
                    field_name=f"legacy payout correction at source row {row.source_row_number}",
                )
                scaled_cached_payout_amount = scale_legacy_room_point_amount(
                    row.cached_payout_amount,
                    field_name=f"legacy cached payout at source row {row.source_row_number}",
                )
                scaled_expected_payout_amount = scale_legacy_room_point_amount(
                    row.expected_payout_amount,
                    field_name=f"legacy expected payout at source row {row.source_row_number}",
                )
                point_account.balance += scaled_correction_amount
                transaction = CirclePointTransaction(
                    persona_id=bet.persona_id,
                    game_account_id=game_account.id,
                    type=LEGACY_PAYOUT_CORRECTION_TRANSACTION_TYPE,
                    amount=scaled_correction_amount,
                    reason=f"legacy_payout_correction:{row.source_row_number}",
                    source=LEGACY_PAYOUT_CORRECTION_SOURCE,
                    related_bet_id=bet.id,
                )
                session.add(transaction)
                session.flush()
                session.add(
                    SheetImportRecord(
                        import_run_id=import_run.id,
                        source_key=row.source_key,
                        row_fingerprint=row.row_fingerprint,
                        source_sheet_name=plan.correction_sheet_name,
                        source_row_number=row.source_row_number,
                        record_type=LEGACY_PAYOUT_CORRECTION_RECORD_TYPE,
                        status="applied",
                        target_entity_type="room_point_transaction",
                        target_entity_id=transaction.id,
                        detail_json={
                            "audit_contract_version": LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION,
                            "game_account_id": game_account.id,
                            "persona_id": bet.persona_id,
                            "bet_id": bet.id,
                            "participant_name": row.participant_name,
                            "race_label": row.race_label,
                            "bet_type": row.bet_type,
                            "numbers": list(row.numbers),
                            "context_fingerprint": row.context_fingerprint,
                            "room_point_scale": EXPECTED_ROOM_POINT_SCALE,
                            "stake_amount": row.stake_amount,
                            "cached_payout_rate": format(row.cached_payout_rate, "f"),
                            "expected_payout_rate": format(row.expected_payout_rate, "f"),
                            "source_correction_amount": row.correction_amount,
                            "source_cached_payout_amount": row.cached_payout_amount,
                            "source_expected_payout_amount": row.expected_payout_amount,
                            "correction_amount": row.correction_amount,
                            "cached_payout_amount": row.cached_payout_amount,
                            "expected_payout_amount": row.expected_payout_amount,
                            "target_correction_amount": scaled_correction_amount,
                            "target_cached_payout_amount": scaled_cached_payout_amount,
                            "target_expected_payout_amount": scaled_expected_payout_amount,
                        },
                    )
                )
            session.flush()
            post_apply_conflicts = _validate_wallet_ledger_totals(session, plan=plan, context=context)
            if post_apply_conflicts:
                first = post_apply_conflicts[0]
                raise LegacyImportConflictError(
                    f"legacy payout correction reconciliation failed at row {first.source_row_number}: {first.code}"
                )
            created_count = len(plan.rows)
            skipped_count = 0

            import_run.status = "completed"
            import_run.finished_at = datetime.now(UTC)
            import_run.summary_json = {
                "audit_contract_version": LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION,
                "created_count": created_count,
                "skipped_count": skipped_count,
                "correction_total": plan.correction_total,
                "source_correction_total": plan.correction_total,
                "target_correction_total": plan.correction_total * EXPECTED_ROOM_POINT_SCALE,
                "room_point_scale": EXPECTED_ROOM_POINT_SCALE,
            }
            session.flush()
    return LegacyPayoutCorrectionResult(
        import_run_id=import_run.id,
        created_count=created_count,
        skipped_count=skipped_count,
        correction_total=plan.correction_total,
    )


def _inspect_legacy_payout_correction(
    session: Session,
    *,
    plan: LegacyPayoutCorrectionPlan,
    source_checksum: str,
    lock_rows: bool,
) -> LegacyPayoutCorrectionPreview:
    require_current_legacy_room_point_import_target(session)
    records = _load_records(session, plan=plan, lock_rows=lock_rows)
    if records and len(records) != len(plan.rows):
        missing = next(row for row in plan.rows if row.source_key not in records)
        return _preview(
            plan,
            new_count=0,
            skipped_count=len(records),
            conflicts=(
                _conflict(missing, "partial_correction_state", "payout corrections are only partially present"),
            ),
        )
    if len({record.import_run_id for record in records.values()}) > 1:
        return _preview(
            plan,
            new_count=0,
            skipped_count=len(records),
            conflicts=(
                _conflict(
                    plan.rows[0],
                    "import_run_ambiguous",
                    "payout correction source records have multiple parent runs",
                ),
            ),
        )
    context, prerequisite_conflicts = _load_correction_context_with_conflicts(
        session,
        plan=plan,
        source_checksum=source_checksum,
        lock_rows=lock_rows,
    )
    if records:
        import_runs = _load_import_runs(session, records=records.values(), lock_rows=lock_rows)
        conflicts = list(prerequisite_conflicts)
        if not conflicts:
            conflicts.extend(
                _verify_existing_records(
                    session,
                    plan=plan,
                    source_checksum=source_checksum,
                    records=records,
                    import_runs=import_runs,
                    context=context,
                )
            )
        conflicts.extend(_validate_wallet_ledger_totals(session, plan=plan, context=context))
        return _preview(
            plan,
            new_count=0,
            skipped_count=len(records),
            conflicts=tuple(conflicts),
        )

    conflicts = list(prerequisite_conflicts)
    if not conflicts:
        conflicts.extend(_validate_initial_state(session, plan=plan, context=context))
    return _preview(
        plan,
        new_count=len(plan.rows) if not conflicts else 0,
        skipped_count=0,
        conflicts=tuple(conflicts),
    )


def _load_records(
    session: Session,
    *,
    plan: LegacyPayoutCorrectionPlan,
    lock_rows: bool,
) -> dict[str, SheetImportRecord]:
    statement = select(SheetImportRecord).where(SheetImportRecord.source_key.in_([row.source_key for row in plan.rows]))
    if lock_rows:
        statement = statement.with_for_update()
    return {record.source_key: record for record in session.scalars(statement)}


def _load_import_runs(
    session: Session,
    *,
    records: Iterable[SheetImportRecord],
    lock_rows: bool,
) -> dict[int, SheetImportRun]:
    import_run_ids = sorted({record.import_run_id for record in records})
    if not import_run_ids:
        return {}
    statement = select(SheetImportRun).where(SheetImportRun.id.in_(import_run_ids)).order_by(SheetImportRun.id)
    if lock_rows:
        statement = statement.with_for_update()
    return {import_run.id: import_run for import_run in session.scalars(statement)}


def _load_correction_context(
    session: Session,
    *,
    plan: LegacyPayoutCorrectionPlan,
    source_checksum: str,
    lock_rows: bool,
) -> _CorrectionContext:
    context, conflicts = _load_correction_context_with_conflicts(
        session,
        plan=plan,
        source_checksum=source_checksum,
        lock_rows=lock_rows,
    )
    if conflicts:
        raise LegacyImportConflictError("legacy payout correction prerequisites changed")
    return context


def _load_correction_context_with_conflicts(
    session: Session,
    *,
    plan: LegacyPayoutCorrectionPlan,
    source_checksum: str,
    lock_rows: bool,
) -> tuple[_CorrectionContext, tuple[LegacyPayoutCorrectionConflict, ...]]:
    conflicts: list[LegacyPayoutCorrectionConflict] = []
    ledger_keys = [
        build_sheet_import_source_key(
            source_identifier=plan.source_identifier,
            sheet_name=DEFAULT_LEDGER_SHEET_NAME,
            row_number=row.source_row_number,
        )
        for row in plan.rows
    ]
    ledger_statement = select(SheetImportRecord).where(SheetImportRecord.source_key.in_(ledger_keys))
    if lock_rows:
        ledger_statement = ledger_statement.with_for_update()
    ledger_records = {record.source_key: record for record in session.scalars(ledger_statement)}
    ledger_runs = _load_import_runs(session, records=ledger_records.values(), lock_rows=lock_rows)

    accounts: dict[tuple[int, str], tuple[GameAccount, CirclePointAccount]] = {}
    accounts_by_source_row: dict[int, tuple[GameAccount, CirclePointAccount]] = {}
    bets_by_source_row: dict[int, Bet] = {}
    validated_race_labels: dict[int, str] = {}
    for row, ledger_key in zip(plan.rows, ledger_keys, strict=True):
        ledger_record = ledger_records.get(ledger_key)
        if (
            ledger_record is None
            or ledger_record.record_type != LEGACY_LEDGER_RECORD_TYPE
            or ledger_record.status != "applied"
            or ledger_record.target_entity_type != "bet"
            or ledger_record.target_entity_id is None
            or ledger_record.source_sheet_name != DEFAULT_LEDGER_SHEET_NAME
            or ledger_record.source_row_number != row.source_row_number
        ):
            conflicts.append(_conflict(row, "ledger_import_missing", "required ledger import is missing"))
            continue
        ledger_run = ledger_runs.get(ledger_record.import_run_id)
        if (
            ledger_run is None
            or ledger_run.import_kind != LEGACY_LEDGER_IMPORT_KIND
            or ledger_run.source_type != "xlsx"
            or ledger_run.source_identifier != plan.source_identifier
            or ledger_run.source_checksum != source_checksum
            or ledger_run.status != "completed"
            or ledger_run.finished_at is None
        ):
            conflicts.append(_conflict(row, "ledger_import_changed", "required ledger provenance changed"))
            continue
        bet_statement = select(Bet).where(Bet.id == ledger_record.target_entity_id)
        if lock_rows:
            bet_statement = bet_statement.with_for_update()
        bet = session.scalar(bet_statement)
        if bet is None:
            conflicts.append(_conflict(row, "ledger_bet_changed", "required ledger Bet changed"))
            continue
        account_key = (bet.game_account_id, bet.persona_id)
        account = accounts.get(account_key)
        if account is None:
            identity_statement = (
                select(GameAccount, DiscordAccount)
                .select_from(GameAccount)
                .join(DiscordAccount, GameAccount.discord_account_id == DiscordAccount.id)
                .where(GameAccount.id == bet.game_account_id)
            )
            owner_statement = (
                select(CirclePointAccount, Persona)
                .select_from(CirclePointAccount)
                .join(Persona, Persona.id == CirclePointAccount.persona_id)
                .where(CirclePointAccount.persona_id == bet.persona_id)
            )
            if lock_rows:
                owner_statement = owner_statement.with_for_update()
            owner = session.execute(owner_statement).one_or_none()
            # Link-console mutations change Persona attachments, not the
            # immutable GameAccount/Discord provenance fields used here.
            # Locking those rows would invert its Persona -> account order.
            identity = session.execute(identity_statement).one_or_none()
            if identity is None or owner is None:
                conflicts.append(_conflict(row, "point_account_missing", "required point account is missing"))
                continue
            game_account, discord_account = identity
            point_account, persona = owner
            if (
                game_account.nickname != row.participant_name
                or discord_account.discord_user_id != row.discord_user_id
                or bet.persona_id != persona.id
                or point_account.persona_id != persona.id
            ):
                conflicts.append(_conflict(row, "account_identity_changed", "participant identity no longer matches"))
                continue
            account = (game_account, point_account)
            accounts[account_key] = account
        game_account, _ = account
        if (
            bet.game_account_id != game_account.id
            or bet.betting_mode != "room_match"
            or bet.bet_type != row.bet_type
            or tuple(bet.numbers) != row.numbers
            or bet.amount
            != scale_legacy_room_point_amount(
                row.stake_amount,
                field_name=f"legacy correction stake at source row {row.source_row_number}",
            )
            or bet.status != "settled"
        ):
            conflicts.append(_conflict(row, "ledger_bet_changed", "required ledger Bet changed"))
            continue
        validated_label = validated_race_labels.get(bet.race_id)
        if validated_label is None:
            race_statement = select(Race).where(Race.id == bet.race_id)
            if lock_rows:
                race_statement = race_statement.with_for_update()
            race = session.scalar(race_statement)
            race_record_statement = select(SheetImportRecord).where(
                SheetImportRecord.record_type == LEGACY_RACE_RECORD_TYPE,
                SheetImportRecord.status == "applied",
                SheetImportRecord.target_entity_type == "race",
                SheetImportRecord.target_entity_id == bet.race_id,
            )
            if lock_rows:
                race_record_statement = race_record_statement.with_for_update()
            race_records = tuple(session.scalars(race_record_statement))
            race_runs = _load_import_runs(session, records=race_records, lock_rows=lock_rows)
            race_record = race_records[0] if len(race_records) == 1 else None
            race_run = race_runs.get(race_record.import_run_id) if race_record is not None else None
            race_detail = (
                race_record.detail_json if race_record is not None and isinstance(race_record.detail_json, dict) else {}
            )
            if (
                race is None
                or race.external_source != plan.source_identifier
                or race_record is None
                or race_run is None
                or race_run.import_kind != LEGACY_RACE_IMPORT_KIND
                or race_run.source_type != "xlsx"
                or race_run.source_identifier != plan.source_identifier
                or race_run.source_checksum != source_checksum
                or race_run.status != "completed"
                or race_run.finished_at is None
                or race_detail.get("race_label") != row.race_label
            ):
                conflicts.append(_conflict(row, "ledger_race_changed", "required ledger Race changed"))
                continue
            validated_label = row.race_label
            validated_race_labels[bet.race_id] = validated_label
        if validated_label != row.race_label:
            conflicts.append(_conflict(row, "ledger_race_changed", "required ledger Race changed"))
            continue
        judgement_statement = select(BetJudgement).where(BetJudgement.bet_id == bet.id)
        if lock_rows:
            judgement_statement = judgement_statement.with_for_update()
        judgements = tuple(session.scalars(judgement_statement))
        scaled_cached_payout = scale_legacy_room_point_amount(
            row.cached_payout_amount,
            field_name=f"legacy cached payout at source row {row.source_row_number}",
        )
        if len(judgements) != 1:
            conflicts.append(_conflict(row, "ledger_judgement_changed", "required ledger judgement changed"))
            continue
        judgement = judgements[0]
        expected_is_hit = row.cached_payout_amount > 0
        if (
            judgement.race_id != bet.race_id
            or judgement.judgement_status != ("hit" if expected_is_hit else "miss")
            or judgement.is_hit is not expected_is_hit
            or judgement.payout_rate != legacy_payout_rate_storage_value(row.cached_payout_rate)
            or judgement.stake_amount != bet.amount
            or judgement.payout_amount != scaled_cached_payout
            or judgement.point_delta != scaled_cached_payout
        ):
            conflicts.append(_conflict(row, "ledger_judgement_changed", "required ledger judgement changed"))
            continue
        accounts_by_source_row[row.source_row_number] = account
        bets_by_source_row[row.source_row_number] = bet
    return _CorrectionContext(accounts_by_source_row, bets_by_source_row), tuple(conflicts)


def _validate_initial_state(
    session: Session,
    *,
    plan: LegacyPayoutCorrectionPlan,
    context: _CorrectionContext,
) -> tuple[LegacyPayoutCorrectionConflict, ...]:
    conflicts: list[LegacyPayoutCorrectionConflict] = []
    checked_personas: set[str] = set()
    for row in plan.rows:
        account = context.accounts_by_source_row.get(row.source_row_number)
        if account is None:
            continue
        persona_id = account[1].persona_id
        if persona_id in checked_personas:
            continue
        expected_initial_balance = scale_legacy_circle_point_balance(
            row.expected_initial_balance,
            field_name=f"legacy payout initial balance at source row {row.source_row_number}",
        )
        if account[1].balance != expected_initial_balance:
            conflicts.append(
                _conflict(row, "point_balance_changed", "point account no longer matches the source snapshot")
            )
        checked_personas.add(persona_id)
    projected_balances: dict[str, int] = {
        point_account.persona_id: point_account.balance for _, point_account in context.accounts_by_source_row.values()
    }
    for row in plan.rows:
        account = context.accounts_by_source_row.get(row.source_row_number)
        if account is None:
            continue
        persona_id = account[1].persona_id
        projected_balances[persona_id] += scale_legacy_room_point_amount(
            row.correction_amount,
            field_name=f"legacy payout correction at source row {row.source_row_number}",
        )
        try:
            validate_circle_point_balance(projected_balances[persona_id])
        except BettingRuleError:
            conflicts.append(
                _conflict(row, "point_balance_out_of_range", "payout correction would exceed the point range")
            )
    conflicts.extend(_validate_wallet_ledger_totals(session, plan=plan, context=context))
    return tuple(conflicts)


def _verify_existing_records(
    session: Session,
    *,
    plan: LegacyPayoutCorrectionPlan,
    source_checksum: str,
    records: dict[str, SheetImportRecord],
    import_runs: dict[int, SheetImportRun],
    context: _CorrectionContext,
) -> tuple[LegacyPayoutCorrectionConflict, ...]:
    conflicts: list[LegacyPayoutCorrectionConflict] = []
    for row in plan.rows:
        record = records[row.source_key]
        detail = record.detail_json if isinstance(record.detail_json, dict) else {}
        import_run = import_runs.get(record.import_run_id)
        audit_contract = (
            classify_legacy_room_point_audit_contract(import_run.summary_json) if import_run is not None else 0
        )
        if (
            import_run is None
            or import_run.import_kind != LEGACY_PAYOUT_CORRECTION_IMPORT_KIND
            or import_run.source_type != "xlsx"
            or import_run.source_identifier != plan.source_identifier
            or import_run.source_checksum != source_checksum
            or import_run.status != "completed"
            or import_run.finished_at is None
        ):
            conflicts.append(_conflict(row, "import_run_changed", "payout correction provenance changed"))
            continue
        if (
            record.row_fingerprint != row.row_fingerprint
            or record.record_type != LEGACY_PAYOUT_CORRECTION_RECORD_TYPE
            or record.status != "applied"
            or record.target_entity_type != "room_point_transaction"
            or record.target_entity_id is None
            or record.source_sheet_name != plan.correction_sheet_name
            or record.source_row_number != row.source_row_number
        ):
            conflicts.append(_conflict(row, "source_record_changed", "payout correction source record changed"))
            continue
        transaction = session.get(CirclePointTransaction, record.target_entity_id)
        account = context.accounts_by_source_row.get(row.source_row_number)
        bet = context.bets_by_source_row.get(row.source_row_number)
        if account is None or bet is None:
            conflicts.append(_conflict(row, "correction_context_changed", "payout correction context changed"))
            continue
        game_account, _ = account
        scaled_correction_amount = scale_legacy_room_point_amount(
            row.correction_amount,
            field_name=f"legacy payout correction at source row {row.source_row_number}",
        )
        scaled_cached_payout_amount = scale_legacy_room_point_amount(
            row.cached_payout_amount,
            field_name=f"legacy cached payout at source row {row.source_row_number}",
        )
        scaled_expected_payout_amount = scale_legacy_room_point_amount(
            row.expected_payout_amount,
            field_name=f"legacy expected payout at source row {row.source_row_number}",
        )
        if (
            transaction is None
            or transaction.type != LEGACY_PAYOUT_CORRECTION_TRANSACTION_TYPE
            or transaction.source != LEGACY_PAYOUT_CORRECTION_SOURCE
            or transaction.game_account_id != game_account.id
            or transaction.persona_id != bet.persona_id
            or transaction.amount != scaled_correction_amount
            or (
                transaction.related_bet_id != bet.id
                if audit_contract == LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION
                else transaction.related_bet_id not in (None, bet.id)
            )
            or not _correction_audit_matches(
                detail,
                audit_contract=audit_contract,
                row=row,
                game_account=game_account,
                bet=bet,
                scaled_correction_amount=scaled_correction_amount,
                scaled_cached_payout_amount=scaled_cached_payout_amount,
                scaled_expected_payout_amount=scaled_expected_payout_amount,
            )
        ):
            conflicts.append(_conflict(row, "transaction_target_changed", "payout correction transaction changed"))
    return tuple(conflicts)


def _correction_audit_matches(
    detail: dict[str, object],
    *,
    audit_contract: int,
    row: LegacyPayoutCorrectionPlanRow,
    game_account: GameAccount,
    bet: Bet,
    scaled_correction_amount: int,
    scaled_cached_payout_amount: int,
    scaled_expected_payout_amount: int,
) -> bool:
    if audit_contract == LEGACY_ROOM_POINT_AUDIT_CONTRACT_V1:
        return (
            detail.get("audit_contract_version") is None
            and detail.get("game_account_id") == game_account.id
            and detail.get("participant_name") == row.participant_name
            and detail.get("correction_amount") == row.correction_amount
            and detail.get("cached_payout_amount") == row.cached_payout_amount
            and detail.get("expected_payout_amount") == row.expected_payout_amount
        )
    return (
        audit_contract == LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION
        and detail.get("audit_contract_version") == LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION
        and detail.get("room_point_scale") == EXPECTED_ROOM_POINT_SCALE
        and detail.get("game_account_id") == game_account.id
        and detail.get("persona_id") == bet.persona_id
        and detail.get("bet_id") == bet.id
        and detail.get("participant_name") == row.participant_name
        and detail.get("race_label") == row.race_label
        and detail.get("bet_type") == row.bet_type
        and detail.get("numbers") == list(row.numbers)
        and detail.get("context_fingerprint") == row.context_fingerprint
        and detail.get("stake_amount") == row.stake_amount
        and detail.get("cached_payout_rate") == format(row.cached_payout_rate, "f")
        and detail.get("expected_payout_rate") == format(row.expected_payout_rate, "f")
        and detail.get("correction_amount") == row.correction_amount
        and detail.get("cached_payout_amount") == row.cached_payout_amount
        and detail.get("expected_payout_amount") == row.expected_payout_amount
        and detail.get("source_correction_amount") == row.correction_amount
        and detail.get("source_cached_payout_amount") == row.cached_payout_amount
        and detail.get("source_expected_payout_amount") == row.expected_payout_amount
        and detail.get("target_correction_amount") == scaled_correction_amount
        and detail.get("target_cached_payout_amount") == scaled_cached_payout_amount
        and detail.get("target_expected_payout_amount") == scaled_expected_payout_amount
    )


def _validate_wallet_ledger_totals(
    session: Session,
    *,
    plan: LegacyPayoutCorrectionPlan,
    context: _CorrectionContext,
) -> list[LegacyPayoutCorrectionConflict]:
    conflicts: list[LegacyPayoutCorrectionConflict] = []
    checked_personas: set[str] = set()
    first_row_by_persona_id = {
        account[1].persona_id: row
        for row in reversed(plan.rows)
        if (account := context.accounts_by_source_row.get(row.source_row_number)) is not None
    }
    for _game_account, point_account in context.accounts_by_source_row.values():
        if point_account.persona_id in checked_personas:
            continue
        checked_personas.add(point_account.persona_id)
        transaction_total = session.scalar(
            select(func.coalesce(func.sum(CirclePointTransaction.amount), 0)).where(
                CirclePointTransaction.persona_id == point_account.persona_id
            )
        )
        if point_account.balance != transaction_total:
            conflicts.append(
                _conflict(
                    first_row_by_persona_id[point_account.persona_id],
                    "point_ledger_mismatch",
                    "Persona wallet no longer reconciles to its ledger",
                )
            )
    return conflicts


def _validate_scalable_plan(plan: LegacyPayoutCorrectionPlan) -> None:
    for row in plan.rows:
        scale_legacy_circle_point_balance(
            row.expected_initial_balance,
            field_name=f"legacy payout initial balance at source row {row.source_row_number}",
        )
        scale_legacy_room_point_amount(
            row.correction_amount,
            field_name=f"legacy payout correction at source row {row.source_row_number}",
        )
        scale_legacy_room_point_amount(
            row.cached_payout_amount,
            field_name=f"legacy cached payout at source row {row.source_row_number}",
        )
        scale_legacy_room_point_amount(
            row.expected_payout_amount,
            field_name=f"legacy expected payout at source row {row.source_row_number}",
        )


def _preview(
    plan: LegacyPayoutCorrectionPlan,
    *,
    new_count: int,
    skipped_count: int,
    conflicts: tuple[LegacyPayoutCorrectionConflict, ...],
) -> LegacyPayoutCorrectionPreview:
    return LegacyPayoutCorrectionPreview(
        new_count=new_count,
        skipped_count=skipped_count,
        correction_total=plan.correction_total,
        conflicts=conflicts,
    )


def _participant_snapshots(
    values: tuple[dict[str, Any], ...],
) -> dict[str, tuple[str, int, int]]:
    snapshots: dict[str, tuple[str, int, int]] = {}
    for value in values:
        participant_name = _required_text(value, "participant_name")
        if participant_name in snapshots:
            raise LegacyImportError(f"duplicate payout correction participant: {participant_name}")
        snapshots[participant_name] = (
            _required_text(value, "discord_user_id"),
            _required_nonnegative_int(value, "current_balance"),
            _required_int(value, "correction_amount"),
        )
    return snapshots


def _required_text(value: dict[str, Any], field_name: str) -> str:
    field_value = value.get(field_name)
    if not isinstance(field_value, str) or not field_value:
        raise LegacyImportError(f"payout correction field is invalid: {field_name}")
    return field_value


def _required_int(value: dict[str, Any], field_name: str) -> int:
    field_value = value.get(field_name)
    if not isinstance(field_value, int) or isinstance(field_value, bool):
        raise LegacyImportError(f"payout correction field is invalid: {field_name}")
    return field_value


def _required_positive_int(value: dict[str, Any], field_name: str) -> int:
    field_value = _required_int(value, field_name)
    if field_value <= 0:
        raise LegacyImportError(f"payout correction field must be positive: {field_name}")
    return field_value


def _required_nonnegative_int(value: dict[str, Any], field_name: str) -> int:
    field_value = _required_int(value, field_name)
    if field_value < 0:
        raise LegacyImportError(f"payout correction field must be nonnegative: {field_name}")
    return field_value


def _required_numbers(value: dict[str, Any], field_name: str) -> tuple[int, ...]:
    field_value = value.get(field_name)
    if not isinstance(field_value, (list, tuple)) or not field_value:
        raise LegacyImportError(f"payout correction field is invalid: {field_name}")
    numbers = tuple(field_value)
    if any(not isinstance(number, int) or isinstance(number, bool) or number <= 0 for number in numbers):
        raise LegacyImportError(f"payout correction field is invalid: {field_name}")
    return numbers


def _required_decimal(value: dict[str, Any], field_name: str) -> Decimal:
    field_value = value.get(field_name)
    if isinstance(field_value, bool) or not isinstance(field_value, (str, int, float, Decimal)):
        raise LegacyImportError(f"payout correction field is invalid: {field_name}")
    try:
        parsed = Decimal(str(field_value))
    except InvalidOperation as exc:
        raise LegacyImportError(f"payout correction field is invalid: {field_name}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise LegacyImportError(f"payout correction field is invalid: {field_name}")
    return parsed


def _conflict(
    row: LegacyPayoutCorrectionPlanRow,
    code: str,
    message: str,
) -> LegacyPayoutCorrectionConflict:
    return LegacyPayoutCorrectionConflict(
        source_row_number=row.source_row_number,
        code=code,
        message=message,
    )


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("legacy payout correction requires a session without pending changes")
