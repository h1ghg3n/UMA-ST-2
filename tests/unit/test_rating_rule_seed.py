from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from uma_st2.application.execution import CommandRunner
from uma_st2.application.rating import (
    RatingRuleSeedCommands,
    RatingRuleSeedConflictError,
    SeedRatingRuleVersion,
    StoredRatingRuleVersion,
)
from uma_st2.domain.match import MatchGrade
from uma_st2.domain.rating import RatingRule
from uma_st2.infrastructure.master_data import prepare_rating_rule_workbook

NOW = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)


class FakeRatingRuleSeedRepository:
    def __init__(self) -> None:
        self.version: StoredRatingRuleVersion | None = None
        self.created_rules: tuple[RatingRule, ...] = ()

    def find_source_version(self, *, command: SeedRatingRuleVersion) -> StoredRatingRuleVersion | None:
        return self.version

    def lock_latest_version_number(self) -> int:
        return self.version.version_number if self.version is not None else 0

    def create_version(
        self,
        *,
        command: SeedRatingRuleVersion,
        version_number: int,
        created_at: datetime,
    ) -> StoredRatingRuleVersion:
        self.created_rules = command.rules
        self.version = StoredRatingRuleVersion(
            version_id=41,
            version_number=version_number,
            rule_set_checksum=command.rule_set_checksum,
            rule_count=len(command.rules),
            created_at=created_at,
        )
        return self.version


class FakeRatingRuleSeedUnitOfWork:
    def __init__(self, repository: FakeRatingRuleSeedRepository) -> None:
        self.rating_rule_seed = repository

    def __enter__(self) -> FakeRatingRuleSeedUnitOfWork:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _command(*, checksum: str = "a" * 64) -> SeedRatingRuleVersion:
    return SeedRatingRuleVersion(
        source_identifier="approved-room-match-rating",
        source_checksum=checksum,
        source_sheet_name="Rate 기준표",
        source_range="A1:D4",
        rules=(
            RatingRule(MatchGrade.G1, 2, 1, Decimal(22)),
            RatingRule(MatchGrade.G1, 2, 2, Decimal("-9.7336")),
        ),
    )


def test_seed_is_idempotent_by_reviewed_source_provenance() -> None:
    repository = FakeRatingRuleSeedRepository()
    commands = RatingRuleSeedCommands(
        CommandRunner(lambda: FakeRatingRuleSeedUnitOfWork(repository)),
        clock=lambda: NOW,
    )

    first = commands.seed(_command())
    second = commands.seed(_command())

    assert first.created is True
    assert second.created is False
    assert first.version_id == second.version_id == 41
    assert first.version_number == second.version_number == 1
    assert len(repository.created_rules) == 2


def test_same_source_provenance_with_different_rule_content_fails_closed() -> None:
    repository = FakeRatingRuleSeedRepository()
    commands = RatingRuleSeedCommands(
        CommandRunner(lambda: FakeRatingRuleSeedUnitOfWork(repository)), clock=lambda: NOW
    )
    commands.seed(_command())
    repository.version = StoredRatingRuleVersion(
        version_id=41,
        version_number=1,
        rule_set_checksum="f" * 64,
        rule_count=2,
        created_at=NOW,
    )

    with pytest.raises(RatingRuleSeedConflictError, match="different canonical rule content"):
        commands.seed(_command())


def test_workbook_parser_maps_legacy_grade_headers_to_v2_and_preserves_provenance(tmp_path: Path) -> None:
    workbook_path = tmp_path / "rating-rules.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Rate 기준표"
    worksheet.append(["GI", None, "레이스 인원"])
    worksheet.append([None, None, 2, 3])
    worksheet.append(["착순", 1, 22, 23])
    worksheet.append([None, 2, -9.7336, -0.9408])
    workbook.save(workbook_path)
    workbook.close()

    command = prepare_rating_rule_workbook(
        workbook_path,
        source_identifier="approved-room-match-rating",
    )

    assert command.source_sheet_name == "Rate 기준표"
    assert command.source_range == "A1:D4"
    assert len(command.source_checksum) == 64
    assert [(rule.grade, rule.participant_count, rule.converted_rank, rule.base_delta) for rule in command.rules] == [
        (MatchGrade.G1, 2, 1, Decimal("22.000000000000000000")),
        (MatchGrade.G1, 2, 2, Decimal("-9.733600000000000000")),
        (MatchGrade.G1, 3, 1, Decimal("23.000000000000000000")),
        (MatchGrade.G1, 3, 2, Decimal("-0.940800000000000000")),
    ]
