"""Circle Match Season export application and workbook v1 tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from io import BytesIO

import pytest
from openpyxl import load_workbook

from uma_st2.application.execution import QueryRunner
from uma_st2.application.exporting import (
    MATCH_EXPORT_PROJECTION_VERSION,
    MATCH_EXPORT_WORKBOOK_SCHEMA_VERSION,
    ExportArtifact,
    MatchExportBet,
    MatchExportCondition,
    MatchExportCourse,
    MatchExportEntry,
    MatchExportMatch,
    MatchExportRatingTransaction,
    MatchExportSeasonChoice,
    MatchSeasonExportInvalidSourceError,
    MatchSeasonExports,
    MatchSeasonExportSource,
    MatchSeasonExportUnavailableError,
    match_export_season_for_datetime,
    parse_match_export_season,
)
from uma_st2.domain.betting import BetStatus, BetType
from uma_st2.domain.identity import GameRegion
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchRatingDisposition,
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)
from uma_st2.infrastructure.exporting import MatchSeasonXlsxRenderer

SOURCE_CUTOFF = datetime(2026, 8, 29, 1, 2, 3, tzinfo=UTC)
GENERATED_AT = datetime(2026, 8, 29, 1, 3, 4, tzinfo=UTC)
SEASON = parse_match_export_season("2026-split-2")


def _course() -> MatchExportCourse:
    return MatchExportCourse(
        stadium_name="=Formula Stadium",
        course_id=10,
        surface=MatchSurface.TURF,
        distance=1600,
        direction=MatchDirection.RIGHT,
        layout=StadiumCourseLayout.OUTER,
    )


def _entry(*, entry_id: int, account_id: int, number: int, rank: int | None) -> MatchExportEntry:
    return MatchExportEntry(
        id=entry_id,
        entry_number=number,
        game_account_id=account_id,
        game_account_name="=Formula Account" if entry_id == 101 else "Imported Account",
        game_region=GameRegion.KR,
        owner_at_event_display_name="Owner",
        affiliation_at_event="Circle",
        character_name="Character",
        variant_name=None,
        running_style="leader",
        training_grade="UG",
        rank=rank,
        rating_disposition=(MatchRatingDisposition.RATED if rank is not None else None),
        popularity_rank=1 if rank is not None else None,
        margin=None,
    )


def _source() -> MatchSeasonExportSource:
    imported = MatchExportMatch(
        id=1,
        name="Imported Match",
        description=None,
        source_kind=MatchSourceKind.IMPORTED_V1,
        grade=MatchGrade.G2,
        scheduled_at=datetime(2026, 8, 1, tzinfo=UTC),
        status=MatchStatus.RESULT_CONFIRMED,
        terminal_reason=None,
        finish_time_ms=99999,
        course=_course(),
        condition=None,
        entries=(_entry(entry_id=100, account_id=1000, number=1, rank=1),),
    )
    native_entry = _entry(entry_id=101, account_id=1001, number=2, rank=1)
    native = MatchExportMatch(
        id=2,
        name="=Formula Native",
        description="Current native facts",
        source_kind=MatchSourceKind.NATIVE_V2,
        grade=MatchGrade.G1,
        scheduled_at=datetime(2026, 8, 2, tzinfo=UTC),
        status=MatchStatus.VOIDED,
        terminal_reason="rollback",
        finish_time_ms=98765,
        course=_course(),
        condition=MatchExportCondition(
            season=MatchSeason.SUMMER,
            weather=MatchWeather.SUNNY,
            time_of_day=MatchTimeOfDay.DAY,
            track_condition=MatchTrackCondition.FIRM,
        ),
        settlement_evidence_present=True,
        rollback_evidence_present=True,
        entries=(native_entry,),
        bets=(
            MatchExportBet(
                id=20,
                persona_display_name="=Formula Bettor",
                bet_type=BetType.WIN,
                selection_entry_ids=(native_entry.id,),
                amount=10,
                status=BetStatus.CANCELLED,
                created_at=datetime(2026, 8, 2, 1, tzinfo=UTC),
                updated_at=datetime(2026, 8, 2, 2, tzinfo=UTC),
                applied_odds=Decimal("2.0"),
                payout_amount=20,
                payout_group_bet_ids=(20,),
                payout_reversal_amount=20,
                refund_amount=10,
                refund_group_bet_ids=(20,),
                refund_reason="operator rollback",
                refunded_at=datetime(2026, 8, 2, 3, tzinfo=UTC),
            ),
        ),
        ratings=(
            MatchExportRatingTransaction(
                id=30,
                match_entry_id=native_entry.id,
                rating_rule_version=4,
                rating_before=Decimal("100.000000000000000000"),
                amount=Decimal("1.250000000000000000"),
                rating_after=Decimal("101.250000000000000000"),
                created_at=datetime(2026, 8, 2, 2, tzinfo=UTC),
            ),
        ),
    )
    return MatchSeasonExportSource(
        season=SEASON,
        source_cutoff=SOURCE_CUTOFF,
        matches=(imported, native),
    )


class FakeRepository:
    def __init__(self, source: MatchSeasonExportSource | None) -> None:
        self.source = source

    def search_seasons(self, *, query: str, limit: int) -> tuple[MatchExportSeasonChoice, ...]:
        choices = (
            MatchExportSeasonChoice("2026-split-2", "2026 Split 2"),
            MatchExportSeasonChoice("2026-split-1", "2026 Split 1"),
        )
        normalized = query.casefold()
        return tuple(
            choice for choice in choices if normalized in choice.key.casefold() or normalized in choice.name.casefold()
        )[:limit]

    def get_season_source(
        self,
        *,
        season,
        source_cutoff: datetime,
    ) -> MatchSeasonExportSource | None:  # type: ignore[no-untyped-def]
        assert season == SEASON
        assert source_cutoff == SOURCE_CUTOFF
        return self.source


class FakeUnitOfWork:
    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository
        self.active = False
        self.rollback_calls = 0

    @property
    def match_season_exports(self) -> FakeRepository:
        assert self.active
        return self.repository

    def __enter__(self):  # type: ignore[no-untyped-def]
        self.active = True
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:  # type: ignore[no-untyped-def]
        self.active = False
        return False

    def commit(self) -> None:
        raise AssertionError("Export query must not commit.")

    def rollback(self) -> None:
        self.rollback_calls += 1


@dataclass
class RecordingRenderer:
    unit_of_work: FakeUnitOfWork
    projection: object | None = None

    def render(self, projection, *, generated_at: datetime) -> ExportArtifact:  # type: ignore[no-untyped-def]
        assert not self.unit_of_work.active
        self.projection = projection
        content = b"workbook"
        return ExportArtifact(
            filename="match.xlsx",
            media_type="application/xlsx",
            content=content,
            sha256_hex=sha256(content).hexdigest(),
            generated_at=generated_at,
            source_cutoff=projection.source_cutoff,
            schema_version=MATCH_EXPORT_WORKBOOK_SCHEMA_VERSION,
            projection_version=MATCH_EXPORT_PROJECTION_VERSION,
            scope_type="match_season",
            scope_id=projection.season.key,
            scope_name=projection.season.name,
            row_count=projection.workbook_data_row_count,
        )


def _exports(
    source: MatchSeasonExportSource | None,
) -> tuple[MatchSeasonExports, FakeUnitOfWork, RecordingRenderer]:
    unit_of_work = FakeUnitOfWork(FakeRepository(source))
    renderer = RecordingRenderer(unit_of_work)
    clock_values = iter((SOURCE_CUTOFF, GENERATED_AT))
    exports = MatchSeasonExports(
        QueryRunner(lambda: unit_of_work),  # type: ignore[arg-type]
        renderer,
        clock=lambda: next(clock_values),
    )
    return exports, unit_of_work, renderer


def test_kst_calendar_half_boundary_and_stable_key() -> None:
    before = datetime(2026, 6, 30, 14, 59, 59, 999999, tzinfo=UTC)
    boundary = datetime(2026, 6, 30, 15, tzinfo=UTC)

    assert match_export_season_for_datetime(before).key == "2026-split-1"
    assert match_export_season_for_datetime(boundary) == SEASON
    assert SEASON.starts_at == boundary
    assert SEASON.ends_at == datetime(2026, 12, 31, 15, tzinfo=UTC)


def test_application_closes_query_uow_before_render_and_revalidates_scope() -> None:
    exports, unit_of_work, renderer = _exports(_source())

    artifact = exports.export_season(season_key=SEASON.key)

    assert unit_of_work.rollback_calls == 1
    assert renderer.projection is not None
    assert artifact.scope_id == SEASON.key
    assert artifact.row_count == 6


def test_application_searches_actual_choices_and_rejects_invalid_source() -> None:
    exports, unit_of_work, _ = _exports(_source())
    assert exports.search_seasons(query="split 1") == (MatchExportSeasonChoice("2026-split-1", "2026 Split 1"),)
    assert unit_of_work.rollback_calls == 1

    exports, _, renderer = _exports(None)
    with pytest.raises(MatchSeasonExportUnavailableError):
        exports.export_season(season_key=SEASON.key)
    assert renderer.projection is None

    malformed = _source()
    imported, native = malformed.matches
    malformed = replace(malformed, matches=(replace(imported, bets=native.bets), native))
    exports, _, renderer = _exports(malformed)
    with pytest.raises(MatchSeasonExportInvalidSourceError):
        exports.export_season(season_key=SEASON.key)
    assert renderer.projection is None


def test_xlsx_v2_preserves_raw_imported_and_audit_backed_native_facts() -> None:
    exports, _, recording_renderer = _exports(_source())
    exports.export_season(season_key=SEASON.key)
    projection = recording_renderer.projection
    assert projection is not None

    artifact = MatchSeasonXlsxRenderer().render(projection, generated_at=GENERATED_AT)

    assert artifact.filename == "match_2026-split-2_20260829T010304Z.xlsx"
    assert artifact.sha256_hex == sha256(artifact.content).hexdigest()
    workbook = load_workbook(BytesIO(artifact.content), data_only=False)
    try:
        assert workbook.sheetnames == ["시즌 요약", "경기", "출전 및 결과", "베팅 및 정산", "레이팅"]
        assert workbook["시즌 요약"]["B2"].value == "2026-split-2"
        match_rows = list(workbook["경기"].iter_rows(min_row=2, values_only=True))
        assert match_rows[1][1] == "'=Formula Native"
        assert match_rows[0][3] == "imported_v1"

        entry_rows = list(workbook["출전 및 결과"].iter_rows(min_row=2, values_only=True))
        assert entry_rows[1][8] == "'=Formula Account"
        assert entry_rows[1][17] == "rated"
        bet_rows = list(workbook["베팅 및 정산"].iter_rows(min_row=2, values_only=True))
        assert len(bet_rows) == 1
        assert bet_rows[0][13:19] == ("2.0", 20, 20, 10, "operator rollback", "2026-08-02 12:00:00 KST")
        rating_rows = list(workbook["레이팅"].iter_rows(min_row=2, values_only=True))
        assert rating_rows[0][14:17] == ("100.0", "1.2", "101.2")
    finally:
        workbook.close()
