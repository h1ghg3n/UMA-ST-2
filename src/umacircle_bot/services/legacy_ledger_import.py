from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Bet,
    BetJudgement,
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    Race,
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
from umacircle_bot.domain.time import source_datetime_to_utc
from umacircle_bot.runtime_preflight import EXPECTED_ROOM_POINT_SCALE
from umacircle_bot.services.legacy_import import (
    LEGACY_IDENTITY_POINT_IMPORT_KIND,
    LEGACY_IDENTITY_POINT_RECORD_TYPE,
)
from umacircle_bot.services.legacy_point_scale import (
    LEGACY_ROOM_POINT_AUDIT_CONTRACT_V1,
    LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION,
    classify_legacy_room_point_audit_contract,
    require_current_legacy_room_point_import_target,
    scale_legacy_circle_point_balance,
    scale_legacy_room_point_amount,
)
from umacircle_bot.services.legacy_race_import import (
    DEFAULT_RACE_SHEET_NAME,
    LEGACY_RACE_IMPORT_KIND,
    LEGACY_RACE_RECORD_TYPE,
)
from umacircle_bot.sheets.legacy_room_ledger import (
    LegacyLedgerEntryKind,
    LegacyRoomLedgerReconciliation,
    LegacyRoomLedgerRow,
    LegacyRoomRaceSourceRow,
)
from umacircle_bot.sheets.row_parsers import RoomPointSourceRow

LEGACY_LEDGER_IMPORT_KIND = "legacy_room_ledger"
LEGACY_OPENING_RECORD_TYPE = "legacy_opening_balance"
LEGACY_LEDGER_RECORD_TYPE = "legacy_room_ledger_entry"
DEFAULT_LEDGER_SHEET_NAME = "베팅 Data"
DEFAULT_POINT_SHEET_NAME = "동친 포인트"
DERIVED_OPENING_SHEET_NAME = "__derived_opening_balance__"
LEDGER_TRANSACTION_SOURCE = "legacy_room_ledger"
PAYOUT_RATE_STORAGE_QUANTUM = Decimal("0.01")


@dataclass(frozen=True)
class LegacyOpeningPlanRow:
    source_row_number: int
    source_key: str
    row_fingerprint: str
    identity_source_key: str
    discord_user_id: str
    participant_name: str
    opening_balance: int
    current_balance: int
    effective_at: datetime | None


@dataclass(frozen=True)
class LegacyLedgerEntryPlanRow:
    source_row_number: int
    source_key: str
    row_fingerprint: str
    discord_user_id: str
    participant_name: str
    entry_kind: LegacyLedgerEntryKind
    race_source_key: str | None
    race_external_id: str | None
    occurred_at: datetime | None
    bet_type: str | None
    numbers: tuple[int, ...]
    amount: int
    is_cancelled: bool
    payout_rate: Decimal
    payout_amount: int
    net_change: int
    has_payout_warning: bool


@dataclass(frozen=True)
class LegacyLedgerImportPlan:
    source_identifier: str
    ledger_sheet_name: str
    openings: tuple[LegacyOpeningPlanRow, ...]
    entries: tuple[LegacyLedgerEntryPlanRow, ...]

    @property
    def source_record_count(self) -> int:
        return len(self.openings) + len(self.entries)

    @property
    def expected_transaction_count(self) -> int:
        return len(self.openings) + sum(
            1 if row.entry_kind is LegacyLedgerEntryKind.GRANT else 0 if row.is_cancelled else 2 for row in self.entries
        )

    @property
    def expected_bet_count(self) -> int:
        return sum(row.entry_kind is LegacyLedgerEntryKind.BET for row in self.entries)

    @property
    def expected_balance_total(self) -> int:
        return sum(row.current_balance for row in self.openings)


@dataclass(frozen=True)
class LegacyLedgerImportConflict:
    source_row_number: int
    code: str
    message: str


@dataclass(frozen=True)
class LegacyLedgerImportPreview:
    new_count: int
    skipped_count: int
    expected_bet_count: int
    expected_transaction_count: int
    expected_balance_total: int
    payout_warning_count: int
    conflicts: tuple[LegacyLedgerImportConflict, ...]

    @property
    def can_apply(self) -> bool:
        return not self.conflicts

    @property
    def source_expected_balance_total(self) -> int:
        return self.expected_balance_total

    @property
    def target_expected_balance_total(self) -> int:
        return self.expected_balance_total * EXPECTED_ROOM_POINT_SCALE


@dataclass(frozen=True)
class LegacyLedgerImportResult:
    import_run_id: int
    created_count: int
    skipped_count: int
    bet_count: int
    transaction_count: int
    payout_warning_count: int


@dataclass(frozen=True)
class _ImportContext:
    game_accounts_by_discord_id: dict[str, GameAccount]
    point_accounts_by_game_id: dict[int, CirclePointAccount]
    persona_ids_by_game_id: dict[int, str]
    races_by_external_id: dict[str, Race]


def build_legacy_ledger_import_plan(
    ledger_rows: tuple[LegacyRoomLedgerRow, ...],
    point_rows: tuple[RoomPointSourceRow, ...],
    race_rows: tuple[LegacyRoomRaceSourceRow, ...],
    reconciliation: LegacyRoomLedgerReconciliation,
    *,
    source_identifier: str,
    source_utc_offset_minutes: int,
    ledger_sheet_name: str = DEFAULT_LEDGER_SHEET_NAME,
    point_sheet_name: str = DEFAULT_POINT_SHEET_NAME,
) -> LegacyLedgerImportPlan:
    normalized_source = normalize_import_source_identifier(source_identifier)
    normalized_ledger_sheet = normalize_import_sheet_name(ledger_sheet_name)
    normalized_point_sheet = normalize_import_sheet_name(point_sheet_name)
    if reconciliation.has_errors:
        raise LegacyImportError("legacy ledger reconciliation contains errors")
    if not ledger_rows or not point_rows or not race_rows:
        raise LegacyImportError("legacy ledger import requires ledger, point, and race rows")

    points_by_name = _unique_by(point_rows, key_name="nickname")
    identities_by_name = _unique_by(reconciliation.identities, key_name="participant_name")
    races_by_label = _unique_by(race_rows, key_name="race_label")
    ledger_by_participant: dict[str, list[LegacyRoomLedgerRow]] = {}
    for row in sorted(ledger_rows, key=lambda item: item.row_number):
        ledger_by_participant.setdefault(row.participant_name, []).append(row)

    openings: list[LegacyOpeningPlanRow] = []
    for participant_name in sorted(points_by_name):
        point = points_by_name[participant_name]
        identity = identities_by_name.get(participant_name)
        participant_rows = ledger_by_participant.get(participant_name, [])
        if identity is None or not participant_rows:
            raise LegacyImportError(f"legacy participant is not fully reconciled: {participant_name}")
        if identity.discord_user_id != point.discord_user_id:
            raise LegacyImportError(f"legacy Discord identity mismatch: {participant_name}")
        if identity.current_balance != point.points or identity.reconstructed_balance != point.points:
            raise LegacyImportError(f"legacy balance mismatch: {participant_name}")
        source_effective_at = next((row.occurred_at for row in participant_rows if row.occurred_at is not None), None)
        effective_at = (
            source_datetime_to_utc(source_effective_at, utc_offset_minutes=source_utc_offset_minutes)
            if source_effective_at is not None
            else None
        )
        identity_source_key = build_sheet_import_source_key(
            source_identifier=normalized_source,
            sheet_name=normalized_point_sheet,
            row_number=point.row_number,
        )
        source_key = build_sheet_import_source_key(
            source_identifier=normalized_source,
            sheet_name=DERIVED_OPENING_SHEET_NAME,
            row_number=point.row_number,
        )
        fingerprint = build_import_row_fingerprint(
            (
                point.discord_user_id,
                participant_name,
                identity.opening_balance,
                point.points,
                effective_at.isoformat() if effective_at is not None else None,
            )
        )
        openings.append(
            LegacyOpeningPlanRow(
                source_row_number=point.row_number,
                source_key=source_key,
                row_fingerprint=fingerprint,
                identity_source_key=identity_source_key,
                discord_user_id=point.discord_user_id,
                participant_name=participant_name,
                opening_balance=identity.opening_balance,
                current_balance=point.points,
                effective_at=effective_at,
            )
        )

    warning_rows = {
        issue.row_number
        for issue in reconciliation.issues
        if issue.severity == "warning" and issue.code == "payout_rate_mismatch" and issue.row_number is not None
    }
    entries: list[LegacyLedgerEntryPlanRow] = []
    for row in sorted(ledger_rows, key=lambda item: item.row_number):
        point = points_by_name.get(row.participant_name)
        if point is None:
            raise LegacyImportError(f"ledger row has no point identity at row {row.row_number}")
        race_source_key: str | None = None
        race_external_id: str | None = None
        if row.entry_kind is LegacyLedgerEntryKind.BET:
            race = races_by_label.get(row.race_label)
            if race is None:
                raise LegacyImportError(f"ledger row has no race mapping at row {row.row_number}")
            race_external_id = str(race.external_race_id)
            race_source_key = build_sheet_import_source_key(
                source_identifier=normalized_source,
                sheet_name=DEFAULT_RACE_SHEET_NAME,
                row_number=race.row_number,
            )
        occurred_at = (
            source_datetime_to_utc(row.occurred_at, utc_offset_minutes=source_utc_offset_minutes)
            if row.occurred_at is not None
            else None
        )
        fingerprint = build_import_row_fingerprint(
            (
                occurred_at.isoformat() if occurred_at is not None else None,
                row.race_label,
                row.participant_name,
                row.entry_kind.value,
                row.bet_type,
                ",".join(str(number) for number in row.numbers),
                row.amount,
                row.is_cancelled,
                row.balance_before,
                str(row.payout_rate),
                row.payout_amount,
                row.balance_after,
                row.net_change,
            )
        )
        entries.append(
            LegacyLedgerEntryPlanRow(
                source_row_number=row.row_number,
                source_key=build_sheet_import_source_key(
                    source_identifier=normalized_source,
                    sheet_name=normalized_ledger_sheet,
                    row_number=row.row_number,
                ),
                row_fingerprint=fingerprint,
                discord_user_id=point.discord_user_id,
                participant_name=row.participant_name,
                entry_kind=row.entry_kind,
                race_source_key=race_source_key,
                race_external_id=race_external_id,
                occurred_at=occurred_at,
                bet_type=row.bet_type,
                numbers=row.numbers,
                amount=row.amount,
                is_cancelled=row.is_cancelled,
                payout_rate=row.payout_rate,
                payout_amount=row.payout_amount,
                net_change=row.net_change,
                has_payout_warning=row.row_number in warning_rows,
            )
        )

    plan = LegacyLedgerImportPlan(
        source_identifier=normalized_source,
        ledger_sheet_name=normalized_ledger_sheet,
        openings=tuple(openings),
        entries=tuple(entries),
    )
    _validate_scalable_plan(plan)
    if plan.expected_transaction_count != len(reconciliation.movements):
        raise LegacyImportError("legacy movement count does not match reconciliation")
    reconstructed_total = sum(row.opening_balance for row in openings) + sum(row.net_change for row in entries)
    if reconstructed_total != plan.expected_balance_total:
        raise LegacyImportError("legacy movement total does not match current balances")
    if set(identities_by_name) != set(points_by_name):
        raise LegacyImportError("legacy identity and point participant sets differ")
    return plan


def preview_legacy_ledger_import(
    session: Session,
    *,
    plan: LegacyLedgerImportPlan,
    source_checksum: str,
) -> LegacyLedgerImportPreview:
    _require_clean_session(session)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    return _inspect_legacy_ledger_import(
        session,
        plan=plan,
        source_checksum=normalized_checksum,
        lock_rows=False,
    )


def apply_legacy_ledger_import(
    session: Session,
    *,
    plan: LegacyLedgerImportPlan,
    source_checksum: str,
) -> LegacyLedgerImportResult:
    _require_clean_session(session)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    with session.begin_nested():
        preview = _inspect_legacy_ledger_import(
            session,
            plan=plan,
            source_checksum=normalized_checksum,
            lock_rows=True,
        )
        if preview.conflicts:
            first = preview.conflicts[0]
            raise LegacyImportConflictError(
                f"legacy ledger import conflict at row {first.source_row_number}: {first.code}"
            )

        records = _load_plan_records(session, plan=plan, lock_rows=True)
        if records:
            import_run = session.get(SheetImportRun, next(iter(records.values())).import_run_id)
            if import_run is None:
                raise LegacyImportConflictError("legacy ledger retry import run disappeared")
            created_count = 0
            skipped_count = plan.source_record_count
            bet_count = 0
            transaction_count = 0
        else:
            import_run = SheetImportRun(
                import_kind=LEGACY_LEDGER_IMPORT_KIND,
                source_type="xlsx",
                source_identifier=plan.source_identifier,
                source_checksum=normalized_checksum,
                status="running",
            )
            session.add(import_run)
            session.flush()
            context, conflicts = _load_prerequisite_context(
                session,
                plan=plan,
                source_checksum=normalized_checksum,
                lock_rows=True,
                records={},
            )
            if conflicts:
                first = conflicts[0]
                raise LegacyImportConflictError(
                    f"legacy ledger prerequisite conflict at row {first.source_row_number}: {first.code}"
                )
            transaction_count = _create_ledger_rows(session, import_run=import_run, plan=plan, context=context)
            _verify_imported_transaction_totals(session, plan=plan, context=context)
            created_count = plan.source_record_count
            skipped_count = 0
            bet_count = plan.expected_bet_count

            import_run.status = "completed"
            import_run.finished_at = datetime.now(UTC)
            import_run.summary_json = {
                "audit_contract_version": LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION,
                "created_count": created_count,
                "skipped_count": skipped_count,
                "bet_count": bet_count,
                "transaction_count": transaction_count,
                "payout_warning_count": sum(row.has_payout_warning for row in plan.entries),
                "expected_balance_total": plan.expected_balance_total,
                "source_expected_balance_total": plan.expected_balance_total,
                "target_expected_balance_total": plan.expected_balance_total * EXPECTED_ROOM_POINT_SCALE,
                "room_point_scale": EXPECTED_ROOM_POINT_SCALE,
            }
            session.flush()

    return LegacyLedgerImportResult(
        import_run_id=import_run.id,
        created_count=created_count,
        skipped_count=skipped_count,
        bet_count=bet_count,
        transaction_count=transaction_count,
        payout_warning_count=sum(row.has_payout_warning for row in plan.entries),
    )


def _inspect_legacy_ledger_import(
    session: Session,
    *,
    plan: LegacyLedgerImportPlan,
    source_checksum: str,
    lock_rows: bool,
) -> LegacyLedgerImportPreview:
    require_current_legacy_room_point_import_target(session)
    records = _load_plan_records(session, plan=plan, lock_rows=lock_rows)
    if records and len(records) != plan.source_record_count:
        first_missing = next(row for row in (*plan.openings, *plan.entries) if row.source_key not in records)
        conflicts = (
            _conflict(
                first_missing.source_row_number,
                "partial_import_state",
                "legacy ledger source records are only partially present",
            ),
        )
        return _preview(plan, new_count=0, skipped_count=len(records), conflicts=conflicts)
    if len({record.import_run_id for record in records.values()}) > 1:
        conflicts = (
            _conflict(
                0,
                "import_run_ambiguous",
                "legacy ledger source records have multiple parent runs",
            ),
        )
        return _preview(plan, new_count=0, skipped_count=len(records), conflicts=conflicts)

    context, prerequisite_conflicts = _load_prerequisite_context(
        session,
        plan=plan,
        source_checksum=source_checksum,
        lock_rows=lock_rows,
        records=records,
    )
    conflicts = list(prerequisite_conflicts)
    if records:
        import_runs = _load_import_runs(session, records=records.values(), lock_rows=lock_rows)
        conflicts.extend(
            _validate_existing_records(
                session,
                plan=plan,
                source_checksum=source_checksum,
                records=records,
                import_runs=import_runs,
                context=context,
            )
        )
        new_count = 0
        skipped_count = len(records)
    elif not conflicts:
        conflicts.extend(_validate_initial_state(session, plan=plan, context=context, lock_rows=lock_rows))
        new_count = plan.source_record_count if not conflicts else 0
        skipped_count = 0
    else:
        new_count = 0
        skipped_count = 0
    return _preview(plan, new_count=new_count, skipped_count=skipped_count, conflicts=tuple(conflicts))


def _preview(
    plan: LegacyLedgerImportPlan,
    *,
    new_count: int,
    skipped_count: int,
    conflicts: tuple[LegacyLedgerImportConflict, ...],
) -> LegacyLedgerImportPreview:
    return LegacyLedgerImportPreview(
        new_count=new_count,
        skipped_count=skipped_count,
        expected_bet_count=plan.expected_bet_count,
        expected_transaction_count=plan.expected_transaction_count,
        expected_balance_total=plan.expected_balance_total,
        payout_warning_count=sum(row.has_payout_warning for row in plan.entries),
        conflicts=conflicts,
    )


def _load_plan_records(
    session: Session,
    *,
    plan: LegacyLedgerImportPlan,
    lock_rows: bool,
) -> dict[str, SheetImportRecord]:
    keys = [row.source_key for row in (*plan.openings, *plan.entries)]
    statement = select(SheetImportRecord).where(SheetImportRecord.source_key.in_(keys))
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


def _load_prerequisite_context(
    session: Session,
    *,
    plan: LegacyLedgerImportPlan,
    source_checksum: str,
    lock_rows: bool,
    records: dict[str, SheetImportRecord],
) -> tuple[_ImportContext, tuple[LegacyLedgerImportConflict, ...]]:
    conflicts: list[LegacyLedgerImportConflict] = []
    identity_keys = [row.identity_source_key for row in plan.openings]
    identity_statement = select(SheetImportRecord).where(SheetImportRecord.source_key.in_(identity_keys))
    if lock_rows:
        identity_statement = identity_statement.with_for_update()
    identity_records = {record.source_key: record for record in session.scalars(identity_statement)}
    identity_runs = _load_import_runs(session, records=identity_records.values(), lock_rows=lock_rows)

    game_accounts: dict[str, GameAccount] = {}
    point_accounts: dict[int, CirclePointAccount] = {}
    persona_ids: dict[int, str] = {}
    for opening in plan.openings:
        record = identity_records.get(opening.identity_source_key)
        if (
            record is None
            or record.record_type != LEGACY_IDENTITY_POINT_RECORD_TYPE
            or record.status != "applied"
            or record.target_entity_type != "game_account"
            or record.target_entity_id is None
        ):
            conflicts.append(
                _conflict(opening.source_row_number, "identity_import_missing", "required identity import is missing")
            )
            continue
        identity_run = identity_runs.get(record.import_run_id)
        if not _prerequisite_run_matches(
            identity_run,
            import_kind=LEGACY_IDENTITY_POINT_IMPORT_KIND,
            source_identifier=plan.source_identifier,
            source_checksum=source_checksum,
        ):
            conflicts.append(
                _conflict(
                    opening.source_row_number,
                    "identity_import_changed",
                    "required identity import provenance changed",
                )
            )
            continue
        game_account = session.get(GameAccount, record.target_entity_id)
        if game_account is None or game_account.discord_account_id is None:
            conflicts.append(
                _conflict(opening.source_row_number, "game_account_missing", "required game account is missing")
            )
            continue
        discord_account = session.get(DiscordAccount, game_account.discord_account_id)
        persona_id = game_account.persona_id
        ledger_record = records.get(opening.source_key)
        if ledger_record is not None:
            detail = ledger_record.detail_json if isinstance(ledger_record.detail_json, dict) else {}
            audited_persona_id = detail.get("persona_id")
            opening_transaction = (
                session.get(CirclePointTransaction, ledger_record.target_entity_id)
                if ledger_record.target_entity_id is not None
                else None
            )
            if isinstance(audited_persona_id, str) and audited_persona_id:
                persona_id = audited_persona_id
            elif opening_transaction is not None:
                persona_id = opening_transaction.persona_id
            else:
                persona_id = None
                conflicts.append(
                    _conflict(
                        opening.source_row_number,
                        "owner_snapshot_missing",
                        "legacy ledger Persona owner snapshot is missing",
                    )
                )
        point_account_statement = select(CirclePointAccount).where(CirclePointAccount.persona_id == persona_id)
        if lock_rows:
            point_account_statement = point_account_statement.with_for_update()
        point_account = session.scalar(point_account_statement) if persona_id is not None else None
        if discord_account is None or discord_account.discord_user_id != opening.discord_user_id:
            conflicts.append(
                _conflict(opening.source_row_number, "discord_identity_changed", "Discord identity no longer matches")
            )
            continue
        if point_account is None:
            conflicts.append(
                _conflict(opening.source_row_number, "point_account_missing", "required point account is missing")
            )
            continue
        game_accounts[opening.discord_user_id] = game_account
        point_accounts[game_account.id] = point_account
        persona_ids[game_account.id] = persona_id

    race_source_keys = sorted({row.race_source_key for row in plan.entries if row.race_source_key is not None})
    race_statement = select(SheetImportRecord).where(SheetImportRecord.source_key.in_(race_source_keys))
    if lock_rows:
        race_statement = race_statement.with_for_update()
    race_records = {record.source_key: record for record in session.scalars(race_statement)}
    race_runs = _load_import_runs(session, records=race_records.values(), lock_rows=lock_rows)
    races: dict[str, Race] = {}
    for entry in plan.entries:
        if entry.race_source_key is None or entry.race_external_id is None:
            continue
        if entry.race_external_id in races:
            continue
        record = race_records.get(entry.race_source_key)
        if (
            record is None
            or record.record_type != LEGACY_RACE_RECORD_TYPE
            or record.status != "applied"
            or record.target_entity_type != "race"
            or record.target_entity_id is None
        ):
            conflicts.append(
                _conflict(entry.source_row_number, "race_import_missing", "required race import is missing")
            )
            continue
        race_run = race_runs.get(record.import_run_id)
        if not _prerequisite_run_matches(
            race_run,
            import_kind=LEGACY_RACE_IMPORT_KIND,
            source_identifier=plan.source_identifier,
            source_checksum=source_checksum,
        ):
            conflicts.append(
                _conflict(
                    entry.source_row_number,
                    "race_import_changed",
                    "required race import provenance changed",
                )
            )
            continue
        race = session.get(Race, record.target_entity_id)
        if (
            race is None
            or race.external_source != plan.source_identifier
            or race.external_race_id != entry.race_external_id
        ):
            conflicts.append(
                _conflict(entry.source_row_number, "race_identity_changed", "imported race identity no longer matches")
            )
            continue
        races[entry.race_external_id] = race

    return (
        _ImportContext(
            game_accounts_by_discord_id=game_accounts,
            point_accounts_by_game_id=point_accounts,
            persona_ids_by_game_id=persona_ids,
            races_by_external_id=races,
        ),
        tuple(conflicts),
    )


def _prerequisite_run_matches(
    import_run: SheetImportRun | None,
    *,
    import_kind: str,
    source_identifier: str,
    source_checksum: str,
) -> bool:
    return bool(
        import_run is not None
        and import_run.import_kind == import_kind
        and import_run.source_type == "xlsx"
        and import_run.source_identifier == source_identifier
        and import_run.source_checksum == source_checksum
        and import_run.status == "completed"
        and import_run.finished_at is not None
    )


def _validate_initial_state(
    session: Session,
    *,
    plan: LegacyLedgerImportPlan,
    context: _ImportContext,
    lock_rows: bool,
) -> tuple[LegacyLedgerImportConflict, ...]:
    conflicts: list[LegacyLedgerImportConflict] = []
    for opening in plan.openings:
        account = context.game_accounts_by_discord_id.get(opening.discord_user_id)
        if account is None:
            continue
        point_account = context.point_accounts_by_game_id.get(account.id)
        if account.identity_status != IdentityStatus.PENDING.value or account.uma_pid is not None:
            conflicts.append(
                _conflict(
                    opening.source_row_number,
                    "identity_not_pending",
                    "ledger import must run before PID confirmation",
                )
            )
        scaled_current_balance = scale_legacy_circle_point_balance(
            opening.current_balance,
            field_name=f"legacy current balance at source row {opening.source_row_number}",
        )
        if point_account is not None and point_account.balance != scaled_current_balance:
            conflicts.append(
                _conflict(
                    opening.source_row_number,
                    "point_balance_changed",
                    "point account no longer matches the imported snapshot",
                )
            )
    game_ids = [account.id for account in context.game_accounts_by_discord_id.values()]
    race_ids = [race.id for race in context.races_by_external_id.values()]
    tx_statement = (
        select(CirclePointTransaction.id).where(CirclePointTransaction.game_account_id.in_(game_ids)).limit(1)
    )
    bet_statement = select(Bet.id).where(Bet.race_id.in_(race_ids), Bet.betting_mode == "room_match").limit(1)
    if lock_rows:
        tx_statement = tx_statement.with_for_update()
        bet_statement = bet_statement.with_for_update()
    if game_ids and session.scalar(tx_statement) is not None:
        conflicts.append(_conflict(0, "point_transactions_exist", "point transactions already exist"))
    if race_ids and session.scalar(bet_statement) is not None:
        conflicts.append(_conflict(0, "room_bets_exist", "room-match bets already exist for imported races"))
    return tuple(conflicts)


def _create_ledger_rows(
    session: Session,
    *,
    import_run: SheetImportRun,
    plan: LegacyLedgerImportPlan,
    context: _ImportContext,
) -> int:
    transaction_count = 0
    for opening in plan.openings:
        game_account = context.game_accounts_by_discord_id[opening.discord_user_id]
        scaled_opening_balance = scale_legacy_room_point_amount(
            opening.opening_balance,
            field_name=f"legacy opening balance at source row {opening.source_row_number}",
        )
        scaled_current_balance = scale_legacy_circle_point_balance(
            opening.current_balance,
            field_name=f"legacy current balance at source row {opening.source_row_number}",
        )
        transaction = _new_transaction(
            game_account_id=game_account.id,
            persona_id=context.persona_ids_by_game_id[game_account.id],
            transaction_type=LEGACY_OPENING_RECORD_TYPE,
            amount=scaled_opening_balance,
            reason=f"legacy_opening:{opening.source_row_number}",
            created_at=opening.effective_at,
        )
        session.add(transaction)
        session.flush()
        session.add(
            SheetImportRecord(
                import_run_id=import_run.id,
                source_key=opening.source_key,
                row_fingerprint=opening.row_fingerprint,
                source_sheet_name=DERIVED_OPENING_SHEET_NAME,
                source_row_number=opening.source_row_number,
                record_type=LEGACY_OPENING_RECORD_TYPE,
                status="applied",
                target_entity_type="room_point_transaction",
                target_entity_id=transaction.id,
                detail_json={
                    "audit_contract_version": LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION,
                    "game_account_id": game_account.id,
                    "persona_id": context.persona_ids_by_game_id[game_account.id],
                    "room_point_scale": EXPECTED_ROOM_POINT_SCALE,
                    "source_opening_balance": opening.opening_balance,
                    "source_current_balance": opening.current_balance,
                    "opening_balance": opening.opening_balance,
                    "current_balance": opening.current_balance,
                    "target_opening_balance": scaled_opening_balance,
                    "target_current_balance": scaled_current_balance,
                },
            )
        )
        session.flush()
        transaction_count += 1

    for entry in plan.entries:
        game_account = context.game_accounts_by_discord_id[entry.discord_user_id]
        scaled_amount = scale_legacy_room_point_amount(
            entry.amount,
            field_name=f"legacy ledger amount at source row {entry.source_row_number}",
        )
        scaled_payout_amount = scale_legacy_room_point_amount(
            entry.payout_amount,
            field_name=f"legacy payout amount at source row {entry.source_row_number}",
        )
        if entry.entry_kind is LegacyLedgerEntryKind.GRANT:
            transaction = _new_transaction(
                game_account_id=game_account.id,
                persona_id=context.persona_ids_by_game_id[game_account.id],
                transaction_type="admin_grant",
                amount=scaled_amount,
                reason=f"legacy_grant:{entry.source_row_number}",
                created_at=entry.occurred_at,
            )
            session.add(transaction)
            session.flush()
            _add_entry_record(
                session,
                import_run=import_run,
                plan=plan,
                entry=entry,
                target_entity_type="room_point_transaction",
                target_entity_id=transaction.id,
                detail_json={
                    "transaction_id": transaction.id,
                    "game_account_id": game_account.id,
                    "persona_id": context.persona_ids_by_game_id[game_account.id],
                },
            )
            transaction_count += 1
            continue

        if entry.race_external_id is None or entry.bet_type is None:
            raise LegacyImportError("bet plan is missing race or bet type")
        race = context.races_by_external_id[entry.race_external_id]
        timestamp = entry.occurred_at or datetime.now(UTC)
        bet = Bet(
            event_id=race.event_id,
            race_id=race.id,
            persona_id=context.persona_ids_by_game_id[game_account.id],
            game_account_id=game_account.id,
            betting_mode="room_match",
            bet_type=entry.bet_type,
            numbers=list(entry.numbers),
            amount=scaled_amount,
            status="cancelled" if entry.is_cancelled else "settled",
            cancelled_at=timestamp if entry.is_cancelled else None,
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(bet)
        session.flush()
        if entry.is_cancelled:
            _add_entry_record(
                session,
                import_run=import_run,
                plan=plan,
                entry=entry,
                target_entity_type="bet",
                target_entity_id=bet.id,
                detail_json={
                    "bet_id": bet.id,
                    "cancelled": True,
                    "persona_id": context.persona_ids_by_game_id[game_account.id],
                },
            )
            continue

        is_hit = entry.payout_amount > 0
        judgement = BetJudgement(
            bet_id=bet.id,
            race_id=race.id,
            judgement_status="hit" if is_hit else "miss",
            is_hit=is_hit,
            payout_rate=legacy_payout_rate_storage_value(entry.payout_rate),
            stake_amount=scaled_amount,
            payout_amount=scaled_payout_amount,
            point_delta=scaled_payout_amount,
            judged_at=timestamp,
        )
        session.add(judgement)
        session.flush()
        stake = _new_transaction(
            game_account_id=game_account.id,
            persona_id=context.persona_ids_by_game_id[game_account.id],
            transaction_type="bet_stake",
            amount=-scaled_amount,
            reason=f"legacy_bet:{entry.source_row_number}",
            related_bet_id=bet.id,
            created_at=timestamp,
        )
        settlement = _new_transaction(
            game_account_id=game_account.id,
            persona_id=context.persona_ids_by_game_id[game_account.id],
            transaction_type="settlement_reward",
            amount=scaled_payout_amount,
            reason=f"legacy_settlement:{entry.source_row_number}",
            related_bet_id=bet.id,
            created_at=timestamp,
        )
        session.add_all([stake, settlement])
        session.flush()
        _add_entry_record(
            session,
            import_run=import_run,
            plan=plan,
            entry=entry,
            target_entity_type="bet",
            target_entity_id=bet.id,
            detail_json={
                "bet_id": bet.id,
                "judgement_id": judgement.id,
                "stake_transaction_id": stake.id,
                "settlement_transaction_id": settlement.id,
                "payout_rate_warning": entry.has_payout_warning,
                "persona_id": context.persona_ids_by_game_id[game_account.id],
            },
        )
        transaction_count += 2
    return transaction_count


def _new_transaction(
    *,
    game_account_id: int,
    persona_id: str,
    transaction_type: str,
    amount: int,
    reason: str,
    created_at: datetime | None,
    related_bet_id: int | None = None,
) -> CirclePointTransaction:
    values = {
        "persona_id": persona_id,
        "game_account_id": game_account_id,
        "type": transaction_type,
        "amount": amount,
        "reason": reason,
        "source": LEDGER_TRANSACTION_SOURCE,
        "related_bet_id": related_bet_id,
    }
    if created_at is not None:
        values["created_at"] = created_at
    return CirclePointTransaction(**values)


def _add_entry_record(
    session: Session,
    *,
    import_run: SheetImportRun,
    plan: LegacyLedgerImportPlan,
    entry: LegacyLedgerEntryPlanRow,
    target_entity_type: str,
    target_entity_id: int,
    detail_json: dict[str, object],
) -> None:
    scaled_amount = scale_legacy_room_point_amount(
        entry.amount,
        field_name=f"legacy ledger amount at source row {entry.source_row_number}",
    )
    scaled_payout_amount = scale_legacy_room_point_amount(
        entry.payout_amount,
        field_name=f"legacy payout amount at source row {entry.source_row_number}",
    )
    session.add(
        SheetImportRecord(
            import_run_id=import_run.id,
            source_key=entry.source_key,
            row_fingerprint=entry.row_fingerprint,
            source_sheet_name=plan.ledger_sheet_name,
            source_row_number=entry.source_row_number,
            record_type=LEGACY_LEDGER_RECORD_TYPE,
            status="applied",
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            detail_json={
                **detail_json,
                "audit_contract_version": LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION,
                "room_point_scale": EXPECTED_ROOM_POINT_SCALE,
                "source_amount": entry.amount,
                "target_amount": scaled_amount,
                "source_payout_amount": entry.payout_amount,
                "target_payout_amount": scaled_payout_amount,
                "source_payout_rate": format(entry.payout_rate, "f"),
                "target_payout_rate": format(legacy_payout_rate_storage_value(entry.payout_rate), "f"),
            },
        )
    )
    session.flush()


def _verify_imported_transaction_totals(
    session: Session,
    *,
    plan: LegacyLedgerImportPlan,
    context: _ImportContext,
) -> None:
    totals = dict(
        session.execute(
            select(CirclePointTransaction.game_account_id, func.sum(CirclePointTransaction.amount))
            .where(
                CirclePointTransaction.game_account_id.in_(
                    [account.id for account in context.game_accounts_by_discord_id.values()]
                )
            )
            .group_by(CirclePointTransaction.game_account_id)
        ).all()
    )
    for opening in plan.openings:
        account = context.game_accounts_by_discord_id[opening.discord_user_id]
        expected_total = scale_legacy_circle_point_balance(
            opening.current_balance,
            field_name=f"legacy current balance at source row {opening.source_row_number}",
        )
        if totals.get(account.id) != expected_total:
            raise LegacyImportError(f"imported transaction total mismatch at row {opening.source_row_number}")


def _validate_existing_records(
    session: Session,
    *,
    plan: LegacyLedgerImportPlan,
    source_checksum: str,
    records: dict[str, SheetImportRecord],
    import_runs: dict[int, SheetImportRun],
    context: _ImportContext,
) -> tuple[LegacyLedgerImportConflict, ...]:
    conflicts: list[LegacyLedgerImportConflict] = []
    for opening in plan.openings:
        record = records[opening.source_key]
        import_run = import_runs.get(record.import_run_id)
        run_conflict = _validate_record_import_run(
            plan=plan,
            source_checksum=source_checksum,
            record=record,
            import_run=import_run,
            source_row_number=opening.source_row_number,
        )
        if run_conflict is not None:
            conflicts.append(run_conflict)
            continue
        account = context.game_accounts_by_discord_id.get(opening.discord_user_id)
        if account is None:
            continue
        persona_id = context.persona_ids_by_game_id[account.id]
        if record.target_entity_id is None:
            conflicts.append(
                _conflict(opening.source_row_number, "opening_target_changed", "opening transaction changed")
            )
            continue
        if record.row_fingerprint != opening.row_fingerprint or record.record_type != LEGACY_OPENING_RECORD_TYPE:
            conflicts.append(_conflict(opening.source_row_number, "opening_record_changed", "opening record changed"))
            continue
        transaction = session.get(CirclePointTransaction, record.target_entity_id)
        detail = record.detail_json if isinstance(record.detail_json, dict) else {}
        scaled_opening_balance = scale_legacy_room_point_amount(
            opening.opening_balance,
            field_name=f"legacy opening balance at source row {opening.source_row_number}",
        )
        if (
            record.status != "applied"
            or record.target_entity_type != "room_point_transaction"
            or transaction is None
            or transaction.game_account_id != account.id
            or transaction.persona_id != persona_id
            or transaction.type != LEGACY_OPENING_RECORD_TYPE
            or transaction.source != LEDGER_TRANSACTION_SOURCE
            or transaction.related_bet_id is not None
            or transaction.amount != scaled_opening_balance
            or record.source_sheet_name != DERIVED_OPENING_SHEET_NAME
            or record.source_row_number != opening.source_row_number
            or detail.get("game_account_id") != account.id
            or (detail.get("persona_id") is not None and detail.get("persona_id") != persona_id)
            or not _opening_audit_matches(
                detail,
                audit_contract=classify_legacy_room_point_audit_contract(import_run.summary_json),
                opening=opening,
                scaled_opening_balance=scaled_opening_balance,
            )
        ):
            conflicts.append(
                _conflict(opening.source_row_number, "opening_target_changed", "opening transaction changed")
            )

    for entry in plan.entries:
        record = records[entry.source_key]
        import_run = import_runs.get(record.import_run_id)
        run_conflict = _validate_record_import_run(
            plan=plan,
            source_checksum=source_checksum,
            record=record,
            import_run=import_run,
            source_row_number=entry.source_row_number,
        )
        if run_conflict is not None:
            conflicts.append(run_conflict)
            continue
        if record.row_fingerprint != entry.row_fingerprint or record.record_type != LEGACY_LEDGER_RECORD_TYPE:
            conflicts.append(_conflict(entry.source_row_number, "ledger_record_changed", "ledger record changed"))
            continue
        if record.status != "applied" or record.target_entity_id is None:
            conflicts.append(_conflict(entry.source_row_number, "ledger_target_missing", "ledger target is missing"))
            continue
        if record.source_sheet_name != plan.ledger_sheet_name or record.source_row_number != entry.source_row_number:
            conflicts.append(_conflict(entry.source_row_number, "ledger_record_changed", "ledger location changed"))
            continue
        account = context.game_accounts_by_discord_id.get(entry.discord_user_id)
        if account is None:
            continue
        persona_id = context.persona_ids_by_game_id[account.id]
        if entry.entry_kind is LegacyLedgerEntryKind.GRANT:
            transaction = session.get(CirclePointTransaction, record.target_entity_id)
            detail = record.detail_json if isinstance(record.detail_json, dict) else {}
            scaled_amount = scale_legacy_room_point_amount(
                entry.amount,
                field_name=f"legacy ledger amount at source row {entry.source_row_number}",
            )
            if (
                record.target_entity_type != "room_point_transaction"
                or transaction is None
                or transaction.game_account_id != account.id
                or transaction.persona_id != persona_id
                or transaction.type != "admin_grant"
                or transaction.source != LEDGER_TRANSACTION_SOURCE
                or transaction.related_bet_id is not None
                or transaction.amount != scaled_amount
                or detail.get("transaction_id") != transaction.id
                or detail.get("game_account_id") != account.id
                or (detail.get("persona_id") is not None and detail.get("persona_id") != persona_id)
                or not _entry_audit_matches(
                    detail,
                    audit_contract=classify_legacy_room_point_audit_contract(import_run.summary_json),
                    entry=entry,
                )
            ):
                conflicts.append(_conflict(entry.source_row_number, "grant_target_changed", "grant target changed"))
            continue
        bet = session.get(Bet, record.target_entity_id)
        race = context.races_by_external_id.get(entry.race_external_id or "")
        scaled_amount = scale_legacy_room_point_amount(
            entry.amount,
            field_name=f"legacy ledger amount at source row {entry.source_row_number}",
        )
        if (
            record.target_entity_type != "bet"
            or bet is None
            or race is None
            or bet.persona_id != persona_id
            or bet.game_account_id != account.id
            or bet.event_id != race.event_id
            or bet.race_id != race.id
            or bet.betting_mode != "room_match"
            or bet.bet_type != entry.bet_type
            or tuple(bet.numbers) != entry.numbers
            or bet.amount != scaled_amount
            or bet.status != ("cancelled" if entry.is_cancelled else "settled")
        ):
            conflicts.append(_conflict(entry.source_row_number, "bet_target_changed", "bet target changed"))
            continue
        detail = record.detail_json if isinstance(record.detail_json, dict) else {}
        if detail.get("bet_id") != bet.id or not _entry_audit_matches(
            detail,
            audit_contract=classify_legacy_room_point_audit_contract(import_run.summary_json),
            entry=entry,
        ):
            conflicts.append(_conflict(entry.source_row_number, "ledger_audit_changed", "ledger audit changed"))
            continue
        if detail.get("persona_id") is not None and detail.get("persona_id") != persona_id:
            conflicts.append(_conflict(entry.source_row_number, "ledger_audit_changed", "ledger audit changed"))
            continue
        if entry.is_cancelled and detail.get("cancelled") is not True:
            conflicts.append(_conflict(entry.source_row_number, "ledger_audit_changed", "cancel audit changed"))
            continue
        if not entry.is_cancelled:
            conflicts.extend(
                _validate_settled_bet_targets(session, entry=entry, account=account, bet=bet, record=record)
            )
    conflicts.extend(_validate_wallet_ledger_totals(session, plan=plan, context=context))
    return tuple(conflicts)


def _validate_settled_bet_targets(
    session: Session,
    *,
    entry: LegacyLedgerEntryPlanRow,
    account: GameAccount,
    bet: Bet,
    record: SheetImportRecord,
) -> list[LegacyLedgerImportConflict]:
    detail = record.detail_json if isinstance(record.detail_json, dict) else {}
    judgement = session.get(BetJudgement, detail.get("judgement_id"))
    stake = session.get(CirclePointTransaction, detail.get("stake_transaction_id"))
    settlement = session.get(CirclePointTransaction, detail.get("settlement_transaction_id"))
    is_hit = entry.payout_amount > 0
    scaled_amount = scale_legacy_room_point_amount(
        entry.amount,
        field_name=f"legacy ledger amount at source row {entry.source_row_number}",
    )
    scaled_payout_amount = scale_legacy_room_point_amount(
        entry.payout_amount,
        field_name=f"legacy payout amount at source row {entry.source_row_number}",
    )
    if (
        judgement is None
        or judgement.bet_id != bet.id
        or judgement.race_id != bet.race_id
        or judgement.judgement_status != ("hit" if is_hit else "miss")
        or judgement.is_hit is not is_hit
        or judgement.payout_rate != legacy_payout_rate_storage_value(entry.payout_rate)
        or judgement.stake_amount != scaled_amount
        or judgement.payout_amount != scaled_payout_amount
        or judgement.point_delta != scaled_payout_amount
    ):
        return [_conflict(entry.source_row_number, "judgement_target_changed", "judgement target changed")]
    if (
        stake is None
        or settlement is None
        or stake.related_bet_id != bet.id
        or settlement.related_bet_id != bet.id
        or stake.game_account_id != account.id
        or settlement.game_account_id != account.id
        or stake.persona_id != bet.persona_id
        or settlement.persona_id != bet.persona_id
        or stake.type != "bet_stake"
        or settlement.type != "settlement_reward"
        or stake.source != LEDGER_TRANSACTION_SOURCE
        or settlement.source != LEDGER_TRANSACTION_SOURCE
        or stake.amount != -scaled_amount
        or settlement.amount != scaled_payout_amount
    ):
        return [_conflict(entry.source_row_number, "transaction_target_changed", "bet transactions changed")]
    if detail.get("payout_rate_warning") is not entry.has_payout_warning:
        return [_conflict(entry.source_row_number, "warning_audit_changed", "payout warning audit changed")]
    return []


def _validate_record_import_run(
    *,
    plan: LegacyLedgerImportPlan,
    source_checksum: str,
    record: SheetImportRecord,
    import_run: SheetImportRun | None,
    source_row_number: int,
) -> LegacyLedgerImportConflict | None:
    if import_run is None:
        return _conflict(source_row_number, "import_run_missing", "ledger import run is missing")
    if (
        import_run.import_kind != LEGACY_LEDGER_IMPORT_KIND
        or import_run.source_type != "xlsx"
        or import_run.source_identifier != plan.source_identifier
    ):
        return _conflict(source_row_number, "import_run_changed", "ledger import provenance changed")
    if import_run.source_checksum != source_checksum:
        return _conflict(source_row_number, "import_checksum_changed", "source workbook checksum changed")
    if import_run.status != "completed" or import_run.finished_at is None:
        return _conflict(source_row_number, "import_run_not_completed", "ledger import run is not completed")
    if record.import_run_id != import_run.id:
        return _conflict(source_row_number, "import_run_changed", "ledger import parent changed")
    return None


def _opening_audit_matches(
    detail: dict[str, object],
    *,
    audit_contract: int,
    opening: LegacyOpeningPlanRow,
    scaled_opening_balance: int,
) -> bool:
    if audit_contract == LEGACY_ROOM_POINT_AUDIT_CONTRACT_V1:
        return (
            detail.get("audit_contract_version") is None
            and detail.get("opening_balance") == opening.opening_balance
            and detail.get("current_balance") == opening.current_balance
        )
    return (
        audit_contract == LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION
        and detail.get("audit_contract_version") == LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION
        and detail.get("room_point_scale") == EXPECTED_ROOM_POINT_SCALE
        and detail.get("opening_balance") == opening.opening_balance
        and detail.get("current_balance") == opening.current_balance
        and detail.get("source_opening_balance") == opening.opening_balance
        and detail.get("source_current_balance") == opening.current_balance
        and detail.get("target_opening_balance") == scaled_opening_balance
        and detail.get("target_current_balance")
        == scale_legacy_circle_point_balance(
            opening.current_balance,
            field_name=f"legacy current balance at source row {opening.source_row_number}",
        )
    )


def _entry_audit_matches(
    detail: dict[str, object],
    *,
    audit_contract: int,
    entry: LegacyLedgerEntryPlanRow,
) -> bool:
    if audit_contract == LEGACY_ROOM_POINT_AUDIT_CONTRACT_V1:
        return detail.get("audit_contract_version") is None
    return (
        audit_contract == LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION
        and detail.get("audit_contract_version") == LEGACY_ROOM_POINT_AUDIT_CONTRACT_VERSION
        and detail.get("room_point_scale") == EXPECTED_ROOM_POINT_SCALE
        and detail.get("source_amount") == entry.amount
        and detail.get("target_amount")
        == scale_legacy_room_point_amount(
            entry.amount,
            field_name=f"legacy ledger amount at source row {entry.source_row_number}",
        )
        and detail.get("source_payout_amount") == entry.payout_amount
        and detail.get("target_payout_amount")
        == scale_legacy_room_point_amount(
            entry.payout_amount,
            field_name=f"legacy payout amount at source row {entry.source_row_number}",
        )
        and detail.get("source_payout_rate") == format(entry.payout_rate, "f")
        and detail.get("target_payout_rate") == format(legacy_payout_rate_storage_value(entry.payout_rate), "f")
    )


def _validate_wallet_ledger_totals(
    session: Session,
    *,
    plan: LegacyLedgerImportPlan,
    context: _ImportContext,
) -> list[LegacyLedgerImportConflict]:
    conflicts: list[LegacyLedgerImportConflict] = []
    checked_personas: set[str] = set()
    openings_by_discord_id = {opening.discord_user_id: opening for opening in plan.openings}
    for discord_user_id, account in context.game_accounts_by_discord_id.items():
        persona_id = context.persona_ids_by_game_id[account.id]
        if persona_id in checked_personas:
            continue
        checked_personas.add(persona_id)
        point_account = context.point_accounts_by_game_id.get(account.id)
        if point_account is None:
            continue
        transaction_total = session.scalar(
            select(func.coalesce(func.sum(CirclePointTransaction.amount), 0)).where(
                CirclePointTransaction.persona_id == persona_id
            )
        )
        if point_account.balance != transaction_total:
            opening = openings_by_discord_id[discord_user_id]
            conflicts.append(
                _conflict(
                    opening.source_row_number,
                    "point_ledger_mismatch",
                    "Persona wallet no longer reconciles to its ledger",
                )
            )
    return conflicts


def legacy_payout_rate_storage_value(value: Decimal) -> Decimal:
    return value.quantize(PAYOUT_RATE_STORAGE_QUANTUM, rounding=ROUND_HALF_UP)


def _validate_scalable_plan(plan: LegacyLedgerImportPlan) -> None:
    for opening in plan.openings:
        scale_legacy_room_point_amount(
            opening.opening_balance,
            field_name=f"legacy opening balance at source row {opening.source_row_number}",
        )
        scale_legacy_circle_point_balance(
            opening.current_balance,
            field_name=f"legacy current balance at source row {opening.source_row_number}",
        )
    for entry in plan.entries:
        scale_legacy_room_point_amount(
            entry.amount,
            field_name=f"legacy ledger amount at source row {entry.source_row_number}",
        )
        scale_legacy_room_point_amount(
            entry.payout_amount,
            field_name=f"legacy payout amount at source row {entry.source_row_number}",
        )


def _unique_by(rows: tuple[object, ...], *, key_name: str) -> dict[str, object]:
    indexed: dict[str, object] = {}
    for row in rows:
        value = getattr(row, key_name)
        if not isinstance(value, str):
            raise LegacyImportError(f"legacy key must be text: {key_name}")
        if value in indexed:
            raise LegacyImportError(f"duplicate legacy key: {value}")
        indexed[value] = row
    return indexed


def _conflict(source_row_number: int, code: str, message: str) -> LegacyLedgerImportConflict:
    return LegacyLedgerImportConflict(
        source_row_number=source_row_number,
        code=code,
        message=message,
    )


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("legacy import requires a session without pending changes")
