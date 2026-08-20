from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.imports import (
    build_import_row_fingerprint,
    build_sheet_import_source_key,
    normalize_import_sheet_name,
    normalize_import_source_identifier,
)
from umacircle_bot.domain.time import source_datetime_to_utc
from umacircle_bot.sheets.legacy_room_ledger import (
    LegacyLedgerEntryKind,
    LegacyRoomPayoutSourceRow,
    LegacyRoomRaceSourceRow,
)
from umacircle_bot.sheets.row_parsers import MatchDataSourceRow

DEFAULT_RESULT_SHEET_NAME = "Data"


class LegacyEntryNumberSource(StrEnum):
    PAYOUT_RESULT = "payout_result"
    SYNTHETIC = "synthetic"


@dataclass(frozen=True)
class LegacyRaceConditionSnapshot:
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
class LegacyRoomResultPlanRow:
    source_row_number: int
    source_key: str
    row_fingerprint: str
    race_external_id: str
    raced_at: datetime
    race_condition: LegacyRaceConditionSnapshot
    entry_number: int
    entry_number_source: LegacyEntryNumberSource
    player_name: str
    uma_name: str
    running_style: str | None
    rank: int
    is_rating_excluded: bool
    converted_rank: int | None
    rating_before: Decimal | None
    peer_rating_difference: Decimal | None
    base_delta: Decimal | None
    adjustment_delta: Decimal | None
    rating_after: Decimal | None
    rank_ratio: Decimal | None


@dataclass(frozen=True)
class LegacyRoomResultImportPlan:
    source_identifier: str
    source_sheet_name: str
    rows: tuple[LegacyRoomResultPlanRow, ...]

    @property
    def race_count(self) -> int:
        return len({row.race_external_id for row in self.rows})

    @property
    def authoritative_entry_number_count(self) -> int:
        return sum(row.entry_number_source is LegacyEntryNumberSource.PAYOUT_RESULT for row in self.rows)

    @property
    def synthetic_entry_number_count(self) -> int:
        return sum(row.entry_number_source is LegacyEntryNumberSource.SYNTHETIC for row in self.rows)


def build_legacy_room_result_import_plan(
    data_rows: tuple[MatchDataSourceRow, ...],
    race_rows: tuple[LegacyRoomRaceSourceRow, ...],
    payout_rows: tuple[LegacyRoomPayoutSourceRow, ...],
    *,
    source_identifier: str,
    source_utc_offset_minutes: int,
    source_sheet_name: str = DEFAULT_RESULT_SHEET_NAME,
) -> LegacyRoomResultImportPlan:
    normalized_source = normalize_import_source_identifier(source_identifier)
    normalized_sheet = normalize_import_sheet_name(source_sheet_name)
    if not data_rows or not race_rows:
        raise LegacyImportError("legacy result plan requires data and race rows")

    races_by_label = _index_races(race_rows)
    payouts_by_race = _index_payouts(payout_rows)
    data_by_label: dict[str, list[MatchDataSourceRow]] = {}
    for row in data_rows:
        if row.grade is None:
            raise LegacyImportError(f"result grade is required at row {row.row_number}")
        data_by_label.setdefault(f"{row.room_match_name} ({row.grade})", []).append(row)
    if set(data_by_label) != set(races_by_label):
        missing_data = set(races_by_label) - set(data_by_label)
        extra_data = set(data_by_label) - set(races_by_label)
        detail = min(missing_data or extra_data)
        raise LegacyImportError(f"result and race label sets differ: {detail}")

    planned: list[LegacyRoomResultPlanRow] = []
    for race_label in sorted(data_by_label, key=lambda label: races_by_label[label].external_race_id):
        race = races_by_label[race_label]
        rows = sorted(data_by_label[race_label], key=_required_rank)
        _validate_race_rows(race, rows, source_utc_offset_minutes=source_utc_offset_minutes)
        rank_to_number = _build_rank_to_entry_number(
            rows,
            payout_definitions=payouts_by_race.get(race.external_race_id),
            race_label=race_label,
        )
        authoritative_ranks = {1, 2, 3} if race.external_race_id in payouts_by_race else set()
        race_time = source_datetime_to_utc(race.raced_at, utc_offset_minutes=source_utc_offset_minutes)
        for row in rows:
            rank = _required_rank(row)
            source_row_time = source_datetime_to_utc(row.raced_at, utc_offset_minutes=source_utc_offset_minutes)
            entry_number = rank_to_number[rank]
            number_source = (
                LegacyEntryNumberSource.PAYOUT_RESULT
                if rank in authoritative_ranks
                else LegacyEntryNumberSource.SYNTHETIC
            )
            fingerprint = build_import_row_fingerprint(
                (
                    source_row_time.isoformat(),
                    race_time.isoformat(),
                    race.external_race_id,
                    race.grade,
                    race.venue,
                    race.track_surface,
                    race.distance,
                    race.direction,
                    race.season,
                    race.weather,
                    race.track_condition,
                    race.condition_label,
                    race.participant_count,
                    entry_number,
                    number_source.value,
                    row.player_name,
                    row.uma_name,
                    row.running_style,
                    rank,
                    row.is_excluded,
                    row.converted_rank,
                    str(row.rating_before) if row.rating_before is not None else None,
                    str(row.rating_diff) if row.rating_diff is not None else None,
                    str(row.base_delta) if row.base_delta is not None else None,
                    str(row.adjustment_delta) if row.adjustment_delta is not None else None,
                    str(row.rating_after) if row.rating_after is not None else None,
                    str(row.rank_ratio) if row.rank_ratio is not None else None,
                )
            )
            planned.append(
                LegacyRoomResultPlanRow(
                    source_row_number=row.row_number,
                    source_key=build_sheet_import_source_key(
                        source_identifier=normalized_source,
                        sheet_name=normalized_sheet,
                        row_number=row.row_number,
                    ),
                    row_fingerprint=fingerprint,
                    race_external_id=str(race.external_race_id),
                    raced_at=race_time,
                    race_condition=LegacyRaceConditionSnapshot(
                        grade=race.grade,
                        venue=race.venue,
                        track_surface=race.track_surface,
                        distance=race.distance,
                        direction=race.direction,
                        season=race.season,
                        weather=race.weather,
                        track_condition=race.track_condition,
                        condition_label=race.condition_label,
                        participant_count=race.participant_count,
                    ),
                    entry_number=entry_number,
                    entry_number_source=number_source,
                    player_name=row.player_name,
                    uma_name=row.uma_name,
                    running_style=row.running_style,
                    rank=rank,
                    is_rating_excluded=row.is_excluded,
                    converted_rank=row.converted_rank,
                    rating_before=row.rating_before,
                    peer_rating_difference=row.rating_diff,
                    base_delta=row.base_delta,
                    adjustment_delta=row.adjustment_delta,
                    rating_after=row.rating_after,
                    rank_ratio=row.rank_ratio,
                )
            )
    return LegacyRoomResultImportPlan(
        source_identifier=normalized_source,
        source_sheet_name=normalized_sheet,
        rows=tuple(planned),
    )


def _index_races(rows: tuple[LegacyRoomRaceSourceRow, ...]) -> dict[str, LegacyRoomRaceSourceRow]:
    indexed: dict[str, LegacyRoomRaceSourceRow] = {}
    seen_ids: set[int] = set()
    for row in rows:
        if row.race_label in indexed or row.external_race_id in seen_ids:
            raise LegacyImportError(f"duplicate legacy race metadata at row {row.row_number}")
        indexed[row.race_label] = row
        seen_ids.add(row.external_race_id)
    return indexed


def _index_payouts(
    rows: tuple[LegacyRoomPayoutSourceRow, ...],
) -> dict[int, dict[str, tuple[int, ...]]]:
    indexed: dict[int, dict[str, tuple[int, ...]]] = {}
    for row in rows:
        if row.entry_kind is LegacyLedgerEntryKind.GRANT:
            continue
        if row.bet_type is None:
            raise LegacyImportError(f"payout bet type is missing at row {row.row_number}")
        definitions = indexed.setdefault(row.external_race_id, {})
        if row.bet_type in definitions:
            raise LegacyImportError(f"duplicate payout type at row {row.row_number}")
        definitions[row.bet_type] = row.numbers
    for race_id, definitions in indexed.items():
        if set(definitions) != {"win", "quinella", "trio"}:
            raise LegacyImportError(f"payout definitions are incomplete for race {race_id}")
    return indexed


def _validate_race_rows(
    race: LegacyRoomRaceSourceRow,
    rows: list[MatchDataSourceRow],
    *,
    source_utc_offset_minutes: int,
) -> None:
    ranks = [_required_rank(row) for row in rows]
    if len(ranks) != len(set(ranks)) or set(ranks) != set(range(1, len(rows) + 1)):
        raise LegacyImportError(f"result ranks are not contiguous for race {race.external_race_id}")
    race_time = source_datetime_to_utc(race.raced_at, utc_offset_minutes=source_utc_offset_minutes)
    for row in rows:
        row_time = source_datetime_to_utc(row.raced_at, utc_offset_minutes=source_utc_offset_minutes)
        expected = (
            race.grade,
            race.venue,
            race.track_surface,
            race.distance,
            race.direction,
            race.season,
            race.weather,
            race.track_condition,
            race.condition_label,
            race.participant_count,
        )
        actual = (
            row.grade,
            row.venue,
            row.track_surface,
            row.distance,
            row.direction,
            row.season,
            row.weather,
            row.track_condition,
            row.condition_label,
            row.max_participant_count,
        )
        if abs((row_time - race_time).total_seconds()) > 1 or actual != expected:
            raise LegacyImportError(f"result race context mismatch at row {row.row_number}")
        rating_values = (
            row.converted_rank,
            row.rating_before,
            row.rating_diff,
            row.base_delta,
            row.adjustment_delta,
            row.rating_after,
            row.rank_ratio,
        )
        if row.is_excluded:
            if any(value is not None for value in rating_values):
                raise LegacyImportError(f"excluded result contains rating values at row {row.row_number}")
        elif any(value is None for value in rating_values):
            raise LegacyImportError(f"included result is missing rating values at row {row.row_number}")


def _build_rank_to_entry_number(
    rows: list[MatchDataSourceRow],
    *,
    payout_definitions: dict[str, tuple[int, ...]] | None,
    race_label: str,
) -> dict[int, int]:
    assigned: dict[int, int] = {}
    used_numbers: set[int] = set()
    if payout_definitions is not None:
        win = set(payout_definitions["win"])
        quinella = set(payout_definitions["quinella"])
        trio = set(payout_definitions["trio"])
        if len(win) != 1 or len(quinella) != 2 or len(trio) != 3 or not win < quinella or not quinella < trio:
            raise LegacyImportError(f"payout result numbers are inconsistent for race {race_label}")
        assigned[1] = next(iter(win))
        assigned[2] = next(iter(quinella - win))
        assigned[3] = next(iter(trio - quinella))
        used_numbers.update(trio)

    next_number = 1
    for row in rows:
        rank = _required_rank(row)
        if rank in assigned:
            continue
        while next_number in used_numbers:
            next_number += 1
        assigned[rank] = next_number
        used_numbers.add(next_number)
        next_number += 1
    return assigned


def _required_rank(row: MatchDataSourceRow) -> int:
    if row.rank is None:
        raise LegacyImportError(f"result rank is required at row {row.row_number}")
    return row.rank
