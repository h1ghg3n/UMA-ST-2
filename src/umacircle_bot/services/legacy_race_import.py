from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import Race, RaceCondition, SheetImportRecord, SheetImportRun
from umacircle_bot.domain.circle_match_seasons import circle_match_season_key
from umacircle_bot.domain.errors import LegacyImportConflictError, LegacyImportError
from umacircle_bot.domain.imports import (
    build_import_row_fingerprint,
    build_sheet_import_source_key,
    normalize_import_sheet_name,
    normalize_import_source_identifier,
    normalize_sha256_hex,
)
from umacircle_bot.domain.time import database_datetime_as_utc, source_datetime_to_utc
from umacircle_bot.sheets.legacy_room_ledger import LegacyRoomRaceSourceRow

LEGACY_RACE_IMPORT_KIND = "legacy_room_races"
LEGACY_RACE_RECORD_TYPE = "legacy_room_race"
DEFAULT_RACE_SHEET_NAME = "룸매치 Data"


@dataclass(frozen=True)
class LegacyRacePlanRow:
    source_row_number: int
    source_key: str
    row_fingerprint: str
    external_race_id: str
    raced_at: datetime
    circle_match_season_key: str
    race_name: str
    race_label: str
    grade: str
    venue: str
    track_surface: str
    distance: int
    direction: str
    season: str
    weather: str
    track_condition: str
    condition_label: str
    participant_count: int


@dataclass(frozen=True)
class LegacyRaceImportPlan:
    source_identifier: str
    source_sheet_name: str
    rows: tuple[LegacyRacePlanRow, ...]


@dataclass(frozen=True)
class LegacyRaceImportConflict:
    source_row_number: int
    code: str
    message: str


@dataclass(frozen=True)
class LegacyRaceImportPreview:
    new_count: int
    skipped_count: int
    conflicts: tuple[LegacyRaceImportConflict, ...]

    @property
    def can_apply(self) -> bool:
        return not self.conflicts


@dataclass(frozen=True)
class LegacyRaceImportResult:
    import_run_id: int
    created_count: int
    skipped_count: int


def build_legacy_race_import_plan(
    race_rows: tuple[LegacyRoomRaceSourceRow, ...],
    *,
    source_identifier: str,
    source_utc_offset_minutes: int,
    source_sheet_name: str = DEFAULT_RACE_SHEET_NAME,
) -> LegacyRaceImportPlan:
    normalized_source = normalize_import_source_identifier(source_identifier)
    normalized_sheet = normalize_import_sheet_name(source_sheet_name)
    if not race_rows:
        raise LegacyImportError("legacy race import requires at least one source row")

    planned: list[LegacyRacePlanRow] = []
    seen_external_ids: set[int] = set()
    seen_labels: set[str] = set()
    for row in sorted(race_rows, key=lambda item: item.row_number):
        if row.external_race_id in seen_external_ids:
            raise LegacyImportError(f"duplicate external race ID at source row {row.row_number}")
        if row.race_label in seen_labels:
            raise LegacyImportError(f"duplicate race label at source row {row.row_number}")
        raced_at_utc = source_datetime_to_utc(
            row.raced_at,
            utc_offset_minutes=source_utc_offset_minutes,
        )
        source_key = build_sheet_import_source_key(
            source_identifier=normalized_source,
            sheet_name=normalized_sheet,
            row_number=row.row_number,
        )
        fingerprint = build_import_row_fingerprint(
            (
                row.external_race_id,
                raced_at_utc.isoformat(),
                row.race_name,
                row.grade,
                row.venue,
                row.track_surface,
                row.distance,
                row.direction,
                row.season,
                row.weather,
                row.track_condition,
                row.condition_label,
                row.participant_count,
            )
        )
        planned.append(
            LegacyRacePlanRow(
                source_row_number=row.row_number,
                source_key=source_key,
                row_fingerprint=fingerprint,
                external_race_id=str(row.external_race_id),
                raced_at=raced_at_utc,
                circle_match_season_key=circle_match_season_key(raced_at_utc),
                race_name=row.race_name,
                race_label=row.race_label,
                grade=row.grade,
                venue=row.venue,
                track_surface=row.track_surface,
                distance=row.distance,
                direction=row.direction,
                season=row.season,
                weather=row.weather,
                track_condition=row.track_condition,
                condition_label=row.condition_label,
                participant_count=row.participant_count,
            )
        )
        seen_external_ids.add(row.external_race_id)
        seen_labels.add(row.race_label)
    return LegacyRaceImportPlan(
        source_identifier=normalized_source,
        source_sheet_name=normalized_sheet,
        rows=tuple(planned),
    )


def preview_legacy_race_import(session: Session, *, plan: LegacyRaceImportPlan) -> LegacyRaceImportPreview:
    _require_clean_session(session)
    return _inspect_legacy_race_import(session, plan=plan, source_checksum=None, lock_rows=False)


def apply_legacy_race_import(
    session: Session,
    *,
    plan: LegacyRaceImportPlan,
    source_checksum: str,
) -> LegacyRaceImportResult:
    _require_clean_session(session)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")

    with session.begin_nested():
        preview = _inspect_legacy_race_import(
            session,
            plan=plan,
            source_checksum=normalized_checksum,
            lock_rows=True,
        )
        if preview.conflicts:
            first = preview.conflicts[0]
            raise LegacyImportConflictError(
                f"legacy race import conflict at row {first.source_row_number}: {first.code}"
            )
        existing_records = _load_import_records(session, plan=plan, lock_rows=True)
        if preview.new_count == 0:
            matching_runs = _load_matching_race_import_runs(
                session,
                plan=plan,
                source_checksum=normalized_checksum,
                lock_rows=True,
            )
            if len(matching_runs) != 1 or not _is_completed_race_import_run(matching_runs[0]):
                raise LegacyImportConflictError("legacy race exact retry import run is missing or ambiguous")
            import_run = matching_runs[0]
            created_count = 0
            skipped_count = len(plan.rows)
        else:
            import_run = SheetImportRun(
                import_kind=LEGACY_RACE_IMPORT_KIND,
                source_type="xlsx",
                source_identifier=plan.source_identifier,
                source_checksum=normalized_checksum,
                status="running",
            )
            session.add(import_run)
            session.flush()

            created_count = 0
            skipped_count = 0
            for row in plan.rows:
                if row.source_key in existing_records:
                    skipped_count += 1
                    continue
                _create_race_row(session, import_run=import_run, plan=plan, row=row)
                created_count += 1

            import_run.status = "completed"
            import_run.finished_at = datetime.now(UTC)
            import_run.summary_json = {
                "created_count": created_count,
                "skipped_count": skipped_count,
            }
            session.flush()

    return LegacyRaceImportResult(
        import_run_id=import_run.id,
        created_count=created_count,
        skipped_count=skipped_count,
    )


def _inspect_legacy_race_import(
    session: Session,
    *,
    plan: LegacyRaceImportPlan,
    source_checksum: str | None,
    lock_rows: bool,
) -> LegacyRaceImportPreview:
    records = _load_import_records(session, plan=plan, lock_rows=lock_rows)
    races = _load_external_races(session, plan=plan, lock_rows=lock_rows)
    conflicts: list[LegacyRaceImportConflict] = []
    new_count = 0
    skipped_count = 0
    for row in plan.rows:
        record = records.get(row.source_key)
        race = races.get(row.external_race_id)
        if record is not None:
            conflict = _validate_existing_record(
                session,
                row=row,
                record=record,
                source_identifier=plan.source_identifier,
            )
            if conflict is not None:
                conflicts.append(conflict)
            else:
                skipped_count += 1
            continue
        if race is not None:
            conflicts.append(
                _conflict(
                    row,
                    "external_race_exists_without_source_record",
                    "external race already exists without the matching import record",
                )
            )
            continue
        new_count += 1
    if not conflicts and source_checksum is not None and new_count == 0:
        matching_runs = _load_matching_race_import_runs(
            session,
            plan=plan,
            source_checksum=source_checksum,
            lock_rows=lock_rows,
        )
        if len(matching_runs) != 1 or not _is_completed_race_import_run(matching_runs[0]):
            conflicts.append(
                _conflict(
                    plan.rows[0],
                    "import_checksum_changed" if not matching_runs else "import_run_ambiguous",
                    "race import run for the current workbook is missing or ambiguous",
                )
            )
    return LegacyRaceImportPreview(
        new_count=new_count,
        skipped_count=skipped_count,
        conflicts=tuple(conflicts),
    )


def _load_import_records(
    session: Session,
    *,
    plan: LegacyRaceImportPlan,
    lock_rows: bool,
) -> dict[str, SheetImportRecord]:
    statement = select(SheetImportRecord).where(SheetImportRecord.source_key.in_([row.source_key for row in plan.rows]))
    if lock_rows:
        statement = statement.with_for_update()
    return {record.source_key: record for record in session.scalars(statement)}


def _load_external_races(
    session: Session,
    *,
    plan: LegacyRaceImportPlan,
    lock_rows: bool,
) -> dict[str, Race]:
    statement = select(Race).where(
        Race.external_source == plan.source_identifier,
        Race.external_race_id.in_([row.external_race_id for row in plan.rows]),
    )
    if lock_rows:
        statement = statement.with_for_update()
    return {race.external_race_id: race for race in session.scalars(statement) if race.external_race_id is not None}


def _load_matching_race_import_runs(
    session: Session,
    *,
    plan: LegacyRaceImportPlan,
    source_checksum: str,
    lock_rows: bool,
) -> tuple[SheetImportRun, ...]:
    statement = (
        select(SheetImportRun)
        .where(
            SheetImportRun.import_kind == LEGACY_RACE_IMPORT_KIND,
            SheetImportRun.source_type == "xlsx",
            SheetImportRun.source_identifier == plan.source_identifier,
            SheetImportRun.source_checksum == source_checksum,
        )
        .order_by(SheetImportRun.id)
    )
    if lock_rows:
        statement = statement.with_for_update()
    return tuple(session.scalars(statement))


def _is_completed_race_import_run(import_run: SheetImportRun) -> bool:
    return import_run.status == "completed" and import_run.finished_at is not None


def _validate_existing_record(
    session: Session,
    *,
    row: LegacyRacePlanRow,
    record: SheetImportRecord,
    source_identifier: str,
) -> LegacyRaceImportConflict | None:
    import_run = session.get(SheetImportRun, record.import_run_id)
    if (
        import_run is None
        or import_run.import_kind != LEGACY_RACE_IMPORT_KIND
        or import_run.source_type != "xlsx"
        or import_run.source_identifier != source_identifier
        or import_run.status != "completed"
        or import_run.finished_at is None
    ):
        return _conflict(row, "import_run_changed", "race import provenance changed")
    if record.row_fingerprint != row.row_fingerprint:
        return _conflict(row, "row_fingerprint_changed", "source race content changed after import")
    if record.record_type != LEGACY_RACE_RECORD_TYPE or record.status != "applied":
        return _conflict(row, "import_record_not_applied", "source record is not a completed race import")
    if record.target_entity_type != "race" or record.target_entity_id is None:
        return _conflict(row, "import_target_missing", "source record has no race target")
    race = session.get(Race, record.target_entity_id)
    if race is None:
        return _conflict(row, "race_missing", "imported race no longer exists")
    condition = session.scalar(select(RaceCondition).where(RaceCondition.race_id == race.id))
    if condition is None:
        return _conflict(row, "race_condition_missing", "imported race condition no longer exists")
    if (
        race.external_source != source_identifier
        or race.external_race_id != row.external_race_id
        or race.name != row.race_name
        or race.starts_at is None
        or database_datetime_as_utc(race.starts_at) != row.raced_at
        or race.race_kind != "room_match"
        or race.status != "result_confirmed"
    ):
        return _conflict(row, "race_identity_changed", "imported race identity no longer matches")
    expected_condition = (
        row.grade,
        row.venue,
        row.track_surface,
        row.distance,
        row.direction,
        row.season,
        row.weather,
        row.track_condition,
        row.condition_label,
        row.participant_count,
    )
    actual_condition = (
        condition.grade,
        condition.venue,
        condition.track_surface,
        condition.distance,
        condition.direction,
        condition.season,
        condition.weather,
        condition.track_condition,
        condition.condition_label,
        condition.participant_count,
    )
    if actual_condition != expected_condition:
        return _conflict(row, "race_condition_changed", "imported race conditions no longer match")
    detail = record.detail_json if isinstance(record.detail_json, dict) else {}
    if detail.get("race_condition_id") != condition.id or detail.get("race_label") != row.race_label:
        return _conflict(row, "import_audit_changed", "stored race import audit values no longer match")
    return None


def _create_race_row(
    session: Session,
    *,
    import_run: SheetImportRun,
    plan: LegacyRaceImportPlan,
    row: LegacyRacePlanRow,
) -> None:
    race = Race(
        external_source=plan.source_identifier,
        external_race_id=row.external_race_id,
        race_kind="room_match",
        name=row.race_name,
        starts_at=row.raced_at,
        status="result_confirmed",
    )
    session.add(race)
    session.flush()
    condition = RaceCondition(
        race_id=race.id,
        grade=row.grade,
        venue=row.venue,
        track_surface=row.track_surface,
        distance=row.distance,
        direction=row.direction,
        season=row.season,
        weather=row.weather,
        track_condition=row.track_condition,
        condition_label=row.condition_label,
        participant_count=row.participant_count,
    )
    session.add(condition)
    session.flush()
    session.add(
        SheetImportRecord(
            import_run_id=import_run.id,
            source_key=row.source_key,
            row_fingerprint=row.row_fingerprint,
            source_sheet_name=plan.source_sheet_name,
            source_row_number=row.source_row_number,
            record_type=LEGACY_RACE_RECORD_TYPE,
            status="applied",
            target_entity_type="race",
            target_entity_id=race.id,
            detail_json={
                "race_condition_id": condition.id,
                "race_label": row.race_label,
            },
        )
    )
    session.flush()


def _conflict(row: LegacyRacePlanRow, code: str, message: str) -> LegacyRaceImportConflict:
    return LegacyRaceImportConflict(
        source_row_number=row.source_row_number,
        code=code,
        message=message,
    )


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("legacy import requires a session without pending changes")
