from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    Persona,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.errors import LegacyImportConflictError, LegacyImportError
from umacircle_bot.domain.identity import IdentityStatus
from umacircle_bot.domain.imports import (
    build_import_row_fingerprint,
    build_sheet_import_source_key,
    normalize_sha256_hex,
)
from umacircle_bot.runtime_preflight import EXPECTED_ROOM_POINT_SCALE
from umacircle_bot.services.legacy_import import (
    LEGACY_IDENTITY_POINT_RECORD_TYPE,
    LegacyIdentityPointImportPlan,
    LegacyIdentityPointImportPreview,
    apply_legacy_identity_point_import,
    preview_legacy_identity_point_import,
)
from umacircle_bot.services.legacy_point_scale import (
    require_current_legacy_room_point_import_target,
    scale_legacy_circle_point_balance,
)
from umacircle_bot.sheets.legacy_room_report import LegacyRoomDryRunReport

WIN5_FIRST_BASELINE_IMPORT_KIND = "win5_first_point_baseline"
WIN5_FIRST_BASELINE_RECORD_TYPE = "win5_first_opening_balance"
WIN5_FIRST_BASELINE_SHEET_NAME = "WIN5_FIRST_OPENING_BASELINE"
WIN5_FIRST_BASELINE_TRANSACTION_TYPE = "legacy_opening_balance"
WIN5_FIRST_BASELINE_TRANSACTION_SOURCE = "win5_first_opening_baseline"
WIN5_FIRST_BASELINE_MANIFEST_VERSION = 1


@dataclass(frozen=True)
class Win5FirstBaselinePlanRow:
    source_row_number: int
    source_key: str
    row_fingerprint: str
    identity_source_key: str
    identity_row_fingerprint: str
    discord_user_id: str
    nickname: str
    source_current_balance: int
    source_correction_amount: int
    source_corrected_balance: int
    target_balance: int


@dataclass(frozen=True)
class Win5FirstBaselinePlan:
    source_identifier: str
    source_checksum: str
    rows: tuple[Win5FirstBaselinePlanRow, ...]

    @property
    def source_current_total(self) -> int:
        return sum(row.source_current_balance for row in self.rows)

    @property
    def source_correction_total(self) -> int:
        return sum(row.source_correction_amount for row in self.rows)

    @property
    def source_corrected_total(self) -> int:
        return sum(row.source_corrected_balance for row in self.rows)

    @property
    def target_total(self) -> int:
        return sum(row.target_balance for row in self.rows)

    @property
    def baseline_checksum(self) -> str:
        payload = f"win5-first-opening-baseline-v1\n{self.source_identifier}\n{self.source_checksum}\n" + "\n".join(
            f"{row.source_key}:{row.row_fingerprint}" for row in self.rows
        )
        return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Win5FirstBaselineConflict:
    source_row_number: int
    code: str
    message: str


@dataclass(frozen=True)
class Win5FirstBaselinePreview:
    identity_preview: LegacyIdentityPointImportPreview
    new_count: int
    skipped_count: int
    source_current_total: int
    source_correction_total: int
    source_corrected_total: int
    target_total: int
    baseline_checksum: str
    conflicts: tuple[Win5FirstBaselineConflict, ...]

    @property
    def can_apply(self) -> bool:
        return not self.identity_preview.conflicts and not self.conflicts


@dataclass(frozen=True)
class Win5FirstBaselineResult:
    identity_import_run_id: int
    baseline_import_run_id: int
    identity_created_count: int
    identity_skipped_count: int
    baseline_created_count: int
    baseline_skipped_count: int
    source_corrected_total: int
    target_total: int
    baseline_checksum: str


@dataclass(frozen=True)
class _IdentityContext:
    identity_record: SheetImportRecord
    discord_account: DiscordAccount
    persona: Persona
    game_account: GameAccount
    point_account: CirclePointAccount


def build_win5_first_baseline_plan(
    identity_plan: LegacyIdentityPointImportPlan,
    report: LegacyRoomDryRunReport,
) -> Win5FirstBaselinePlan:
    source_checksum = normalize_sha256_hex(report.source_checksum, field_name="reconciliation source checksum")
    if report.has_errors:
        raise LegacyImportError("WIN5-first baseline requires an error-free Room Match reconciliation")
    if report.final_point_total is None or report.corrected_final_point_total is None:
        raise LegacyImportError("WIN5-first baseline reconciliation totals are unavailable")
    if report.point_row_count != len(identity_plan.rows):
        raise LegacyImportError("WIN5-first baseline identity count does not match the reconciliation report")
    if report.final_point_total != identity_plan.total_current_balance:
        raise LegacyImportError("WIN5-first baseline current total does not match the identity plan")

    identity_by_discord_id = {row.discord_user_id: row for row in identity_plan.rows}
    corrections: dict[str, int] = {}
    for raw in report.payout_correction_plan:
        discord_user_id = _required_text(raw.get("discord_user_id"), field_name="correction Discord user ID")
        participant_name = _required_text(raw.get("participant_name"), field_name="correction participant name")
        current_balance = _required_int(raw.get("current_balance"), field_name="correction current balance")
        correction_amount = _required_int(raw.get("correction_amount"), field_name="correction amount")
        corrected_balance = _required_int(raw.get("corrected_balance"), field_name="corrected balance")
        identity_row = identity_by_discord_id.get(discord_user_id)
        if identity_row is None:
            raise LegacyImportError("WIN5-first payout correction has no identity row")
        if discord_user_id in corrections:
            raise LegacyImportError("WIN5-first payout correction contains a duplicate identity")
        if participant_name != identity_row.nickname or current_balance != identity_row.current_balance:
            raise LegacyImportError("WIN5-first payout correction identity snapshot changed")
        if correction_amount < 0 or current_balance + correction_amount != corrected_balance:
            raise LegacyImportError("WIN5-first payout correction arithmetic is invalid")
        corrections[discord_user_id] = correction_amount

    rows: list[Win5FirstBaselinePlanRow] = []
    for identity_row in identity_plan.rows:
        correction_amount = corrections.get(identity_row.discord_user_id, 0)
        corrected_balance = identity_row.current_balance + correction_amount
        target_balance = scale_legacy_circle_point_balance(
            corrected_balance,
            field_name=f"WIN5-first corrected balance at source row {identity_row.source_row_number}",
        )
        source_key = build_sheet_import_source_key(
            source_identifier=identity_plan.source_identifier,
            sheet_name=WIN5_FIRST_BASELINE_SHEET_NAME,
            row_number=identity_row.source_row_number,
        )
        row_fingerprint = build_import_row_fingerprint(
            (
                WIN5_FIRST_BASELINE_MANIFEST_VERSION,
                identity_row.source_key,
                identity_row.row_fingerprint,
                identity_row.discord_user_id,
                identity_row.nickname,
                identity_row.current_balance,
                correction_amount,
                corrected_balance,
                target_balance,
                EXPECTED_ROOM_POINT_SCALE,
            )
        )
        rows.append(
            Win5FirstBaselinePlanRow(
                source_row_number=identity_row.source_row_number,
                source_key=source_key,
                row_fingerprint=row_fingerprint,
                identity_source_key=identity_row.source_key,
                identity_row_fingerprint=identity_row.row_fingerprint,
                discord_user_id=identity_row.discord_user_id,
                nickname=identity_row.nickname,
                source_current_balance=identity_row.current_balance,
                source_correction_amount=correction_amount,
                source_corrected_balance=corrected_balance,
                target_balance=target_balance,
            )
        )

    plan = Win5FirstBaselinePlan(
        source_identifier=identity_plan.source_identifier,
        source_checksum=source_checksum,
        rows=tuple(rows),
    )
    if plan.source_correction_total != report.payout_correction_total:
        raise LegacyImportError("WIN5-first payout correction total does not match the reconciliation report")
    if plan.source_corrected_total != report.corrected_final_point_total:
        raise LegacyImportError("WIN5-first corrected total does not match the reconciliation report")
    return plan


def preview_win5_first_baseline(
    session: Session,
    *,
    identity_plan: LegacyIdentityPointImportPlan,
    baseline_plan: Win5FirstBaselinePlan,
    source_checksum: str,
) -> Win5FirstBaselinePreview:
    _require_clean_session(session)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    _require_matching_plans(identity_plan, baseline_plan, source_checksum=normalized_checksum)
    identity_preview = preview_legacy_identity_point_import(
        session,
        plan=identity_plan,
        source_checksum=normalized_checksum,
    )
    return _inspect_baseline(
        session,
        identity_plan=identity_plan,
        baseline_plan=baseline_plan,
        source_checksum=normalized_checksum,
        identity_preview=identity_preview,
        lock_rows=False,
    )


def apply_win5_first_baseline(
    session: Session,
    *,
    identity_plan: LegacyIdentityPointImportPlan,
    baseline_plan: Win5FirstBaselinePlan,
    source_checksum: str,
) -> Win5FirstBaselineResult:
    _require_clean_session(session)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    _require_matching_plans(identity_plan, baseline_plan, source_checksum=normalized_checksum)

    with session.begin_nested():
        identity_result = apply_legacy_identity_point_import(
            session,
            plan=identity_plan,
            source_checksum=normalized_checksum,
        )
        identity_preview = preview_legacy_identity_point_import(
            session,
            plan=identity_plan,
            source_checksum=normalized_checksum,
        )
        preview = _inspect_baseline(
            session,
            identity_plan=identity_plan,
            baseline_plan=baseline_plan,
            source_checksum=normalized_checksum,
            identity_preview=identity_preview,
            lock_rows=True,
        )
        if preview.conflicts:
            first = preview.conflicts[0]
            raise LegacyImportConflictError(
                f"WIN5-first baseline conflict at row {first.source_row_number}: {first.code}"
            )

        existing_records = _load_baseline_records(session, plan=baseline_plan, lock_rows=True)
        if existing_records:
            baseline_run = session.get(SheetImportRun, next(iter(existing_records.values())).import_run_id)
            if baseline_run is None:
                raise LegacyImportConflictError("WIN5-first baseline retry import run disappeared")
            baseline_created_count = 0
            baseline_skipped_count = len(baseline_plan.rows)
        else:
            contexts = _load_identity_contexts(
                session,
                identity_plan=identity_plan,
                baseline_plan=baseline_plan,
                lock_rows=True,
            )
            baseline_run = SheetImportRun(
                import_kind=WIN5_FIRST_BASELINE_IMPORT_KIND,
                source_type="xlsx",
                source_identifier=baseline_plan.source_identifier,
                source_checksum=normalized_checksum,
                status="running",
            )
            session.add(baseline_run)
            session.flush()
            for row in baseline_plan.rows:
                _create_baseline_row(
                    session,
                    import_run=baseline_run,
                    row=row,
                    context=contexts[row.identity_source_key],
                )
            baseline_created_count = len(baseline_plan.rows)
            baseline_skipped_count = 0
            baseline_run.status = "completed"
            baseline_run.finished_at = datetime.now(UTC)
            baseline_run.summary_json = _summary(baseline_plan)
            session.flush()

    return Win5FirstBaselineResult(
        identity_import_run_id=identity_result.import_run_id,
        baseline_import_run_id=baseline_run.id,
        identity_created_count=identity_result.created_count,
        identity_skipped_count=identity_result.skipped_count,
        baseline_created_count=baseline_created_count,
        baseline_skipped_count=baseline_skipped_count,
        source_corrected_total=baseline_plan.source_corrected_total,
        target_total=baseline_plan.target_total,
        baseline_checksum=baseline_plan.baseline_checksum,
    )


def _inspect_baseline(
    session: Session,
    *,
    identity_plan: LegacyIdentityPointImportPlan,
    baseline_plan: Win5FirstBaselinePlan,
    source_checksum: str,
    identity_preview: LegacyIdentityPointImportPreview,
    lock_rows: bool,
) -> Win5FirstBaselinePreview:
    require_current_legacy_room_point_import_target(session)
    conflicts = [
        Win5FirstBaselineConflict(
            source_row_number=conflict.source_row_number,
            code=f"identity_{conflict.code}",
            message=conflict.message,
        )
        for conflict in identity_preview.conflicts
    ]
    records = _load_baseline_records(session, plan=baseline_plan, lock_rows=lock_rows)
    runs = _load_baseline_runs(session, records=records, lock_rows=lock_rows)
    matching_runs = tuple(
        session.scalars(
            select(SheetImportRun).where(
                SheetImportRun.import_kind == WIN5_FIRST_BASELINE_IMPORT_KIND,
                SheetImportRun.source_type == "xlsx",
                SheetImportRun.source_identifier == baseline_plan.source_identifier,
            )
        )
    )
    if records and len(records) != len(baseline_plan.rows):
        missing = next(row for row in baseline_plan.rows if row.source_key not in records)
        conflicts.append(_conflict(missing, "partial_import_state", "baseline source records are partially present"))
    elif not records and matching_runs:
        conflicts.append(_conflict(baseline_plan.rows[0], "orphan_import_run", "baseline run exists without records"))

    if not conflicts and records:
        if len(matching_runs) != 1 or len(runs) != 1:
            conflicts.append(_conflict(baseline_plan.rows[0], "import_run_ambiguous", "baseline run is ambiguous"))
        else:
            contexts = _load_identity_contexts(
                session,
                identity_plan=identity_plan,
                baseline_plan=baseline_plan,
                lock_rows=lock_rows,
            )
            run = next(iter(runs.values()))
            for row in baseline_plan.rows:
                conflict = _validate_existing_baseline_row(
                    session,
                    row=row,
                    record=records[row.source_key],
                    run=run,
                    context=contexts[row.identity_source_key],
                    source_identifier=baseline_plan.source_identifier,
                    source_checksum=source_checksum,
                )
                if conflict is not None:
                    conflicts.append(conflict)
                    break
            if not conflicts and run.summary_json != _summary(baseline_plan):
                conflicts.append(
                    _conflict(baseline_plan.rows[0], "import_summary_changed", "baseline summary no longer matches")
                )

    entity_counts = _entity_counts(session)
    transaction_count = entity_counts["room_point_transactions"]
    if not conflicts and records:
        if any(entity_counts[name] != len(baseline_plan.rows) for name in _BASELINE_ENTITY_NAMES):
            conflicts.append(
                _conflict(baseline_plan.rows[0], "unexpected_identity_population", "baseline population changed")
            )
        elif transaction_count != len(baseline_plan.rows):
            conflicts.append(
                _conflict(baseline_plan.rows[0], "unexpected_point_history", "baseline has extra point transactions")
            )
    elif not conflicts and not records:
        if identity_preview.new_count == len(identity_plan.rows):
            if any(entity_counts[name] for name in (*_BASELINE_ENTITY_NAMES, "room_point_transactions")):
                conflicts.append(
                    _conflict(baseline_plan.rows[0], "target_not_empty", "baseline target identity state is not empty")
                )
        elif identity_preview.skipped_count == len(identity_plan.rows):
            if any(entity_counts[name] != len(baseline_plan.rows) for name in _BASELINE_ENTITY_NAMES):
                conflicts.append(
                    _conflict(baseline_plan.rows[0], "unexpected_identity_population", "identity population changed")
                )
            elif transaction_count:
                conflicts.append(
                    _conflict(baseline_plan.rows[0], "point_history_already_started", "point history already exists")
                )

    return Win5FirstBaselinePreview(
        identity_preview=identity_preview,
        new_count=len(baseline_plan.rows) if not records and not conflicts else 0,
        skipped_count=len(records) if records and not conflicts else 0,
        source_current_total=baseline_plan.source_current_total,
        source_correction_total=baseline_plan.source_correction_total,
        source_corrected_total=baseline_plan.source_corrected_total,
        target_total=baseline_plan.target_total,
        baseline_checksum=baseline_plan.baseline_checksum,
        conflicts=tuple(conflicts),
    )


_BASELINE_ENTITY_NAMES = (
    "discord_accounts",
    "personas",
    "game_accounts",
    "room_point_accounts",
)


def _entity_counts(session: Session) -> dict[str, int]:
    models = (DiscordAccount, Persona, GameAccount, CirclePointAccount, CirclePointTransaction)
    return {model.__tablename__: int(session.scalar(select(func.count()).select_from(model)) or 0) for model in models}


def _load_baseline_records(
    session: Session,
    *,
    plan: Win5FirstBaselinePlan,
    lock_rows: bool,
) -> dict[str, SheetImportRecord]:
    statement = select(SheetImportRecord).where(SheetImportRecord.source_key.in_([row.source_key for row in plan.rows]))
    if lock_rows:
        statement = statement.order_by(SheetImportRecord.id).with_for_update()
    return {record.source_key: record for record in session.scalars(statement)}


def _load_baseline_runs(
    session: Session,
    *,
    records: dict[str, SheetImportRecord],
    lock_rows: bool,
) -> dict[int, SheetImportRun]:
    run_ids = sorted({record.import_run_id for record in records.values()})
    if not run_ids:
        return {}
    statement = select(SheetImportRun).where(SheetImportRun.id.in_(run_ids)).order_by(SheetImportRun.id)
    if lock_rows:
        statement = statement.with_for_update()
    return {run.id: run for run in session.scalars(statement)}


def _load_identity_contexts(
    session: Session,
    *,
    identity_plan: LegacyIdentityPointImportPlan,
    baseline_plan: Win5FirstBaselinePlan,
    lock_rows: bool,
) -> dict[str, _IdentityContext]:
    identity_keys = [row.source_key for row in identity_plan.rows]
    statement = select(SheetImportRecord).where(SheetImportRecord.source_key.in_(identity_keys))
    if lock_rows:
        statement = statement.order_by(SheetImportRecord.id).with_for_update()
    identity_records = {record.source_key: record for record in session.scalars(statement)}
    if len(identity_records) != len(identity_plan.rows):
        raise LegacyImportConflictError("WIN5-first baseline identity records are incomplete")

    account_ids = sorted(
        int(record.target_entity_id) for record in identity_records.values() if record.target_entity_id is not None
    )
    account_statement = select(GameAccount).where(GameAccount.id.in_(account_ids)).order_by(GameAccount.id)
    if lock_rows:
        account_statement = account_statement.with_for_update()
    accounts = {account.id: account for account in session.scalars(account_statement)}
    persona_ids = sorted({account.persona_id for account in accounts.values() if account.persona_id is not None})
    persona_statement = select(Persona).where(Persona.id.in_(persona_ids)).order_by(Persona.id)
    wallet_statement = (
        select(CirclePointAccount).where(CirclePointAccount.persona_id.in_(persona_ids)).order_by(CirclePointAccount.id)
    )
    if lock_rows:
        persona_statement = persona_statement.with_for_update()
        wallet_statement = wallet_statement.with_for_update()
    personas = {persona.id: persona for persona in session.scalars(persona_statement)}
    wallets = {wallet.persona_id: wallet for wallet in session.scalars(wallet_statement)}
    discord_ids = sorted(
        account.discord_account_id for account in accounts.values() if account.discord_account_id is not None
    )
    discord_statement = select(DiscordAccount).where(DiscordAccount.id.in_(discord_ids)).order_by(DiscordAccount.id)
    if lock_rows:
        discord_statement = discord_statement.with_for_update()
    discord_accounts = {account.id: account for account in session.scalars(discord_statement)}

    contexts: dict[str, _IdentityContext] = {}
    baseline_by_identity_key = {row.identity_source_key: row for row in baseline_plan.rows}
    for source_key, record in identity_records.items():
        row = baseline_by_identity_key[source_key]
        account = accounts.get(record.target_entity_id) if record.target_entity_id is not None else None
        persona = personas.get(account.persona_id) if account is not None and account.persona_id is not None else None
        discord = (
            discord_accounts.get(account.discord_account_id)
            if account is not None and account.discord_account_id is not None
            else None
        )
        wallet = wallets.get(persona.id) if persona is not None else None
        valid = (
            record.record_type == LEGACY_IDENTITY_POINT_RECORD_TYPE
            and record.status == "applied"
            and record.target_entity_type == "game_account"
            and record.row_fingerprint == row.identity_row_fingerprint
            and account is not None
            and persona is not None
            and discord is not None
            and wallet is not None
            and discord.discord_user_id == row.discord_user_id
            and discord.persona_id == persona.id
            and account.persona_id == persona.id
            and account.discord_account_id == discord.id
        )
        if not valid:
            raise LegacyImportConflictError("WIN5-first baseline identity ownership changed")
        contexts[source_key] = _IdentityContext(
            identity_record=record,
            discord_account=discord,
            persona=persona,
            game_account=account,
            point_account=wallet,
        )
    return contexts


def _create_baseline_row(
    session: Session,
    *,
    import_run: SheetImportRun,
    row: Win5FirstBaselinePlanRow,
    context: _IdentityContext,
) -> None:
    if (
        context.persona.status != "inactive"
        or context.game_account.identity_status != IdentityStatus.PENDING.value
        or context.game_account.uma_pid is not None
    ):
        raise LegacyImportConflictError("WIN5-first baseline requires inactive pending imported identities")
    context.point_account.balance = row.target_balance
    transaction = CirclePointTransaction(
        persona_id=context.persona.id,
        game_account_id=context.game_account.id,
        type=WIN5_FIRST_BASELINE_TRANSACTION_TYPE,
        amount=row.target_balance,
        reason="WIN5-first authoritative RoomPoint opening baseline",
        source=WIN5_FIRST_BASELINE_TRANSACTION_SOURCE,
        idempotency_key=_transaction_idempotency_key(row),
    )
    session.add(transaction)
    session.flush()
    session.add(
        SheetImportRecord(
            import_run_id=import_run.id,
            source_key=row.source_key,
            row_fingerprint=row.row_fingerprint,
            source_sheet_name=WIN5_FIRST_BASELINE_SHEET_NAME,
            source_row_number=row.source_row_number,
            record_type=WIN5_FIRST_BASELINE_RECORD_TYPE,
            status="applied",
            target_entity_type="room_point_transaction",
            target_entity_id=transaction.id,
            detail_json={
                "manifest_version": WIN5_FIRST_BASELINE_MANIFEST_VERSION,
                "identity_source_key": row.identity_source_key,
                "identity_import_record_id": context.identity_record.id,
                "discord_account_id": context.discord_account.id,
                "persona_id": context.persona.id,
                "game_account_id": context.game_account.id,
                "point_account_id": context.point_account.id,
                "source_current_balance": row.source_current_balance,
                "source_correction_amount": row.source_correction_amount,
                "source_corrected_balance": row.source_corrected_balance,
                "target_balance": row.target_balance,
                "room_point_scale": EXPECTED_ROOM_POINT_SCALE,
                "transaction_idempotency_key": transaction.idempotency_key,
            },
        )
    )
    session.flush()


def _validate_existing_baseline_row(
    session: Session,
    *,
    row: Win5FirstBaselinePlanRow,
    record: SheetImportRecord,
    run: SheetImportRun,
    context: _IdentityContext,
    source_identifier: str,
    source_checksum: str,
) -> Win5FirstBaselineConflict | None:
    if (
        run.import_kind != WIN5_FIRST_BASELINE_IMPORT_KIND
        or run.source_type != "xlsx"
        or run.source_identifier != source_identifier
        or run.source_checksum != source_checksum
        or run.status != "completed"
        or run.finished_at is None
    ):
        return _conflict(row, "import_run_changed", "baseline import run provenance changed")
    if (
        record.import_run_id != run.id
        or record.source_sheet_name != WIN5_FIRST_BASELINE_SHEET_NAME
        or record.source_row_number != row.source_row_number
        or record.row_fingerprint != row.row_fingerprint
        or record.record_type != WIN5_FIRST_BASELINE_RECORD_TYPE
        or record.status != "applied"
        or record.target_entity_type != "room_point_transaction"
        or record.target_entity_id is None
    ):
        return _conflict(row, "import_record_changed", "baseline import record changed")
    transaction = session.get(CirclePointTransaction, record.target_entity_id)
    if (
        transaction is None
        or transaction.persona_id != context.persona.id
        or transaction.game_account_id != context.game_account.id
        or transaction.type != WIN5_FIRST_BASELINE_TRANSACTION_TYPE
        or transaction.source != WIN5_FIRST_BASELINE_TRANSACTION_SOURCE
        or transaction.amount != row.target_balance
        or transaction.idempotency_key != _transaction_idempotency_key(row)
    ):
        return _conflict(row, "opening_transaction_changed", "baseline opening transaction changed")
    detail = record.detail_json if isinstance(record.detail_json, dict) else {}
    expected_detail = {
        "manifest_version": WIN5_FIRST_BASELINE_MANIFEST_VERSION,
        "identity_source_key": row.identity_source_key,
        "identity_import_record_id": context.identity_record.id,
        "discord_account_id": context.discord_account.id,
        "persona_id": context.persona.id,
        "game_account_id": context.game_account.id,
        "point_account_id": context.point_account.id,
        "source_current_balance": row.source_current_balance,
        "source_correction_amount": row.source_correction_amount,
        "source_corrected_balance": row.source_corrected_balance,
        "target_balance": row.target_balance,
        "room_point_scale": EXPECTED_ROOM_POINT_SCALE,
        "transaction_idempotency_key": transaction.idempotency_key,
    }
    if detail != expected_detail:
        return _conflict(row, "import_audit_changed", "baseline import audit changed")
    if (
        context.persona.status != "inactive"
        or context.game_account.identity_status != IdentityStatus.PENDING.value
        or context.game_account.uma_pid is not None
        or context.point_account.balance != row.target_balance
    ):
        return _conflict(row, "baseline_identity_changed", "baseline identity or wallet changed")
    transaction_total = int(
        session.scalar(
            select(func.coalesce(func.sum(CirclePointTransaction.amount), 0)).where(
                CirclePointTransaction.persona_id == context.persona.id
            )
        )
        or 0
    )
    if transaction_total != row.target_balance:
        return _conflict(row, "point_ledger_changed", "baseline Persona ledger changed")
    return None


def _summary(plan: Win5FirstBaselinePlan) -> dict[str, object]:
    return {
        "manifest_version": WIN5_FIRST_BASELINE_MANIFEST_VERSION,
        "identity_count": len(plan.rows),
        "wallet_count": len(plan.rows),
        "transaction_count": len(plan.rows),
        "source_current_total": plan.source_current_total,
        "source_correction_total": plan.source_correction_total,
        "source_corrected_total": plan.source_corrected_total,
        "target_total": plan.target_total,
        "room_point_scale": EXPECTED_ROOM_POINT_SCALE,
        "baseline_checksum": plan.baseline_checksum,
    }


def _transaction_idempotency_key(row: Win5FirstBaselinePlanRow) -> str:
    return f"win5-first-opening:{row.row_fingerprint}"


def _require_matching_plans(
    identity_plan: LegacyIdentityPointImportPlan,
    baseline_plan: Win5FirstBaselinePlan,
    *,
    source_checksum: str,
) -> None:
    if identity_plan.source_identifier != baseline_plan.source_identifier:
        raise LegacyImportError("WIN5-first identity and baseline source identifiers differ")
    if baseline_plan.source_checksum != source_checksum:
        raise LegacyImportError("WIN5-first baseline source checksum differs")
    if not identity_plan.rows or len(identity_plan.rows) != len(baseline_plan.rows):
        raise LegacyImportError("WIN5-first identity and baseline plans differ")
    if tuple(row.source_key for row in identity_plan.rows) != tuple(
        row.identity_source_key for row in baseline_plan.rows
    ):
        raise LegacyImportError("WIN5-first baseline identity source keys differ")


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise LegacyImportError(f"{field_name} must be non-empty normalized text")
    return value


def _required_int(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise LegacyImportError(f"{field_name} must be an integer")
    return value


def _conflict(row: Win5FirstBaselinePlanRow, code: str, message: str) -> Win5FirstBaselineConflict:
    return Win5FirstBaselineConflict(source_row_number=row.source_row_number, code=code, message=message)


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("WIN5-first baseline requires a session without pending changes")
