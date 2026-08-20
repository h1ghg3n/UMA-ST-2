from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    IdentityBackfillTask,
    Persona,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.errors import LegacyImportConflictError, LegacyImportError
from umacircle_bot.domain.identity import IdentityStatus
from umacircle_bot.domain.imports import (
    build_import_row_fingerprint,
    build_sheet_import_source_key,
    normalize_import_sheet_name,
    normalize_import_source_identifier,
    normalize_sha256_hex,
)
from umacircle_bot.runtime_preflight import EXPECTED_ROOM_POINT_SCALE
from umacircle_bot.services.legacy_point_scale import (
    LEGACY_ROOM_POINT_AUDIT_CONTRACT_V1,
    LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION,
    classify_legacy_room_point_audit_contract,
    require_current_legacy_room_point_import_target,
    scale_legacy_circle_point_balance,
    scale_legacy_room_point_amount,
)
from umacircle_bot.sheets.legacy_room_ledger import LegacyIdentityReconciliation
from umacircle_bot.sheets.row_parsers import RoomPointSourceRow

LEGACY_IDENTITY_POINT_IMPORT_KIND = "legacy_room_identity_points"
LEGACY_IDENTITY_POINT_RECORD_TYPE = "legacy_identity_point"
DEFAULT_POINT_SHEET_NAME = "동친 포인트"


@dataclass(frozen=True)
class LegacyIdentityPointPlanRow:
    source_row_number: int
    source_key: str
    row_fingerprint: str
    discord_user_id: str
    nickname: str
    ingame_name: str | None
    opening_balance: int
    current_balance: int


@dataclass(frozen=True)
class LegacyIdentityPointImportPlan:
    source_identifier: str
    source_sheet_name: str
    rows: tuple[LegacyIdentityPointPlanRow, ...]

    @property
    def total_current_balance(self) -> int:
        return sum(row.current_balance for row in self.rows)


@dataclass(frozen=True)
class LegacyIdentityPointImportConflict:
    source_row_number: int
    code: str
    message: str


@dataclass(frozen=True)
class LegacyIdentityPointImportPreview:
    new_count: int
    skipped_count: int
    total_current_balance: int
    conflicts: tuple[LegacyIdentityPointImportConflict, ...]

    @property
    def can_apply(self) -> bool:
        return not self.conflicts

    @property
    def source_total_current_balance(self) -> int:
        return self.total_current_balance

    @property
    def target_total_current_balance(self) -> int:
        return self.total_current_balance * EXPECTED_ROOM_POINT_SCALE


@dataclass(frozen=True)
class LegacyIdentityPointImportResult:
    import_run_id: int
    created_count: int
    skipped_count: int
    total_current_balance: int

    @property
    def source_total_current_balance(self) -> int:
        return self.total_current_balance

    @property
    def target_total_current_balance(self) -> int:
        return self.total_current_balance * EXPECTED_ROOM_POINT_SCALE


def build_legacy_identity_point_import_plan(
    point_rows: tuple[RoomPointSourceRow, ...],
    identities: tuple[LegacyIdentityReconciliation, ...],
    *,
    source_identifier: str,
    source_sheet_name: str = DEFAULT_POINT_SHEET_NAME,
) -> LegacyIdentityPointImportPlan:
    normalized_source = normalize_import_source_identifier(source_identifier)
    normalized_sheet = normalize_import_sheet_name(source_sheet_name)
    if not point_rows:
        raise LegacyImportError("legacy point import requires at least one source row")

    identities_by_name: dict[str, LegacyIdentityReconciliation] = {}
    for identity in identities:
        if identity.participant_name in identities_by_name:
            raise LegacyImportError(f"duplicate reconciled identity: {identity.participant_name}")
        identities_by_name[identity.participant_name] = identity

    planned: list[LegacyIdentityPointPlanRow] = []
    seen_discord_ids: set[str] = set()
    seen_nicknames: set[str] = set()
    seen_source_keys: set[str] = set()
    for point_row in sorted(point_rows, key=lambda row: row.row_number):
        if point_row.discord_user_id in seen_discord_ids:
            raise LegacyImportError(f"duplicate Discord user ID at source row {point_row.row_number}")
        if point_row.nickname in seen_nicknames:
            raise LegacyImportError(f"duplicate nickname at source row {point_row.row_number}")
        identity = identities_by_name.get(point_row.nickname)
        if identity is None:
            raise LegacyImportError(f"point row has no reconciled identity: {point_row.nickname}")
        if identity.discord_user_id != point_row.discord_user_id:
            raise LegacyImportError(f"Discord user ID mismatch for {point_row.nickname}")
        if identity.current_balance != point_row.points or identity.reconstructed_balance != point_row.points:
            raise LegacyImportError(f"point balance mismatch for {point_row.nickname}")
        scale_legacy_room_point_amount(
            identity.opening_balance,
            field_name=f"legacy opening balance at source row {point_row.row_number}",
        )
        scale_legacy_circle_point_balance(
            point_row.points,
            field_name=f"legacy current balance at source row {point_row.row_number}",
        )

        source_key = build_sheet_import_source_key(
            source_identifier=normalized_source,
            sheet_name=normalized_sheet,
            row_number=point_row.row_number,
        )
        if source_key in seen_source_keys:
            raise LegacyImportError(f"duplicate source key at source row {point_row.row_number}")
        fingerprint = build_import_row_fingerprint(
            (
                point_row.discord_user_id,
                point_row.nickname,
                point_row.points,
                point_row.ingame_name,
                identity.opening_balance,
            )
        )
        planned.append(
            LegacyIdentityPointPlanRow(
                source_row_number=point_row.row_number,
                source_key=source_key,
                row_fingerprint=fingerprint,
                discord_user_id=point_row.discord_user_id,
                nickname=point_row.nickname,
                ingame_name=point_row.ingame_name,
                opening_balance=identity.opening_balance,
                current_balance=point_row.points,
            )
        )
        seen_discord_ids.add(point_row.discord_user_id)
        seen_nicknames.add(point_row.nickname)
        seen_source_keys.add(source_key)

    unexpected_identities = set(identities_by_name) - seen_nicknames
    if unexpected_identities:
        raise LegacyImportError(f"reconciled identity has no point row: {min(unexpected_identities)}")
    return LegacyIdentityPointImportPlan(
        source_identifier=normalized_source,
        source_sheet_name=normalized_sheet,
        rows=tuple(planned),
    )


def preview_legacy_identity_point_import(
    session: Session,
    *,
    plan: LegacyIdentityPointImportPlan,
    source_checksum: str,
) -> LegacyIdentityPointImportPreview:
    _require_clean_session(session)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    return _inspect_legacy_identity_point_import(
        session,
        plan=plan,
        source_checksum=normalized_checksum,
        lock_rows=False,
    )


def apply_legacy_identity_point_import(
    session: Session,
    *,
    plan: LegacyIdentityPointImportPlan,
    source_checksum: str,
) -> LegacyIdentityPointImportResult:
    _require_clean_session(session)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")

    with session.begin_nested():
        preview = _inspect_legacy_identity_point_import(
            session,
            plan=plan,
            source_checksum=normalized_checksum,
            lock_rows=True,
        )
        if preview.conflicts:
            first = preview.conflicts[0]
            raise LegacyImportConflictError(
                f"legacy identity/point import conflict at row {first.source_row_number}: {first.code}"
            )

        existing_records = _load_import_records(session, plan=plan, lock_rows=True)
        if existing_records:
            import_run = session.get(SheetImportRun, next(iter(existing_records.values())).import_run_id)
            if import_run is None:
                raise LegacyImportConflictError("legacy identity/point retry import run disappeared")
            created_count = 0
            skipped_count = len(plan.rows)
        else:
            import_run = SheetImportRun(
                import_kind=LEGACY_IDENTITY_POINT_IMPORT_KIND,
                source_type="xlsx",
                source_identifier=plan.source_identifier,
                source_checksum=normalized_checksum,
                status="running",
            )
            session.add(import_run)
            session.flush()

            for row in plan.rows:
                _create_pending_identity_point_row(session, import_run=import_run, plan=plan, row=row)

            created_count = len(plan.rows)
            skipped_count = 0
            import_run.status = "completed"
            import_run.finished_at = datetime.now(UTC)
            import_run.summary_json = {
                "audit_contract_version": LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION,
                "created_count": created_count,
                "skipped_count": skipped_count,
                "total_current_balance": plan.total_current_balance,
                "source_total_current_balance": plan.total_current_balance,
                "target_total_current_balance": plan.total_current_balance * EXPECTED_ROOM_POINT_SCALE,
                "room_point_scale": EXPECTED_ROOM_POINT_SCALE,
            }
            session.flush()

    return LegacyIdentityPointImportResult(
        import_run_id=import_run.id,
        created_count=created_count,
        skipped_count=skipped_count,
        total_current_balance=plan.total_current_balance,
    )


def _inspect_legacy_identity_point_import(
    session: Session,
    *,
    plan: LegacyIdentityPointImportPlan,
    source_checksum: str,
    lock_rows: bool,
) -> LegacyIdentityPointImportPreview:
    require_current_legacy_room_point_import_target(session)
    existing_records = _load_import_records(session, plan=plan, lock_rows=lock_rows)
    if existing_records and len(existing_records) != len(plan.rows):
        first_missing = next(row for row in plan.rows if row.source_key not in existing_records)
        return LegacyIdentityPointImportPreview(
            new_count=0,
            skipped_count=len(existing_records),
            total_current_balance=plan.total_current_balance,
            conflicts=(
                _conflict(
                    first_missing,
                    "partial_import_state",
                    "legacy identity source records are only partially present",
                ),
            ),
        )
    import_runs = _load_import_runs(session, records=existing_records.values(), lock_rows=lock_rows)
    discord_accounts = _load_discord_accounts(session, plan=plan, lock_rows=lock_rows)
    conflicts: list[LegacyIdentityPointImportConflict] = []
    new_count = 0
    skipped_count = 0
    for row in plan.rows:
        record = existing_records.get(row.source_key)
        discord_account = discord_accounts.get(row.discord_user_id)
        if record is not None:
            conflict = _validate_existing_import_record(
                session,
                plan=plan,
                row=row,
                record=record,
                import_run=import_runs.get(record.import_run_id),
                source_checksum=source_checksum,
            )
            if conflict is not None:
                conflicts.append(conflict)
            else:
                skipped_count += 1
            continue
        if discord_account is not None:
            conflicts.append(
                LegacyIdentityPointImportConflict(
                    source_row_number=row.source_row_number,
                    code="discord_account_exists_without_source_record",
                    message="Discord account already exists without the matching import record",
                )
            )
            continue
        new_count += 1
    if not conflicts and len({record.import_run_id for record in existing_records.values()}) > 1:
        conflicts.append(
            _conflict(
                plan.rows[0],
                "import_run_ambiguous",
                "legacy identity source records have multiple parent runs",
            )
        )
    return LegacyIdentityPointImportPreview(
        new_count=new_count,
        skipped_count=skipped_count,
        total_current_balance=plan.total_current_balance,
        conflicts=tuple(conflicts),
    )


def _load_import_records(
    session: Session,
    *,
    plan: LegacyIdentityPointImportPlan,
    lock_rows: bool,
) -> dict[str, SheetImportRecord]:
    statement = select(SheetImportRecord).where(SheetImportRecord.source_key.in_([row.source_key for row in plan.rows]))
    if lock_rows:
        statement = statement.order_by(SheetImportRecord.id).with_for_update()
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


def _load_discord_accounts(
    session: Session,
    *,
    plan: LegacyIdentityPointImportPlan,
    lock_rows: bool,
) -> dict[str, DiscordAccount]:
    statement = select(DiscordAccount).where(
        DiscordAccount.discord_user_id.in_([row.discord_user_id for row in plan.rows])
    )
    if lock_rows:
        statement = statement.with_for_update()
    return {account.discord_user_id: account for account in session.scalars(statement)}


def _validate_existing_import_record(
    session: Session,
    *,
    plan: LegacyIdentityPointImportPlan,
    row: LegacyIdentityPointPlanRow,
    record: SheetImportRecord,
    import_run: SheetImportRun | None,
    source_checksum: str,
) -> LegacyIdentityPointImportConflict | None:
    if import_run is None:
        return _conflict(row, "import_run_missing", "source record import run is missing")
    if (
        import_run.import_kind != LEGACY_IDENTITY_POINT_IMPORT_KIND
        or import_run.source_type != "xlsx"
        or import_run.source_identifier != plan.source_identifier
    ):
        return _conflict(row, "import_run_provenance_changed", "source import run provenance no longer matches")
    if import_run.source_checksum != source_checksum:
        return _conflict(row, "import_checksum_changed", "source workbook checksum changed after import")
    if import_run.status != "completed" or import_run.finished_at is None:
        return _conflict(row, "import_run_not_completed", "source import run is not completed")
    if record.source_sheet_name != plan.source_sheet_name or record.source_row_number != row.source_row_number:
        return _conflict(row, "source_location_changed", "source record location changed after import")
    if record.row_fingerprint != row.row_fingerprint:
        return _conflict(row, "row_fingerprint_changed", "source row content changed after import")
    if record.record_type != LEGACY_IDENTITY_POINT_RECORD_TYPE or record.status != "applied":
        return _conflict(row, "import_record_not_applied", "source record is not a completed identity import")
    if record.target_entity_type != "game_account" or record.target_entity_id is None:
        return _conflict(row, "import_target_missing", "source record has no game account target")
    game_account = session.get(GameAccount, record.target_entity_id)
    if game_account is None or game_account.discord_account_id is None or game_account.persona_id is None:
        return _conflict(row, "game_account_missing", "imported game account no longer exists")
    discord_account = session.get(DiscordAccount, game_account.discord_account_id)
    persona = session.get(Persona, game_account.persona_id)
    point_account = session.scalar(
        select(CirclePointAccount).where(CirclePointAccount.persona_id == game_account.persona_id)
    )
    if discord_account is None or discord_account.discord_user_id != row.discord_user_id:
        return _conflict(row, "discord_identity_changed", "imported Discord identity no longer matches")
    if persona is None or discord_account.persona_id != persona.id or game_account.persona_id != persona.id:
        return _conflict(row, "persona_ownership_changed", "imported Persona ownership no longer matches")
    if game_account.nickname != row.nickname or game_account.ingame_name != row.ingame_name:
        return _conflict(row, "game_identity_changed", "imported legacy names no longer match")
    if point_account is None:
        return _conflict(row, "point_account_missing", "imported room point account no longer exists")
    detail = record.detail_json if isinstance(record.detail_json, dict) else {}
    scaled_opening_balance = scale_legacy_room_point_amount(
        row.opening_balance,
        field_name=f"legacy opening balance at source row {row.source_row_number}",
    )
    scaled_current_balance = scale_legacy_circle_point_balance(
        row.current_balance,
        field_name=f"legacy current balance at source row {row.source_row_number}",
    )
    if detail.get("discord_account_id") != discord_account.id or detail.get("game_account_id") != game_account.id:
        return _conflict(row, "import_audit_changed", "stored import audit ownership no longer matches")
    if detail.get("point_account_id") != point_account.id:
        return _conflict(row, "import_audit_changed", "stored import audit wallet no longer matches")
    backfill_tasks = tuple(
        session.scalars(select(IdentityBackfillTask).where(IdentityBackfillTask.game_account_id == game_account.id))
    )
    if (
        len(backfill_tasks) != 1
        or backfill_tasks[0].source_import_record_id != record.id
        or backfill_tasks[0].status not in {"pending", "conflict", "resolved"}
    ):
        return _conflict(row, "identity_backfill_task_changed", "identity backfill provenance no longer matches")
    if not _identity_point_audit_matches(
        detail,
        audit_contract=classify_legacy_room_point_audit_contract(import_run.summary_json),
        row=row,
        scaled_opening_balance=scaled_opening_balance,
        scaled_current_balance=scaled_current_balance,
    ):
        return _conflict(row, "import_audit_changed", "stored import audit values no longer match")
    transaction_count, transaction_total = session.execute(
        select(func.count(CirclePointTransaction.id), func.coalesce(func.sum(CirclePointTransaction.amount), 0)).where(
            CirclePointTransaction.persona_id == persona.id
        )
    ).one()
    if transaction_count == 0:
        if point_account.balance != scaled_current_balance:
            return _conflict(row, "point_balance_changed", "imported point snapshot no longer matches")
    elif point_account.balance != transaction_total:
        return _conflict(row, "point_ledger_mismatch", "Persona wallet no longer reconciles to its ledger")
    return None


def _identity_point_audit_matches(
    detail: dict[str, object],
    *,
    audit_contract: int,
    row: LegacyIdentityPointPlanRow,
    scaled_opening_balance: int,
    scaled_current_balance: int,
) -> bool:
    if audit_contract == LEGACY_ROOM_POINT_AUDIT_CONTRACT_V1:
        return (
            detail.get("audit_contract_version") is None
            and detail.get("opening_balance") == row.opening_balance
            and detail.get("imported_balance") == row.current_balance
        )
    return (
        audit_contract == LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION
        and detail.get("audit_contract_version") == LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION
        and detail.get("room_point_scale") == EXPECTED_ROOM_POINT_SCALE
        and detail.get("opening_balance") == row.opening_balance
        and detail.get("imported_balance") == row.current_balance
        and detail.get("source_opening_balance") == row.opening_balance
        and detail.get("source_imported_balance") == row.current_balance
        and detail.get("target_opening_balance") == scaled_opening_balance
        and detail.get("target_imported_balance") == scaled_current_balance
    )


def _create_pending_identity_point_row(
    session: Session,
    *,
    import_run: SheetImportRun,
    plan: LegacyIdentityPointImportPlan,
    row: LegacyIdentityPointPlanRow,
) -> None:
    discord_account = DiscordAccount(
        discord_user_id=row.discord_user_id,
        discord_nickname=row.nickname,
    )
    session.add(discord_account)
    session.flush()
    persona = Persona(
        display_name=row.nickname,
        display_name_source="discord",
        status="inactive",
    )
    session.add(persona)
    session.flush()
    discord_account.persona_id = persona.id
    game_account = GameAccount(
        discord_account_id=discord_account.id,
        persona_id=persona.id,
        uma_pid=None,
        nickname=row.nickname,
        ingame_name=row.ingame_name,
        identity_status=IdentityStatus.PENDING.value,
    )
    session.add(game_account)
    session.flush()
    scaled_opening_balance = scale_legacy_room_point_amount(
        row.opening_balance,
        field_name=f"legacy opening balance at source row {row.source_row_number}",
    )
    scaled_current_balance = scale_legacy_circle_point_balance(
        row.current_balance,
        field_name=f"legacy current balance at source row {row.source_row_number}",
    )
    point_account = CirclePointAccount(persona_id=persona.id, balance=scaled_current_balance)
    session.add(point_account)
    session.flush()
    import_record = SheetImportRecord(
        import_run_id=import_run.id,
        source_key=row.source_key,
        row_fingerprint=row.row_fingerprint,
        source_sheet_name=plan.source_sheet_name,
        source_row_number=row.source_row_number,
        record_type=LEGACY_IDENTITY_POINT_RECORD_TYPE,
        status="applied",
        target_entity_type="game_account",
        target_entity_id=game_account.id,
        detail_json={
            "audit_contract_version": LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION,
            "discord_account_id": discord_account.id,
            "game_account_id": game_account.id,
            "point_account_id": point_account.id,
            "room_point_scale": EXPECTED_ROOM_POINT_SCALE,
            "source_opening_balance": row.opening_balance,
            "source_imported_balance": row.current_balance,
            "opening_balance": row.opening_balance,
            "imported_balance": row.current_balance,
            "target_opening_balance": scaled_opening_balance,
            "target_imported_balance": scaled_current_balance,
        },
    )
    session.add(import_record)
    session.flush()
    session.add(
        IdentityBackfillTask(
            game_account_id=game_account.id,
            source_import_record_id=import_record.id,
            status="pending",
        )
    )
    session.flush()


def _conflict(
    row: LegacyIdentityPointPlanRow,
    code: str,
    message: str,
) -> LegacyIdentityPointImportConflict:
    return LegacyIdentityPointImportConflict(
        source_row_number=row.source_row_number,
        code=code,
        message=message,
    )


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("legacy import requires a session without pending changes")
