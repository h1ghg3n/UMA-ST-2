"""MariaDB evidence for the reviewed Rating rule one-shot seed command."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from openpyxl import Workbook
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from uma_st2.adapters.cli import RatingRuleSeedCliRequest
from uma_st2.config import DatabaseSettings
from uma_st2.infrastructure.database.orm import RatingRuleORM, RatingRuleVersionORM
from uma_st2.rating_rule_seed_entrypoint import run_rating_rule_seed

pytestmark = pytest.mark.integration


def _write_workbook(path: Path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Rate 기준표"
    worksheet.append(["GI", None, "레이스 인원"])
    worksheet.append([None, None, 2])
    worksheet.append(["착순", 1, 22])
    worksheet.append([None, 2, -9.7336])
    workbook.save(path)
    workbook.close()


def test_one_shot_cli_path_creates_one_immutable_version_and_exact_retries(
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    suffix = uuid4().hex
    workbook_path = tmp_path / "rating-rules.xlsx"
    _write_workbook(workbook_path)
    settings = DatabaseSettings(
        DATABASE_URL=migrated_engine.url.render_as_string(hide_password=False),
    )
    request = RatingRuleSeedCliRequest(
        workbook_path=workbook_path,
        source_identifier=f"integration-reviewed-rating-{suffix}",
        sheet_name=None,
    )

    first = run_rating_rule_seed(settings, request)
    second = run_rating_rule_seed(settings, request)

    assert first.created is True
    assert second.created is False
    assert first.version_id == second.version_id
    assert first.rule_set_checksum == second.rule_set_checksum
    with migrated_engine.begin() as connection:
        stored = connection.execute(
            select(
                RatingRuleVersionORM.source_sheet_name,
                RatingRuleVersionORM.source_range,
                RatingRuleVersionORM.rule_count,
            ).where(RatingRuleVersionORM.id == first.version_id)
        ).one()
        rules = connection.execute(
            select(RatingRuleORM.converted_rank, RatingRuleORM.base_delta)
            .where(RatingRuleORM.rating_rule_version_id == first.version_id)
            .order_by(RatingRuleORM.converted_rank)
        ).all()
        assert stored == ("Rate 기준표", "A1:C4", 2)
        assert rules == [
            (1, Decimal("22.000000000000000000")),
            (2, Decimal("-9.733600000000000000")),
        ]
        connection.execute(delete(RatingRuleORM).where(RatingRuleORM.rating_rule_version_id == first.version_id))
        connection.execute(delete(RatingRuleVersionORM).where(RatingRuleVersionORM.id == first.version_id))
