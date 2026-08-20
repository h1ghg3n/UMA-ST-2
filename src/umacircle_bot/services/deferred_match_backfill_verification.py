from __future__ import annotations

from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    GameAccount,
    Race,
    RaceCondition,
    RaceEntry,
    RaceRatingContext,
    RaceResult,
    RatingEvent,
    RatingRuleVersion,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.circle_match_seasons import (
    CIRCLE_MATCH_SEASON_TIMEZONE_NAME,
    circle_match_season_for,
)
from umacircle_bot.domain.errors import LegacyImportError, MatchRatingConflictError
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.services.legacy_identity_mapping import LEGACY_IDENTITY_MAPPING_IMPORT_KIND
from umacircle_bot.services.legacy_race_import import (
    LEGACY_RACE_IMPORT_KIND,
    LEGACY_RACE_RECORD_TYPE,
    LegacyRaceImportPlan,
)
from umacircle_bot.services.legacy_rating_backfill import verify_source_rating_snapshot
from umacircle_bot.services.legacy_rating_chain_disposition import (
    LEGACY_RATING_CHAIN_DISPOSITION_IMPORT_KIND,
    load_applied_legacy_rating_chain_dispositions,
)
from umacircle_bot.services.legacy_result_import import (
    LEGACY_RESULT_RECORD_TYPE,
    LegacyRoomResultPersistencePlan,
)
from umacircle_bot.services.legacy_source_account_seed import (
    LEGACY_SOURCE_ACCOUNT_SEED_IMPORT_KIND,
    load_applied_legacy_source_account_seed,
)


class _SeasonPopulationRow(TypedDict):
    season_name: str
    race_count: int
    entry_count: int
    result_count: int


def build_backfill_population_report(
    session: Session,
    *,
    source_identifier: str,
    result_import_kind: str,
) -> dict[str, object]:
    source_races = tuple(
        session.scalars(
            select(Race)
            .where(
                Race.external_source == source_identifier,
                Race.race_kind == "room_match",
            )
            .order_by(Race.starts_at, Race.id)
        )
    )
    source_race_ids = select(Race.id).where(
        Race.external_source == source_identifier,
        Race.race_kind == "room_match",
    )
    entries = tuple(session.scalars(select(RaceEntry).where(RaceEntry.race_id.in_(source_race_ids))))
    results = tuple(session.scalars(select(RaceResult).where(RaceResult.race_id.in_(source_race_ids))))
    season_population, unassigned_season_race_count = _season_population(
        source_races=source_races,
        source_entries=entries,
        source_results=results,
    )
    return {
        "race_count": len(source_races),
        "entry_count": len(entries),
        "result_count": len(results),
        "season_timezone": CIRCLE_MATCH_SEASON_TIMEZONE_NAME,
        "season_population": season_population,
        "unassigned_season_race_count": unassigned_season_race_count,
        "rating_event_count": int(
            session.scalar(
                select(func.count()).select_from(RatingEvent).where(RatingEvent.race_id.in_(source_race_ids))
            )
            or 0
        ),
        "rating_context_count": int(
            session.scalar(
                select(func.count())
                .select_from(RaceRatingContext)
                .where(RaceRatingContext.race_id.in_(source_race_ids))
            )
            or 0
        ),
        "race_import_run_count": _run_count(session, LEGACY_RACE_IMPORT_KIND, source_identifier),
        "result_import_run_count": _run_count(session, result_import_kind, source_identifier),
        "source_account_seed_import_run_count": _run_count(
            session,
            LEGACY_SOURCE_ACCOUNT_SEED_IMPORT_KIND,
            source_identifier,
        ),
        "mapping_import_run_count": _run_count(session, LEGACY_IDENTITY_MAPPING_IMPORT_KIND, source_identifier),
        "disposition_import_run_count": _run_count(
            session,
            LEGACY_RATING_CHAIN_DISPOSITION_IMPORT_KIND,
            source_identifier,
        ),
    }


def build_completed_backfill_report(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
    confirmed_mapping_decision_checksum: str,
    confirmed_disposition_decision_checksum: str,
    rating_rule_version_id: int,
    result_import_kind: str,
    expected_race_count: int,
    expected_entry_count: int,
    expected_result_count: int,
    source_race_plan: LegacyRaceImportPlan | None,
    source_result_plan: LegacyRoomResultPersistencePlan | None,
) -> tuple[dict[str, object], tuple[str, ...]]:
    errors: list[str] = []
    race_runs = _completed_runs(
        session,
        import_kind=LEGACY_RACE_IMPORT_KIND,
        source_identifier=source_identifier,
        source_checksum=source_checksum,
    )
    result_runs = _completed_runs(
        session,
        import_kind=result_import_kind,
        source_identifier=source_identifier,
        source_checksum=source_checksum,
    )
    mapping_runs = tuple(
        session.scalars(
            select(SheetImportRun).where(
                SheetImportRun.import_kind == LEGACY_IDENTITY_MAPPING_IMPORT_KIND,
                SheetImportRun.source_identifier == source_identifier,
                SheetImportRun.status == "completed",
                SheetImportRun.finished_at.is_not(None),
            )
        )
    )
    if len(race_runs) != 1:
        errors.append("race_import_provenance_mismatch")
    if len(result_runs) != 1:
        errors.append("result_import_provenance_mismatch")
    race_record_targets = _import_record_target_ids(
        session,
        run_id=race_runs[0].id if len(race_runs) == 1 else None,
        record_type=LEGACY_RACE_RECORD_TYPE,
        target_entity_type="race",
    )
    result_record_targets = _import_record_target_ids(
        session,
        run_id=result_runs[0].id if len(result_runs) == 1 else None,
        record_type=LEGACY_RESULT_RECORD_TYPE,
        target_entity_type="race_result",
    )
    mapping_summary = mapping_runs[0].summary_json if len(mapping_runs) == 1 else None
    if not isinstance(mapping_summary, dict) or (
        mapping_summary.get("source_checksum") != source_checksum
        or mapping_summary.get("decision_checksum") != confirmed_mapping_decision_checksum
        or mapping_summary.get("unresolved_history_count") != 0
    ):
        errors.append("mapping_provenance_mismatch")
    source_account_seed = None
    source_account_seed_valid = True
    try:
        source_account_seed = load_applied_legacy_source_account_seed(
            session,
            source_identifier=source_identifier,
            source_checksum=source_checksum,
        )
    except LegacyImportError:
        source_account_seed_valid = False
        errors.append("source_account_seed_provenance_mismatch")

    disposition = None
    try:
        disposition = load_applied_legacy_rating_chain_dispositions(
            session,
            source_identifier=source_identifier,
            source_checksum=source_checksum,
            confirmed_decision_checksum=confirmed_disposition_decision_checksum,
        )
    except LegacyImportError:
        errors.append("rating_disposition_provenance_mismatch")
    if disposition is not None and (
        disposition.mapping_decision_checksum != confirmed_mapping_decision_checksum
        or disposition.unresolved_history_result_ids
    ):
        errors.append("rating_disposition_provenance_mismatch")

    version = session.get(RatingRuleVersion, rating_rule_version_id)
    if version is None or version.source_checksum != source_checksum:
        errors.append("rating_rule_version_mismatch")

    source_races = tuple(
        session.scalars(
            select(Race)
            .where(Race.external_source == source_identifier, Race.race_kind == "room_match")
            .order_by(Race.starts_at, Race.id)
        )
    )
    source_race_ids = {race.id for race in source_races}
    source_entries = tuple(
        session.scalars(select(RaceEntry).where(RaceEntry.race_id.in_(source_race_ids)).order_by(RaceEntry.id))
    )
    source_results = tuple(
        session.scalars(select(RaceResult).where(RaceResult.race_id.in_(source_race_ids)).order_by(RaceResult.id))
    )
    source_result_ids = {result.id for result in source_results}
    season_population, unassigned_season_race_count = _season_population(
        source_races=source_races,
        source_entries=source_entries,
        source_results=source_results,
    )
    result_run_summary = result_runs[0].summary_json if len(result_runs) == 1 else None
    if (
        len(source_races) != expected_race_count
        or len(source_entries) != expected_entry_count
        or len(source_results) != expected_result_count
        or len(race_record_targets) != expected_race_count
        or len(result_record_targets) != expected_result_count
        or set(race_record_targets) != source_race_ids
        or set(result_record_targets) != source_result_ids
        or not isinstance(result_run_summary, dict)
        or result_run_summary.get("race_count") != expected_race_count
        or result_run_summary.get("row_count") != expected_result_count
    ):
        errors.append("backfill_population_mismatch")

    mapping_records = (
        tuple(session.scalars(select(SheetImportRecord).where(SheetImportRecord.import_run_id == mapping_runs[0].id)))
        if len(mapping_runs) == 1
        else ()
    )
    mapped_account_ids = {
        int(record.target_entity_id)
        for record in mapping_records
        if record.target_entity_type == "game_account" and record.target_entity_id is not None
    }
    mapped_accounts = {
        account.id: account
        for account in session.scalars(select(GameAccount).where(GameAccount.id.in_(mapped_account_ids)))
    }
    if set(mapped_accounts) != mapped_account_ids:
        errors.append("mapping_provenance_mismatch")
    source_only_account_ids = {
        account_id for account_id, account in mapped_accounts.items() if account.persona_id is None
    }
    seeded_account_ids = set(source_account_seed.created_game_account_ids) if source_account_seed is not None else set()
    if source_account_seed_valid and not source_only_account_ids.issubset(seeded_account_ids):
        errors.append("source_account_seed_provenance_mismatch")

    if source_race_plan is None or source_result_plan is None:
        errors.append("source_plan_missing")
    else:
        source_plan_errors = _source_plan_errors(
            source_identifier=source_identifier,
            source_race_plan=source_race_plan,
            source_result_plan=source_result_plan,
            source_races=source_races,
            source_entries=source_entries,
            source_results=source_results,
            race_runs=race_runs,
            result_runs=result_runs,
            session=session,
        )
        errors.extend(source_plan_errors)
        expected_season_race_counts: dict[str, int] = {}
        for row in source_race_plan.rows:
            expected_season_race_counts[row.circle_match_season_key] = (
                expected_season_race_counts.get(row.circle_match_season_key, 0) + 1
            )
        actual_season_race_counts = {key: int(value["race_count"]) for key, value in season_population.items()}
        if unassigned_season_race_count or actual_season_race_counts != expected_season_race_counts:
            errors.append("circle_match_season_population_mismatch")

    all_events = tuple(session.scalars(select(RatingEvent).order_by(RatingEvent.id)))
    all_contexts = tuple(session.scalars(select(RaceRatingContext).order_by(RaceRatingContext.id)))
    source_result_by_id = {result.id: result for result in source_results}
    expected_event_result_ids: set[int] = set()
    expected_context_race_ids: set[int] = set()
    history_context_only_count = 0
    if disposition is not None:
        history_context_only_count = len(disposition.history_context_only_result_ids)
        eligible_rows = tuple(
            session.execute(
                select(RaceResult.id, RaceResult.race_id, RaceCondition.grade)
                .join(RaceCondition, RaceCondition.race_id == RaceResult.race_id)
                .where(
                    RaceResult.race_id.in_(source_race_ids),
                    RaceResult.is_rating_excluded.is_(False),
                    RaceResult.is_result_void.is_(False),
                )
            )
        )
        for result_id, race_id, grade in eligible_rows:
            if str(grade).strip().upper() == "OP":
                continue
            if result_id in disposition.replay_result_ids:
                expected_event_result_ids.add(result_id)
                expected_context_race_ids.add(race_id)
            elif result_id in disposition.history_context_only_result_ids:
                expected_context_race_ids.add(race_id)
    if (
        {event.race_result_id for event in all_events} != expected_event_result_ids
        or any(event.race_id not in source_race_ids for event in all_events)
        or any(event.rating_rule_version_id != rating_rule_version_id for event in all_events)
        or any(
            (result := source_result_by_id.get(event.race_result_id)) is None
            or event.race_id != result.race_id
            or event.game_account_id != result.game_account_id
            for event in all_events
        )
    ):
        errors.append("rating_event_population_mismatch")
    if {context.race_id for context in all_contexts} != expected_context_race_ids or any(
        context.race_id not in source_race_ids for context in all_contexts
    ):
        errors.append("rating_context_population_mismatch")
    if not errors:
        try:
            contexts_by_race_id = {context.race_id: context for context in all_contexts}
            for race_id in sorted(expected_context_race_ids):
                raw = (
                    contexts_by_race_id[race_id].raw_context_json
                    if race_id in contexts_by_race_id
                    and isinstance(contexts_by_race_id[race_id].raw_context_json, dict)
                    else {}
                )
                verify_source_rating_snapshot(
                    session,
                    race_id=race_id,
                    expected_recalculation_decision_checksum=(
                        disposition.decision_checksum if raw.get("mode") == "historical_recalculation" else None
                    ),
                )
        except (LegacyImportError, MatchRatingConflictError):
            errors.append("rating_source_snapshot_mismatch")

    report = {
        "race_import_run_id": race_runs[0].id if len(race_runs) == 1 else None,
        "result_import_run_id": result_runs[0].id if len(result_runs) == 1 else None,
        "mapping_import_run_id": mapping_runs[0].id if len(mapping_runs) == 1 else None,
        "source_account_seed_import_run_id": (
            source_account_seed.import_run_id if source_account_seed is not None else None
        ),
        "source_only_game_account_count": len(source_only_account_ids),
        "seeded_source_account_count": len(seeded_account_ids),
        "disposition_import_run_id": disposition.import_run_id if disposition is not None else None,
        "mapping_decision_checksum": confirmed_mapping_decision_checksum,
        "disposition_decision_checksum": confirmed_disposition_decision_checksum,
        "rating_rule_version_id": rating_rule_version_id,
        "race_count": len(source_races),
        "entry_count": len(source_entries),
        "result_count": len(source_results),
        "race_import_record_count": len(race_record_targets),
        "result_import_record_count": len(result_record_targets),
        "expected_race_count": expected_race_count,
        "expected_entry_count": expected_entry_count,
        "expected_result_count": expected_result_count,
        "season_timezone": CIRCLE_MATCH_SEASON_TIMEZONE_NAME,
        "season_population": season_population,
        "unassigned_season_race_count": unassigned_season_race_count,
        "rating_event_count": len(all_events),
        "rating_context_count": len(all_contexts),
        "history_context_only_result_count": history_context_only_count,
        "unresolved_history_result_count": (
            len(disposition.unresolved_history_result_ids) if disposition is not None else None
        ),
    }
    return report, tuple(errors)


def _season_population(
    *,
    source_races: tuple[Race, ...],
    source_entries: tuple[RaceEntry, ...],
    source_results: tuple[RaceResult, ...],
) -> tuple[dict[str, _SeasonPopulationRow], int]:
    race_seasons: dict[int, str] = {}
    population: dict[str, _SeasonPopulationRow] = {}
    unassigned = 0
    for race in source_races:
        if race.starts_at is None:
            unassigned += 1
            continue
        season = circle_match_season_for(database_datetime_as_utc(race.starts_at))
        key = season.key
        race_seasons[race.id] = key
        row = population.setdefault(
            key,
            {
                "season_name": season.name,
                "race_count": 0,
                "entry_count": 0,
                "result_count": 0,
            },
        )
        row["race_count"] += 1
    for entry in source_entries:
        key = race_seasons.get(entry.race_id)
        if key is not None:
            population[key]["entry_count"] += 1
    for result in source_results:
        key = race_seasons.get(result.race_id)
        if key is not None:
            population[key]["result_count"] += 1
    return {key: population[key] for key in sorted(population)}, unassigned


def _source_plan_errors(
    *,
    session: Session,
    source_identifier: str,
    source_race_plan: LegacyRaceImportPlan,
    source_result_plan: LegacyRoomResultPersistencePlan,
    source_races: tuple[Race, ...],
    source_entries: tuple[RaceEntry, ...],
    source_results: tuple[RaceResult, ...],
    race_runs: tuple[SheetImportRun, ...],
    result_runs: tuple[SheetImportRun, ...],
) -> tuple[str, ...]:
    errors: list[str] = []
    if (
        source_race_plan.source_identifier != source_identifier
        or source_result_plan.source_identifier != source_identifier
        or len(source_race_plan.rows) != len(source_races)
        or len(source_result_plan.rows) != len(source_results)
    ):
        return ("source_plan_mismatch",)

    race_records = _records_by_source_key(
        session,
        run_id=race_runs[0].id if len(race_runs) == 1 else None,
        record_type=LEGACY_RACE_RECORD_TYPE,
    )
    race_by_external_id = {race.external_race_id: race for race in source_races}
    for row in source_race_plan.rows:
        record = race_records.get(row.source_key)
        race = race_by_external_id.get(row.external_race_id)
        if (
            record is None
            or record.row_fingerprint != row.row_fingerprint
            or record.source_sheet_name != source_race_plan.source_sheet_name
            or record.source_row_number != row.source_row_number
            or record.status != "applied"
            or record.target_entity_type != "race"
            or race is None
            or record.target_entity_id != race.id
            or race.external_source != source_identifier
            or race.external_race_id != row.external_race_id
            or race.name != row.race_name
            or race.starts_at is None
            or database_datetime_as_utc(race.starts_at) != row.raced_at
            or race.race_kind != "room_match"
            or race.status != "result_confirmed"
        ):
            errors.append("source_race_plan_mismatch")
            break
        condition = session.scalar(select(RaceCondition).where(RaceCondition.race_id == race.id))
        if condition is None or (
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
        ) != (
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
        ):
            errors.append("source_race_plan_mismatch")
            break

    result_records = _records_by_source_key(
        session,
        run_id=result_runs[0].id if len(result_runs) == 1 else None,
        record_type=LEGACY_RESULT_RECORD_TYPE,
    )
    entry_by_target = {(entry.race_id, entry.entry_number): entry for entry in source_entries}
    result_by_target = {(result.race_id, result.entry_number): result for result in source_results}
    for row in source_result_plan.rows:
        record = result_records.get(row.source.source_key)
        entry = entry_by_target.get((row.entry.race_id, row.entry.entry_number))
        result = result_by_target.get((row.result.race_id, row.result.entry_number))
        detail = record.detail_json if record is not None and isinstance(record.detail_json, dict) else {}
        if (
            record is None
            or record.row_fingerprint != row.source.row_fingerprint
            or record.source_sheet_name != source_result_plan.source_sheet_name
            or record.source_row_number != row.source.source_row_number
            or record.status != "applied"
            or record.target_entity_type != "race_result"
            or entry is None
            or result is None
            or record.target_entity_id != result.id
            or detail.get("race_entry_id") != entry.id
            or entry.source_import_record_id != record.id
            or result.source_import_record_id != record.id
            or entry.entry_kind != row.entry.entry_kind
            or entry.entry_number_source != row.entry.entry_number_source
            or entry.player_name != row.entry.player_name
            or entry.horse_name_or_label != row.entry.horse_name_or_label
            or entry.running_style != row.entry.running_style
            or result.character_name != row.result.character_name
            or result.rank != row.result.rank
            or result.converted_rank != row.result.converted_rank
            or result.is_betting_excluded != row.result.is_betting_excluded
            or result.is_rating_excluded != row.result.is_rating_excluded
            or result.is_result_void != row.result.is_result_void
            or result.raw_result_json != row.result.build_raw_result_json()
        ):
            errors.append("source_result_plan_mismatch")
            break
    return tuple(errors)


def _completed_runs(
    session: Session,
    *,
    import_kind: str,
    source_identifier: str,
    source_checksum: str,
) -> tuple[SheetImportRun, ...]:
    return tuple(
        session.scalars(
            select(SheetImportRun)
            .where(
                SheetImportRun.import_kind == import_kind,
                SheetImportRun.source_identifier == source_identifier,
                SheetImportRun.source_checksum == source_checksum,
                SheetImportRun.status == "completed",
                SheetImportRun.finished_at.is_not(None),
            )
            .order_by(SheetImportRun.id)
        )
    )


def _run_count(session: Session, import_kind: str, source_identifier: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(SheetImportRun)
            .where(SheetImportRun.import_kind == import_kind, SheetImportRun.source_identifier == source_identifier)
        )
        or 0
    )


def _import_record_target_ids(
    session: Session,
    *,
    run_id: int | None,
    record_type: str,
    target_entity_type: str,
) -> tuple[int, ...]:
    if run_id is None:
        return ()
    target_ids = tuple(
        session.scalars(
            select(SheetImportRecord.target_entity_id).where(
                SheetImportRecord.import_run_id == run_id,
                SheetImportRecord.record_type == record_type,
                SheetImportRecord.status == "applied",
                SheetImportRecord.target_entity_type == target_entity_type,
                SheetImportRecord.target_entity_id.is_not(None),
            )
        )
    )
    if any(target_id is None for target_id in target_ids):
        return ()
    return tuple(int(target_id) for target_id in target_ids if target_id is not None)


def _records_by_source_key(
    session: Session,
    *,
    run_id: int | None,
    record_type: str,
) -> dict[str, SheetImportRecord]:
    if run_id is None:
        return {}
    return {
        record.source_key: record
        for record in session.scalars(
            select(SheetImportRecord).where(
                SheetImportRecord.import_run_id == run_id,
                SheetImportRecord.record_type == record_type,
            )
        )
    }
