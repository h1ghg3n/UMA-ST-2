from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from umacircle_bot.services.legacy_result_import import (
    LEGACY_ROOM_RESULT_IMPORT_MANIFEST,
    LEGACY_ROOM_RESULT_SUPPORTED_MANIFESTS,
)
from umacircle_bot.sheets.legacy_result_plan import LegacyEntryNumberSource, LegacyRoomResultImportPlan

RATING_DISPLAY_TOLERANCE = Decimal("0.0000001")


@dataclass(frozen=True, slots=True)
class LegacyResultVerificationIssue:
    code: str
    source_row_number: int
    race_external_id: str
    message: str


@dataclass(frozen=True, slots=True)
class LegacyResultVerificationReport:
    row_count: int
    race_count: int
    authoritative_entry_number_count: int
    synthetic_entry_number_count: int
    rating_excluded_count: int
    expected_row_count: int
    expected_race_count: int
    expected_authoritative_entry_number_count: int
    expected_synthetic_entry_number_count: int
    expected_rating_excluded_count: int
    arithmetic_issues: tuple[LegacyResultVerificationIssue, ...]
    continuity_issues: tuple[LegacyResultVerificationIssue, ...]

    @property
    def has_calculation_issues(self) -> bool:
        return bool(self.arithmetic_issues or self.continuity_issues)

    @property
    def matches_canonical_manifest(self) -> bool:
        return (
            self.row_count == self.expected_row_count
            and self.race_count == self.expected_race_count
            and self.authoritative_entry_number_count == self.expected_authoritative_entry_number_count
            and self.synthetic_entry_number_count == self.expected_synthetic_entry_number_count
            and self.rating_excluded_count == self.expected_rating_excluded_count
        )

    @property
    def requires_operator_review(self) -> bool:
        # A verification report is evidence only. It never authorizes a result import.
        return True


def verify_legacy_room_result_plan(plan: LegacyRoomResultImportPlan) -> LegacyResultVerificationReport:
    """Audit a parsed legacy result plan without opening or writing a database session.

    The legacy workbook stores cached Excel results. Its `RateAfter` formula is
    `MAX(RateBefore + deltaRate1 + deltaRate2, 0)`. Cached display values may
    differ by up to seven decimal places, so this checks the formula within
    observed display precision rather than inventing a rating engine.
    """

    arithmetic_issues: list[LegacyResultVerificationIssue] = []
    rows_by_player: dict[str, list] = defaultdict(list)

    for row in plan.rows:
        if row.is_rating_excluded:
            continue

        assert row.rating_before is not None
        assert row.base_delta is not None
        assert row.adjustment_delta is not None
        assert row.rating_after is not None
        expected_after = max(row.rating_before + row.base_delta + row.adjustment_delta, Decimal())
        if abs(expected_after - row.rating_after) > RATING_DISPLAY_TOLERANCE:
            arithmetic_issues.append(
                LegacyResultVerificationIssue(
                    code="rating_arithmetic_mismatch",
                    source_row_number=row.source_row_number,
                    race_external_id=row.race_external_id,
                    message="stored rating_after does not match the workbook rating formula",
                )
            )
        rows_by_player[row.player_name].append(row)

    continuity_issues: list[LegacyResultVerificationIssue] = []
    for player_rows in rows_by_player.values():
        player_rows.sort(key=lambda row: (row.raced_at, row.source_row_number))
        for previous, current in zip(player_rows, player_rows[1:], strict=False):
            assert previous.rating_after is not None
            assert current.rating_before is not None
            if abs(previous.rating_after - current.rating_before) > RATING_DISPLAY_TOLERANCE:
                continuity_issues.append(
                    LegacyResultVerificationIssue(
                        code="rating_continuity_mismatch",
                        source_row_number=current.source_row_number,
                        race_external_id=current.race_external_id,
                        message="rating_before does not continue the player's previous rating_after",
                    )
                )

    row_count = len(plan.rows)
    race_count = plan.race_count
    authoritative_count = sum(row.entry_number_source is LegacyEntryNumberSource.PAYOUT_RESULT for row in plan.rows)
    synthetic_count = sum(row.entry_number_source is LegacyEntryNumberSource.SYNTHETIC for row in plan.rows)
    rating_excluded_count = sum(row.is_rating_excluded for row in plan.rows)
    expected_manifest = next(
        (
            manifest
            for manifest in LEGACY_ROOM_RESULT_SUPPORTED_MANIFESTS
            if (
                row_count,
                race_count,
                authoritative_count,
                synthetic_count,
                rating_excluded_count,
            )
            == (
                manifest.row_count,
                manifest.race_count,
                manifest.authoritative_entry_number_count,
                manifest.synthetic_entry_number_count,
                manifest.rating_excluded_count,
            )
        ),
        LEGACY_ROOM_RESULT_IMPORT_MANIFEST,
    )
    return LegacyResultVerificationReport(
        row_count=row_count,
        race_count=race_count,
        authoritative_entry_number_count=authoritative_count,
        synthetic_entry_number_count=synthetic_count,
        rating_excluded_count=rating_excluded_count,
        expected_row_count=expected_manifest.row_count,
        expected_race_count=expected_manifest.race_count,
        expected_authoritative_entry_number_count=expected_manifest.authoritative_entry_number_count,
        expected_synthetic_entry_number_count=expected_manifest.synthetic_entry_number_count,
        expected_rating_excluded_count=expected_manifest.rating_excluded_count,
        arithmetic_issues=tuple(arithmetic_issues),
        continuity_issues=tuple(continuity_issues),
    )
