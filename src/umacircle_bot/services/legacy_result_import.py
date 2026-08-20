from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import Bet, Race, RaceCondition, RaceEntry, RaceResult, SheetImportRecord, SheetImportRun
from umacircle_bot.domain.errors import LegacyImportConflictError, LegacyImportError
from umacircle_bot.domain.imports import build_sheet_import_source_key, normalize_sha256_hex
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.sheets.legacy_result_plan import (
    LegacyEntryNumberSource,
    LegacyRaceConditionSnapshot,
    LegacyRoomResultImportPlan,
    LegacyRoomResultPlanRow,
)

ROOM_MATCH_ENTRY_KIND = "room_match"
LEGACY_RESULT_RECORD_TYPE = "legacy_room_result"
LEGACY_RESULT_TARGET_ENTITY_TYPE = "race_result"
LEGACY_RESULT_IMPORT_KIND = "legacy_room_results"
LEGACY_RESULT_TEST_IMPORT_KIND = "legacy_room_results_test"
DEFAULT_PREVIEW_CONFLICT_LIMIT = 50
MAX_PREVIEW_CONFLICT_LIMIT = 100

LEGACY_RESULT_PREVIOUS_ROW_COUNT = 486
LEGACY_RESULT_PREVIOUS_RACE_COUNT = 51
LEGACY_RESULT_PREVIOUS_AUTHORITATIVE_COUNT = 75
LEGACY_RESULT_PREVIOUS_SYNTHETIC_COUNT = 411
LEGACY_RESULT_PREVIOUS_RATING_EXCLUDED_COUNT = 48
LEGACY_RESULT_PREVIOUS_SOURCE_CHECKSUM = "26e911e7331c951a540a0b5bdb61f5517d2d8acd307e58f8ad01bf98056363ef"
LEGACY_RESULT_EXPECTED_ROW_COUNT = 540
LEGACY_RESULT_EXPECTED_RACE_COUNT = 56
LEGACY_RESULT_EXPECTED_AUTHORITATIVE_COUNT = 75
LEGACY_RESULT_EXPECTED_SYNTHETIC_COUNT = 465
LEGACY_RESULT_EXPECTED_RATING_EXCLUDED_COUNT = 48


@dataclass(frozen=True)
class LegacyRoomResultImportManifest:
    row_count: int
    race_count: int
    authoritative_entry_number_count: int
    synthetic_entry_number_count: int
    rating_excluded_count: int


LEGACY_ROOM_RESULT_PREVIOUS_IMPORT_MANIFEST = LegacyRoomResultImportManifest(
    row_count=LEGACY_RESULT_PREVIOUS_ROW_COUNT,
    race_count=LEGACY_RESULT_PREVIOUS_RACE_COUNT,
    authoritative_entry_number_count=LEGACY_RESULT_PREVIOUS_AUTHORITATIVE_COUNT,
    synthetic_entry_number_count=LEGACY_RESULT_PREVIOUS_SYNTHETIC_COUNT,
    rating_excluded_count=LEGACY_RESULT_PREVIOUS_RATING_EXCLUDED_COUNT,
)

LEGACY_ROOM_RESULT_IMPORT_MANIFEST = LegacyRoomResultImportManifest(
    row_count=LEGACY_RESULT_EXPECTED_ROW_COUNT,
    race_count=LEGACY_RESULT_EXPECTED_RACE_COUNT,
    authoritative_entry_number_count=LEGACY_RESULT_EXPECTED_AUTHORITATIVE_COUNT,
    synthetic_entry_number_count=LEGACY_RESULT_EXPECTED_SYNTHETIC_COUNT,
    rating_excluded_count=LEGACY_RESULT_EXPECTED_RATING_EXCLUDED_COUNT,
)
LEGACY_ROOM_RESULT_SUPPORTED_MANIFESTS = (
    LEGACY_ROOM_RESULT_PREVIOUS_IMPORT_MANIFEST,
    LEGACY_ROOM_RESULT_IMPORT_MANIFEST,
)


@dataclass(frozen=True)
class LegacyImportedRaceReference:
    race_id: int
    external_source: str
    external_race_id: str
    race_kind: str


@dataclass(frozen=True)
class LegacyResultSourceIdentity:
    source_sheet_name: str
    source_row_number: int
    source_key: str
    row_fingerprint: str


@dataclass(frozen=True)
class LegacyRaceEntryPayload:
    race_id: int
    entry_number: int
    entry_kind: str
    entry_number_source: str
    player_name: str
    horse_name_or_label: str
    running_style: str | None


@dataclass(frozen=True)
class LegacyRaceResultPayload:
    race_id: int
    entry_number: int
    game_account_id: int | None
    character_name: str
    rank: int
    converted_rank: int | None
    is_betting_excluded: bool
    is_rating_excluded: bool
    is_result_void: bool
    raced_at: datetime
    rating_before: Decimal | None
    peer_rating_difference: Decimal | None
    base_delta: Decimal | None
    adjustment_delta: Decimal | None
    rating_after: Decimal | None
    rank_ratio: Decimal | None

    def build_raw_result_json(self) -> dict[str, object]:
        return {
            "source_raced_at": self.raced_at.isoformat(),
            "legacy_rating": {
                "rating_before": _decimal_text(self.rating_before),
                "peer_rating_difference": _decimal_text(self.peer_rating_difference),
                "base_delta": _decimal_text(self.base_delta),
                "adjustment_delta": _decimal_text(self.adjustment_delta),
                "rating_after": _decimal_text(self.rating_after),
                "rank_ratio": _decimal_text(self.rank_ratio),
            },
        }


@dataclass(frozen=True)
class LegacyRoomResultPersistenceRow:
    source: LegacyResultSourceIdentity
    race_external_id: str
    race_condition: LegacyRaceConditionSnapshot
    entry: LegacyRaceEntryPayload
    result: LegacyRaceResultPayload


@dataclass(frozen=True)
class LegacyRoomResultPersistencePlan:
    source_identifier: str
    source_sheet_name: str
    rows: tuple[LegacyRoomResultPersistenceRow, ...]

    @property
    def race_count(self) -> int:
        return len({row.entry.race_id for row in self.rows})

    @property
    def authoritative_entry_number_count(self) -> int:
        return sum(row.entry.entry_number_source == LegacyEntryNumberSource.PAYOUT_RESULT.value for row in self.rows)

    @property
    def synthetic_entry_number_count(self) -> int:
        return sum(row.entry.entry_number_source == LegacyEntryNumberSource.SYNTHETIC.value for row in self.rows)

    @property
    def rating_excluded_count(self) -> int:
        return sum(row.result.is_rating_excluded for row in self.rows)


@dataclass(frozen=True)
class LegacyRoomResultImportConflict:
    source_row_number: int
    race_external_id: str
    entry_number: int
    code: str
    message: str


@dataclass(frozen=True)
class LegacyRoomResultRacePreview:
    race_id: int
    race_external_id: str
    row_count: int
    new_count: int
    skipped_count: int
    conflict_count: int
    authoritative_entry_number_count: int
    synthetic_entry_number_count: int
    rating_excluded_count: int


@dataclass(frozen=True)
class LegacyRoomResultImportPreview:
    row_count: int
    race_count: int
    new_count: int
    skipped_count: int
    conflict_count: int
    authoritative_entry_number_count: int
    synthetic_entry_number_count: int
    rating_excluded_count: int
    conflicts: tuple[LegacyRoomResultImportConflict, ...]
    race_summaries: tuple[LegacyRoomResultRacePreview, ...]

    @property
    def can_apply(self) -> bool:
        return self.conflict_count == 0

    @property
    def omitted_conflict_count(self) -> int:
        return self.conflict_count - len(self.conflicts)


@dataclass(frozen=True)
class LegacyRoomResultImportResult:
    import_run_id: int
    row_count: int
    race_count: int
    created_count: int
    skipped_count: int
    authoritative_entry_number_count: int
    synthetic_entry_number_count: int
    rating_excluded_count: int


@dataclass
class _MutableRacePreview:
    race_id: int
    race_external_id: str
    row_count: int = 0
    new_count: int = 0
    skipped_count: int = 0
    conflict_count: int = 0
    authoritative_entry_number_count: int = 0
    synthetic_entry_number_count: int = 0
    rating_excluded_count: int = 0


def preview_legacy_room_result_import(
    session: Session,
    *,
    plan: LegacyRoomResultPersistencePlan,
    source_checksum: str,
    conflict_limit: int = DEFAULT_PREVIEW_CONFLICT_LIMIT,
) -> LegacyRoomResultImportPreview:
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    expected_manifest = _select_supported_manifest(plan)
    return _preview_legacy_room_result_import_with_manifest(
        session,
        plan=plan,
        conflict_limit=conflict_limit,
        expected_manifest=expected_manifest,
        expected_source_checksum=normalized_checksum,
        allow_append_extension=expected_manifest == LEGACY_ROOM_RESULT_IMPORT_MANIFEST,
    )


def _preview_legacy_room_result_import_with_manifest(
    session: Session,
    *,
    plan: LegacyRoomResultPersistencePlan,
    conflict_limit: int = DEFAULT_PREVIEW_CONFLICT_LIMIT,
    expected_manifest: LegacyRoomResultImportManifest,
    expected_source_checksum: str | None = None,
    allow_append_extension: bool = False,
) -> LegacyRoomResultImportPreview:
    _require_clean_session(session)
    _validate_preview_conflict_limit(conflict_limit)
    _validate_persistence_plan(plan, expected_manifest=expected_manifest)
    return _inspect_legacy_room_result_import(
        session,
        plan=plan,
        conflict_limit=conflict_limit,
        lock_rows=False,
        expected_source_checksum=expected_source_checksum,
        allow_append_extension=allow_append_extension,
    )


def apply_legacy_room_result_import(
    session: Session,
    *,
    plan: LegacyRoomResultPersistencePlan,
    source_checksum: str,
) -> LegacyRoomResultImportResult:
    expected_manifest = _select_supported_manifest(plan)
    return _apply_legacy_room_result_import_with_manifest(
        session,
        plan=plan,
        source_checksum=source_checksum,
        expected_manifest=expected_manifest,
        import_kind=LEGACY_RESULT_IMPORT_KIND,
        allow_append_extension=expected_manifest == LEGACY_ROOM_RESULT_IMPORT_MANIFEST,
    )


def _apply_legacy_room_result_import_with_manifest(
    session: Session,
    *,
    plan: LegacyRoomResultPersistencePlan,
    source_checksum: str,
    expected_manifest: LegacyRoomResultImportManifest,
    import_kind: str,
    allow_append_extension: bool = False,
) -> LegacyRoomResultImportResult:
    _require_clean_session(session)
    if import_kind == LEGACY_RESULT_IMPORT_KIND and expected_manifest not in LEGACY_ROOM_RESULT_SUPPORTED_MANIFESTS:
        raise LegacyImportError("production legacy result import requires canonical manifest")
    _validate_persistence_plan(plan, expected_manifest=expected_manifest)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")

    _ensure_outer_database_transaction(session)
    with session.begin_nested():
        preview = _inspect_legacy_room_result_import(
            session,
            plan=plan,
            conflict_limit=1,
            lock_rows=True,
            expected_import_kind=import_kind,
            expected_source_checksum=normalized_checksum,
            allow_append_extension=allow_append_extension,
        )
        if preview.conflict_count:
            first = preview.conflicts[0]
            raise LegacyImportConflictError(
                f"legacy result import conflict at row {first.source_row_number}: {first.code}"
            )

        existing_records = _load_result_import_records(
            session,
            plan=plan,
            lock_rows=True,
        )
        if preview.new_count == 0:
            import_run = _load_exact_retry_result_import_run(
                session,
                import_kind=import_kind,
                source_identifier=plan.source_identifier,
                source_checksum=normalized_checksum,
            )
            created_count = 0
            skipped_count = len(plan.rows)
        else:
            import_run = SheetImportRun(
                import_kind=import_kind,
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
                if row.source.source_key in existing_records:
                    skipped_count += 1
                    continue
                _create_result_import_row(
                    session,
                    import_run=import_run,
                    row=row,
                )
                created_count += 1

            verification = _inspect_legacy_room_result_import(
                session,
                plan=plan,
                conflict_limit=1,
                lock_rows=True,
                active_import_run_id=import_run.id,
                expected_import_kind=import_kind,
                expected_source_checksum=normalized_checksum,
                allow_append_extension=allow_append_extension,
            )
            if verification.conflict_count or verification.new_count:
                raise LegacyImportError("legacy result import verification failed")
            if verification.skipped_count != len(plan.rows):
                raise LegacyImportError("legacy result import verification count changed")

            import_run.status = "completed"
            import_run.finished_at = datetime.now(UTC)
            import_run.summary_json = {
                "row_count": len(plan.rows),
                "race_count": plan.race_count,
                "created_count": created_count,
                "skipped_count": skipped_count,
                "authoritative_entry_number_count": plan.authoritative_entry_number_count,
                "synthetic_entry_number_count": plan.synthetic_entry_number_count,
                "rating_excluded_count": plan.rating_excluded_count,
            }
            session.flush()

    return LegacyRoomResultImportResult(
        import_run_id=import_run.id,
        row_count=len(plan.rows),
        race_count=plan.race_count,
        created_count=created_count,
        skipped_count=skipped_count,
        authoritative_entry_number_count=plan.authoritative_entry_number_count,
        synthetic_entry_number_count=plan.synthetic_entry_number_count,
        rating_excluded_count=plan.rating_excluded_count,
    )


def _load_exact_retry_result_import_run(
    session: Session,
    *,
    import_kind: str,
    source_identifier: str,
    source_checksum: str,
) -> SheetImportRun:
    runs = tuple(
        session.scalars(
            select(SheetImportRun)
            .where(
                SheetImportRun.import_kind == import_kind,
                SheetImportRun.source_type == "xlsx",
                SheetImportRun.source_identifier == source_identifier,
                SheetImportRun.source_checksum == source_checksum,
            )
            .order_by(SheetImportRun.id)
            .with_for_update()
        )
    )
    if len(runs) != 1 or runs[0].status != "completed" or runs[0].finished_at is None:
        raise LegacyImportConflictError("legacy result exact retry import run is missing or ambiguous")
    return runs[0]


def _inspect_legacy_room_result_import(
    session: Session,
    *,
    plan: LegacyRoomResultPersistencePlan,
    conflict_limit: int,
    lock_rows: bool,
    active_import_run_id: int | None = None,
    expected_import_kind: str = LEGACY_RESULT_IMPORT_KIND,
    expected_source_checksum: str | None = None,
    allow_append_extension: bool = False,
) -> LegacyRoomResultImportPreview:
    races = _load_result_races(session, plan=plan, lock_rows=lock_rows)
    conditions = _load_result_conditions(session, plan=plan, lock_rows=lock_rows)
    records = _load_result_import_records(session, plan=plan, lock_rows=lock_rows)
    import_runs = _load_result_import_runs(session, records=records, lock_rows=lock_rows)
    entries = _load_result_entries(session, plan=plan, lock_rows=lock_rows)
    results = _load_race_results(session, plan=plan, lock_rows=lock_rows)
    append_extension = _is_supported_append_extension(
        plan,
        records=records,
        import_runs=import_runs,
        active_import_run_id=active_import_run_id,
        expected_source_checksum=expected_source_checksum,
        allow_append_extension=allow_append_extension,
    )
    appended_bet_race_ids = _load_append_race_ids_with_bets(
        session,
        plan=plan,
        records=records,
        append_extension=append_extension,
        lock_rows=lock_rows,
    )

    conflicts: list[LegacyRoomResultImportConflict] = []
    conflict_count = 0
    new_count = 0
    skipped_count = 0
    race_order: list[tuple[int, str]] = []
    race_states: dict[tuple[int, str], _MutableRacePreview] = {}

    for row_index, row in enumerate(plan.rows):
        race_key = (row.entry.race_id, row.race_external_id)
        race_state = race_states.get(race_key)
        if race_state is None:
            race_state = _MutableRacePreview(
                race_id=row.entry.race_id,
                race_external_id=row.race_external_id,
            )
            race_states[race_key] = race_state
            race_order.append(race_key)
        _update_race_source_counts(race_state, row=row)

        target_key = (row.entry.race_id, row.entry.entry_number)
        if row.entry.race_id in appended_bet_race_ids:
            conflict = _result_conflict(
                row,
                "room_bets_exist",
                "an appended legacy result race already has room-match bets",
            )
        else:
            conflict = _classify_result_import_row(
                row=row,
                source_identifier=plan.source_identifier,
                race=races.get(row.entry.race_id),
                condition=conditions.get(row.entry.race_id),
                record=records.get(row.source.source_key),
                import_run=import_runs.get(records[row.source.source_key].import_run_id)
                if row.source.source_key in records
                else None,
                active_import_run_id=active_import_run_id,
                expected_import_kind=expected_import_kind,
                expected_source_checksum=expected_source_checksum,
                allow_prior_source_checksum=(
                    append_extension and row_index < LEGACY_ROOM_RESULT_PREVIOUS_IMPORT_MANIFEST.row_count
                ),
                entry=entries.get(target_key),
                result=results.get(target_key),
            )
        if conflict is not None:
            conflict_count += 1
            race_state.conflict_count += 1
            if len(conflicts) < conflict_limit:
                conflicts.append(conflict)
        elif records.get(row.source.source_key) is None:
            new_count += 1
            race_state.new_count += 1
        else:
            skipped_count += 1
            race_state.skipped_count += 1

    planned_target_keys = {(row.entry.race_id, row.entry.entry_number) for row in plan.rows}
    unexpected_target_keys = sorted((set(entries) | set(results)) - planned_target_keys)
    first_row_by_race = {row.entry.race_id: row for row in plan.rows}
    for race_id, entry_number in unexpected_target_keys:
        representative = first_row_by_race[race_id]
        race_key = (race_id, representative.race_external_id)
        race_states[race_key].conflict_count += 1
        conflict_count += 1
        if len(conflicts) < conflict_limit:
            entry_exists = (race_id, entry_number) in entries
            result_exists = (race_id, entry_number) in results
            code = "unexpected_target" if entry_exists and result_exists else "partial_import_state"
            conflicts.append(
                LegacyRoomResultImportConflict(
                    source_row_number=representative.source.source_row_number,
                    race_external_id=representative.race_external_id,
                    entry_number=entry_number,
                    code=code,
                    message="race contains a result target that is not present in the legacy result plan",
                )
            )

    if conflict_count == 0 and new_count and skipped_count and not append_extension:
        first_row = plan.rows[0]
        conflict_count = 1
        first_race_key = (first_row.entry.race_id, first_row.race_external_id)
        race_states[first_race_key].conflict_count += 1
        if conflict_limit:
            conflicts.append(
                _result_conflict(
                    first_row,
                    "partial_import_state",
                    "legacy result batch mixes new rows with exact retries",
                )
            )

    race_summaries = tuple(
        LegacyRoomResultRacePreview(
            race_id=race_states[key].race_id,
            race_external_id=race_states[key].race_external_id,
            row_count=race_states[key].row_count,
            new_count=race_states[key].new_count,
            skipped_count=race_states[key].skipped_count,
            conflict_count=race_states[key].conflict_count,
            authoritative_entry_number_count=race_states[key].authoritative_entry_number_count,
            synthetic_entry_number_count=race_states[key].synthetic_entry_number_count,
            rating_excluded_count=race_states[key].rating_excluded_count,
        )
        for key in race_order
    )
    return LegacyRoomResultImportPreview(
        row_count=len(plan.rows),
        race_count=plan.race_count,
        new_count=new_count,
        skipped_count=skipped_count,
        conflict_count=conflict_count,
        authoritative_entry_number_count=plan.authoritative_entry_number_count,
        synthetic_entry_number_count=plan.synthetic_entry_number_count,
        rating_excluded_count=plan.rating_excluded_count,
        conflicts=tuple(conflicts),
        race_summaries=race_summaries,
    )


def map_legacy_room_result_plan(
    plan: LegacyRoomResultImportPlan,
    imported_races: tuple[LegacyImportedRaceReference, ...],
) -> LegacyRoomResultPersistencePlan:
    races_by_external_id = _index_imported_races(plan, imported_races)
    required_external_ids = {row.race_external_id for row in plan.rows}
    missing_external_ids = required_external_ids - races_by_external_id.keys()
    if missing_external_ids:
        raise LegacyImportError(f"legacy result race is not imported: {min(missing_external_ids)}")

    mapped: list[LegacyRoomResultPersistenceRow] = []
    seen_sources: set[str] = set()
    seen_source_rows: set[int] = set()
    seen_targets: set[tuple[int, int]] = set()
    for row in plan.rows:
        race = races_by_external_id[row.race_external_id]
        if row.source_key in seen_sources or row.source_row_number in seen_source_rows:
            raise LegacyImportError(f"duplicate legacy result source identity at row {row.source_row_number}")
        target_key = (race.race_id, row.entry_number)
        if target_key in seen_targets:
            raise LegacyImportError(f"duplicate legacy result target for race {row.race_external_id}")
        seen_sources.add(row.source_key)
        seen_source_rows.add(row.source_row_number)
        seen_targets.add(target_key)
        mapped.append(_map_row(plan, row, race))

    return LegacyRoomResultPersistencePlan(
        source_identifier=plan.source_identifier,
        source_sheet_name=plan.source_sheet_name,
        rows=tuple(mapped),
    )


def _index_imported_races(
    plan: LegacyRoomResultImportPlan,
    imported_races: tuple[LegacyImportedRaceReference, ...],
) -> dict[str, LegacyImportedRaceReference]:
    indexed: dict[str, LegacyImportedRaceReference] = {}
    seen_ids: set[int] = set()
    for race in imported_races:
        if isinstance(race.race_id, bool) or not isinstance(race.race_id, int) or race.race_id < 1:
            raise LegacyImportError("imported race ID must be a positive integer")
        if race.external_source != plan.source_identifier:
            raise LegacyImportError("imported race source does not match result plan")
        if race.race_kind != ROOM_MATCH_ENTRY_KIND:
            raise LegacyImportError(f"imported race is not a room match: {race.external_race_id}")
        if not race.external_race_id:
            raise LegacyImportError("imported race external ID is required")
        if race.external_race_id in indexed or race.race_id in seen_ids:
            raise LegacyImportError(f"duplicate imported race reference: {race.external_race_id}")
        indexed[race.external_race_id] = race
        seen_ids.add(race.race_id)
    return indexed


def _map_row(
    plan: LegacyRoomResultImportPlan,
    row: LegacyRoomResultPlanRow,
    race: LegacyImportedRaceReference,
) -> LegacyRoomResultPersistenceRow:
    source = LegacyResultSourceIdentity(
        source_sheet_name=plan.source_sheet_name,
        source_row_number=row.source_row_number,
        source_key=row.source_key,
        row_fingerprint=row.row_fingerprint,
    )
    entry = LegacyRaceEntryPayload(
        race_id=race.race_id,
        entry_number=row.entry_number,
        entry_kind=ROOM_MATCH_ENTRY_KIND,
        entry_number_source=row.entry_number_source.value,
        player_name=row.player_name,
        horse_name_or_label=row.uma_name,
        running_style=row.running_style,
    )
    result = LegacyRaceResultPayload(
        race_id=race.race_id,
        entry_number=row.entry_number,
        game_account_id=None,
        character_name=row.uma_name,
        rank=row.rank,
        converted_rank=row.converted_rank,
        is_betting_excluded=False,
        is_rating_excluded=row.is_rating_excluded,
        is_result_void=False,
        raced_at=row.raced_at,
        rating_before=row.rating_before,
        peer_rating_difference=row.peer_rating_difference,
        base_delta=row.base_delta,
        adjustment_delta=row.adjustment_delta,
        rating_after=row.rating_after,
        rank_ratio=row.rank_ratio,
    )
    return LegacyRoomResultPersistenceRow(
        source=source,
        race_external_id=row.race_external_id,
        race_condition=row.race_condition,
        entry=entry,
        result=result,
    )


def _validate_preview_conflict_limit(conflict_limit: int) -> None:
    if (
        isinstance(conflict_limit, bool)
        or not isinstance(conflict_limit, int)
        or not 1 <= conflict_limit <= MAX_PREVIEW_CONFLICT_LIMIT
    ):
        raise LegacyImportError(f"preview conflict limit must be between 1 and {MAX_PREVIEW_CONFLICT_LIMIT}")


def _validate_persistence_plan(
    plan: LegacyRoomResultPersistencePlan,
    *,
    expected_manifest: LegacyRoomResultImportManifest,
) -> None:
    if not isinstance(plan.source_identifier, str) or not plan.source_identifier.strip():
        raise LegacyImportError("legacy result source identifier is required")
    if len(plan.source_identifier.strip()) > 200:
        raise LegacyImportError("legacy result source identifier is too long")
    if not isinstance(plan.source_sheet_name, str) or not plan.source_sheet_name.strip():
        raise LegacyImportError("legacy result source sheet is required")
    if len(plan.source_sheet_name.strip()) > 100:
        raise LegacyImportError("legacy result source sheet is too long")
    if not plan.rows:
        raise LegacyImportError("legacy result persistence plan is empty")
    _validate_expected_manifest(plan, expected_manifest=expected_manifest)

    seen_sources: set[str] = set()
    seen_source_rows: set[int] = set()
    seen_targets: set[tuple[int, int]] = set()
    for row in plan.rows:
        if row.source.source_sheet_name != plan.source_sheet_name:
            raise LegacyImportError("legacy result row source sheet does not match plan")
        if row.source.source_row_number < 1:
            raise LegacyImportError("legacy result source row must be positive")
        normalize_sha256_hex(row.source.source_key, field_name="source key")
        normalize_sha256_hex(row.source.row_fingerprint, field_name="row fingerprint")
        expected_source_key = build_sheet_import_source_key(
            source_identifier=plan.source_identifier,
            sheet_name=row.source.source_sheet_name,
            row_number=row.source.source_row_number,
        )
        if row.source.source_key != expected_source_key:
            raise LegacyImportError(f"legacy result source key does not match row {row.source.source_row_number}")
        if row.source.source_key in seen_sources or row.source.source_row_number in seen_source_rows:
            raise LegacyImportError(f"duplicate legacy result source identity at row {row.source.source_row_number}")
        target_key = (row.entry.race_id, row.entry.entry_number)
        if target_key in seen_targets:
            raise LegacyImportError(f"duplicate legacy result target at row {row.source.source_row_number}")
        if row.entry.race_id < 1 or row.entry.entry_number < 1:
            raise LegacyImportError("legacy result target IDs must be positive")
        if row.result.race_id != row.entry.race_id or row.result.entry_number != row.entry.entry_number:
            raise LegacyImportError("legacy result entry and result target do not match")
        if row.entry.entry_kind != ROOM_MATCH_ENTRY_KIND:
            raise LegacyImportError("legacy result entry is not a room match")
        if row.entry.entry_number_source not in {
            LegacyEntryNumberSource.PAYOUT_RESULT.value,
            LegacyEntryNumberSource.SYNTHETIC.value,
        }:
            raise LegacyImportError("legacy result entry number source is invalid")
        _validate_legacy_result_payload(row)
        seen_sources.add(row.source.source_key)
        seen_source_rows.add(row.source.source_row_number)
        seen_targets.add(target_key)


def _validate_legacy_result_payload(row: LegacyRoomResultPersistenceRow) -> None:
    result = row.result
    if result.game_account_id is not None:
        raise LegacyImportError("legacy result game account must remain unresolved")
    if result.is_betting_excluded:
        raise LegacyImportError("legacy result cannot be betting-excluded during initial import")
    if result.is_result_void:
        raise LegacyImportError("legacy result cannot be void during initial import")
    if result.character_name != row.entry.horse_name_or_label:
        raise LegacyImportError("legacy result character does not match the race entry")
    if result.rank < 1:
        raise LegacyImportError("legacy result rank must be positive")
    if result.raced_at.tzinfo is None:
        raise LegacyImportError("legacy result raced_at must be timezone-aware")

    rating_values = (
        result.converted_rank,
        result.rating_before,
        result.peer_rating_difference,
        result.base_delta,
        result.adjustment_delta,
        result.rating_after,
        result.rank_ratio,
    )
    if result.is_rating_excluded:
        if any(value is not None for value in rating_values):
            raise LegacyImportError("Rating-excluded legacy result must not contain Rating values")
    elif any(value is None for value in rating_values):
        raise LegacyImportError("Rating-included legacy result requires complete Rating values")


def _validate_expected_manifest(
    plan: LegacyRoomResultPersistencePlan,
    *,
    expected_manifest: LegacyRoomResultImportManifest,
) -> None:
    actual = _manifest_for_plan(plan)
    if actual != expected_manifest:
        _raise_manifest_mismatch(actual=actual, expected=expected_manifest)


def _manifest_for_plan(plan: LegacyRoomResultPersistencePlan) -> LegacyRoomResultImportManifest:
    return LegacyRoomResultImportManifest(
        row_count=len(plan.rows),
        race_count=plan.race_count,
        authoritative_entry_number_count=plan.authoritative_entry_number_count,
        synthetic_entry_number_count=plan.synthetic_entry_number_count,
        rating_excluded_count=plan.rating_excluded_count,
    )


def _select_supported_manifest(
    plan: LegacyRoomResultPersistencePlan,
) -> LegacyRoomResultImportManifest:
    actual = _manifest_for_plan(plan)
    for manifest in LEGACY_ROOM_RESULT_SUPPORTED_MANIFESTS:
        if actual == manifest:
            return manifest
    _raise_manifest_mismatch(actual=actual, expected=LEGACY_ROOM_RESULT_IMPORT_MANIFEST)


def _raise_manifest_mismatch(
    *,
    actual: LegacyRoomResultImportManifest,
    expected: LegacyRoomResultImportManifest,
) -> None:
    raise LegacyImportError(
        "legacy result manifest mismatch: "
        f"expected {expected.row_count}/{expected.race_count}/"
        f"{expected.authoritative_entry_number_count}/"
        f"{expected.synthetic_entry_number_count}/"
        f"{expected.rating_excluded_count}, got "
        f"{actual.row_count}/{actual.race_count}/"
        f"{actual.authoritative_entry_number_count}/"
        f"{actual.synthetic_entry_number_count}/"
        f"{actual.rating_excluded_count}"
    )


def _create_result_import_row(
    session: Session,
    *,
    import_run: SheetImportRun,
    row: LegacyRoomResultPersistenceRow,
) -> None:
    record = SheetImportRecord(
        import_run_id=import_run.id,
        source_key=row.source.source_key,
        row_fingerprint=row.source.row_fingerprint,
        source_sheet_name=row.source.source_sheet_name,
        source_row_number=row.source.source_row_number,
        record_type=LEGACY_RESULT_RECORD_TYPE,
        status="pending",
    )
    session.add(record)
    session.flush()

    entry = RaceEntry(
        race_id=row.entry.race_id,
        entry_number=row.entry.entry_number,
        entry_kind=row.entry.entry_kind,
        entry_number_source=row.entry.entry_number_source,
        source_import_record_id=record.id,
        player_name=row.entry.player_name,
        horse_name_or_label=row.entry.horse_name_or_label,
        running_style=row.entry.running_style,
    )
    result = RaceResult(
        race_id=row.result.race_id,
        entry_number=row.result.entry_number,
        game_account_id=row.result.game_account_id,
        character_name=row.result.character_name,
        rank=row.result.rank,
        converted_rank=row.result.converted_rank,
        is_betting_excluded=row.result.is_betting_excluded,
        is_rating_excluded=row.result.is_rating_excluded,
        is_result_void=row.result.is_result_void,
        source_import_record_id=record.id,
        raw_result_json=row.result.build_raw_result_json(),
    )
    session.add_all([entry, result])
    session.flush()

    record.status = "applied"
    record.target_entity_type = LEGACY_RESULT_TARGET_ENTITY_TYPE
    record.target_entity_id = result.id
    record.detail_json = {
        "race_entry_id": entry.id,
        "race_result_id": result.id,
        "race_id": row.entry.race_id,
        "entry_number": row.entry.entry_number,
    }
    session.flush()


def _load_result_import_records(
    session: Session,
    *,
    plan: LegacyRoomResultPersistencePlan,
    lock_rows: bool,
) -> dict[str, SheetImportRecord]:
    source_keys = [row.source.source_key for row in plan.rows]
    statement = (
        select(SheetImportRecord)
        .where(SheetImportRecord.source_key.in_(source_keys))
        .order_by(SheetImportRecord.source_key)
    )
    if lock_rows:
        statement = statement.with_for_update()
    return {record.source_key: record for record in session.scalars(statement)}


def _load_result_import_runs(
    session: Session,
    *,
    records: dict[str, SheetImportRecord],
    lock_rows: bool,
) -> dict[int, SheetImportRun]:
    import_run_ids = sorted({record.import_run_id for record in records.values()})
    if not import_run_ids:
        return {}
    statement = select(SheetImportRun).where(SheetImportRun.id.in_(import_run_ids)).order_by(SheetImportRun.id)
    if lock_rows:
        statement = statement.with_for_update()
    return {import_run.id: import_run for import_run in session.scalars(statement)}


def _load_result_races(
    session: Session,
    *,
    plan: LegacyRoomResultPersistencePlan,
    lock_rows: bool,
) -> dict[int, Race]:
    race_ids = sorted({row.entry.race_id for row in plan.rows})
    statement = select(Race).where(Race.id.in_(race_ids)).order_by(Race.id)
    if lock_rows:
        statement = statement.with_for_update()
    return {race.id: race for race in session.scalars(statement)}


def _load_result_conditions(
    session: Session,
    *,
    plan: LegacyRoomResultPersistencePlan,
    lock_rows: bool,
) -> dict[int, RaceCondition]:
    race_ids = sorted({row.entry.race_id for row in plan.rows})
    statement = select(RaceCondition).where(RaceCondition.race_id.in_(race_ids)).order_by(RaceCondition.race_id)
    if lock_rows:
        statement = statement.with_for_update()
    return {condition.race_id: condition for condition in session.scalars(statement)}


def _load_result_entries(
    session: Session,
    *,
    plan: LegacyRoomResultPersistencePlan,
    lock_rows: bool,
) -> dict[tuple[int, int], RaceEntry]:
    race_ids = sorted({row.entry.race_id for row in plan.rows})
    statement = (
        select(RaceEntry).where(RaceEntry.race_id.in_(race_ids)).order_by(RaceEntry.race_id, RaceEntry.entry_number)
    )
    if lock_rows:
        statement = statement.with_for_update()
    return {(entry.race_id, entry.entry_number): entry for entry in session.scalars(statement)}


def _load_race_results(
    session: Session,
    *,
    plan: LegacyRoomResultPersistencePlan,
    lock_rows: bool,
) -> dict[tuple[int, int], RaceResult]:
    race_ids = sorted({row.result.race_id for row in plan.rows})
    statement = (
        select(RaceResult).where(RaceResult.race_id.in_(race_ids)).order_by(RaceResult.race_id, RaceResult.entry_number)
    )
    if lock_rows:
        statement = statement.with_for_update()
    return {(result.race_id, result.entry_number): result for result in session.scalars(statement)}


def _is_supported_append_extension(
    plan: LegacyRoomResultPersistencePlan,
    *,
    records: dict[str, SheetImportRecord],
    import_runs: dict[int, SheetImportRun],
    active_import_run_id: int | None,
    expected_source_checksum: str | None,
    allow_append_extension: bool,
) -> bool:
    if (
        not allow_append_extension
        or expected_source_checksum is None
        or _manifest_for_plan(plan) != LEGACY_ROOM_RESULT_IMPORT_MANIFEST
    ):
        return False

    prefix_size = LEGACY_ROOM_RESULT_PREVIOUS_IMPORT_MANIFEST.row_count
    prefix = plan.rows[:prefix_size]
    suffix = plan.rows[prefix_size:]
    if not suffix or any(row.source.source_key not in records for row in prefix):
        return False

    suffix_records = [records.get(row.source.source_key) for row in suffix]
    if any(record is None for record in suffix_records):
        return all(record is None for record in suffix_records)

    for record in suffix_records:
        assert record is not None
        import_run = import_runs.get(record.import_run_id)
        if import_run is None:
            return False
        if import_run.source_checksum != expected_source_checksum and import_run.id != active_import_run_id:
            return False
    return True


def _load_append_race_ids_with_bets(
    session: Session,
    *,
    plan: LegacyRoomResultPersistencePlan,
    records: dict[str, SheetImportRecord],
    append_extension: bool,
    lock_rows: bool,
) -> set[int]:
    if not append_extension:
        return set()
    prefix_size = LEGACY_ROOM_RESULT_PREVIOUS_IMPORT_MANIFEST.row_count
    new_race_ids = {row.entry.race_id for row in plan.rows[prefix_size:] if row.source.source_key not in records}
    if not new_race_ids:
        return set()
    statement = (
        select(Bet.race_id)
        .where(
            Bet.race_id.in_(new_race_ids),
            Bet.betting_mode == ROOM_MATCH_ENTRY_KIND,
        )
        .order_by(Bet.id)
    )
    if lock_rows:
        statement = statement.with_for_update()
    return set(session.scalars(statement))


def _classify_result_import_row(
    *,
    row: LegacyRoomResultPersistenceRow,
    source_identifier: str,
    race: Race | None,
    condition: RaceCondition | None,
    record: SheetImportRecord | None,
    import_run: SheetImportRun | None,
    active_import_run_id: int | None,
    expected_import_kind: str,
    expected_source_checksum: str | None,
    allow_prior_source_checksum: bool,
    entry: RaceEntry | None,
    result: RaceResult | None,
) -> LegacyRoomResultImportConflict | None:
    if race is None:
        return _result_conflict(
            row,
            "race_missing",
            "mapped race no longer exists",
        )
    if (
        race.external_source != source_identifier
        or race.external_race_id != row.race_external_id
        or race.race_kind != ROOM_MATCH_ENTRY_KIND
        or race.status not in {"result_confirmed", "settled"}
        or race.starts_at is None
        or database_datetime_as_utc(race.starts_at) != row.result.raced_at
    ):
        return _result_conflict(
            row,
            "race_reference_changed",
            "mapped race no longer matches the legacy result source",
        )
    if condition is None:
        return _result_conflict(
            row,
            "race_condition_missing",
            "mapped race condition no longer exists",
        )
    expected_condition = row.race_condition
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
    if actual_condition != (
        expected_condition.grade,
        expected_condition.venue,
        expected_condition.track_surface,
        expected_condition.distance,
        expected_condition.direction,
        expected_condition.season,
        expected_condition.weather,
        expected_condition.track_condition,
        expected_condition.condition_label,
        expected_condition.participant_count,
    ):
        return _result_conflict(
            row,
            "race_condition_changed",
            "mapped race conditions no longer match the legacy result source",
        )
    if record is None:
        if entry is not None or result is not None:
            return _result_conflict(
                row,
                "partial_import_state",
                "result targets exist without the matching import record",
            )
        return None

    if import_run is None:
        return _result_conflict(
            row,
            "import_run_missing",
            "stored result import run no longer exists",
        )
    valid_status = (import_run.status == "completed" and import_run.finished_at is not None) or (
        active_import_run_id is not None
        and import_run.id == active_import_run_id
        and import_run.status == "running"
        and import_run.finished_at is None
    )
    if (
        import_run.import_kind != expected_import_kind
        or import_run.source_type != "xlsx"
        or import_run.source_identifier != source_identifier
        or (
            expected_source_checksum is not None
            and import_run.source_checksum != expected_source_checksum
            and not (
                allow_prior_source_checksum and import_run.source_checksum == LEGACY_RESULT_PREVIOUS_SOURCE_CHECKSUM
            )
        )
        or not valid_status
    ):
        return _result_conflict(
            row,
            "import_run_changed",
            "stored result import run provenance no longer matches",
        )

    if record.row_fingerprint != row.source.row_fingerprint:
        return _result_conflict(
            row,
            "row_fingerprint_changed",
            "source result content changed after import",
        )
    if (
        record.source_sheet_name != row.source.source_sheet_name
        or record.source_row_number != row.source.source_row_number
        or record.record_type != LEGACY_RESULT_RECORD_TYPE
        or record.status != "applied"
    ):
        return _result_conflict(
            row,
            "import_record_changed",
            "stored result import record no longer matches",
        )
    if (entry is None) != (result is None):
        return _result_conflict(
            row,
            "partial_import_state",
            "only one required result target exists",
        )
    if entry is None or result is None:
        return _result_conflict(
            row,
            "targets_missing",
            "stored result targets are missing",
        )
    if not _record_targets_match(record=record, entry=entry, result=result, row=row):
        return _result_conflict(
            row,
            "target_provenance_changed",
            "stored result target linkage no longer matches",
        )
    if not _entry_matches_payload(entry=entry, row=row):
        return _result_conflict(
            row,
            "entry_target_changed",
            "stored race entry no longer matches the source result",
        )
    if not _result_matches_payload(result=result, row=row):
        return _result_conflict(
            row,
            "result_target_changed",
            "stored race result no longer matches the source result",
        )
    return None


def _record_targets_match(
    *,
    record: SheetImportRecord,
    entry: RaceEntry,
    result: RaceResult,
    row: LegacyRoomResultPersistenceRow,
) -> bool:
    detail = record.detail_json if isinstance(record.detail_json, dict) else {}
    return (
        record.target_entity_type == LEGACY_RESULT_TARGET_ENTITY_TYPE
        and record.target_entity_id == result.id
        and detail.get("race_entry_id") == entry.id
        and detail.get("race_result_id") == result.id
        and detail.get("race_id") == row.entry.race_id
        and detail.get("entry_number") == row.entry.entry_number
        and entry.source_import_record_id == record.id
        and result.source_import_record_id == record.id
    )


def _entry_matches_payload(
    *,
    entry: RaceEntry,
    row: LegacyRoomResultPersistenceRow,
) -> bool:
    expected = row.entry
    return (
        entry.race_id == expected.race_id
        and entry.entry_number == expected.entry_number
        and entry.entry_kind == expected.entry_kind
        and entry.entry_number_source == expected.entry_number_source
        and entry.player_name == expected.player_name
        and entry.horse_name_or_label == expected.horse_name_or_label
        and entry.running_style == expected.running_style
    )


def _result_matches_payload(
    *,
    result: RaceResult,
    row: LegacyRoomResultPersistenceRow,
) -> bool:
    expected = row.result
    return (
        result.race_id == expected.race_id
        and result.entry_number == expected.entry_number
        and result.game_account_id == expected.game_account_id
        and result.character_name == expected.character_name
        and result.rank == expected.rank
        and result.converted_rank == expected.converted_rank
        and result.is_betting_excluded == expected.is_betting_excluded
        and result.is_rating_excluded == expected.is_rating_excluded
        and result.is_result_void == expected.is_result_void
        and result.raw_result_json == expected.build_raw_result_json()
    )


def _update_race_source_counts(
    state: _MutableRacePreview,
    *,
    row: LegacyRoomResultPersistenceRow,
) -> None:
    state.row_count += 1
    if row.entry.entry_number_source == LegacyEntryNumberSource.PAYOUT_RESULT.value:
        state.authoritative_entry_number_count += 1
    elif row.entry.entry_number_source == LegacyEntryNumberSource.SYNTHETIC.value:
        state.synthetic_entry_number_count += 1
    if row.result.is_rating_excluded:
        state.rating_excluded_count += 1


def _result_conflict(
    row: LegacyRoomResultPersistenceRow,
    code: str,
    message: str,
) -> LegacyRoomResultImportConflict:
    return LegacyRoomResultImportConflict(
        source_row_number=row.source.source_row_number,
        race_external_id=row.race_external_id,
        entry_number=row.entry.entry_number,
        code=code,
        message=message,
    )


def _ensure_outer_database_transaction(session: Session) -> None:
    connection = session.connection()
    if connection.dialect.name != "sqlite":
        return
    driver_connection = connection.connection.driver_connection
    if not driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("legacy import requires a session without pending changes")


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
