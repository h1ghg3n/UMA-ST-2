"""Tests for the explicit one-shot Rating rule seed boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from uma_st2 import rating_rule_seed_entrypoint
from uma_st2.adapters.cli import RatingRuleSeedCliRequest, parse_rating_rule_seed_cli_request
from uma_st2.application.rating import RatingRuleSeedError, SeededRatingRuleVersion, SeedRatingRuleVersion

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


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


class FakeSettings:
    database_url_value = "mysql+pymysql://user:password@db/app?charset=utf8mb4"


class FakeDatabaseRuntime:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


class FakeCommands:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.commands: list[SeedRatingRuleVersion] = []

    def seed(self, command: SeedRatingRuleVersion) -> SeededRatingRuleVersion:
        self.commands.append(command)
        if self.failure is not None:
            raise self.failure
        return SeededRatingRuleVersion(
            version_id=41,
            version_number=2,
            rule_set_checksum=command.rule_set_checksum,
            rule_count=len(command.rules),
            created=True,
            created_at=NOW,
        )


def test_cli_parser_requires_explicit_workbook_and_source_identifier(tmp_path: Path) -> None:
    path = tmp_path / "rules.xlsx"

    assert parse_rating_rule_seed_cli_request(
        ["--workbook", str(path), "--source-identifier", " reviewed-v2-rules "]
    ) == RatingRuleSeedCliRequest(
        workbook_path=path,
        source_identifier="reviewed-v2-rules",
        sheet_name=None,
    )
    assert (
        parse_rating_rule_seed_cli_request(
            [
                "--workbook",
                str(path),
                "--source-identifier",
                "reviewed-v2-rules",
                "--sheet-name",
                "Rules",
            ]
        ).sheet_name
        == "Rules"
    )

    with pytest.raises(SystemExit):
        parse_rating_rule_seed_cli_request(["--workbook", str(path), "--source-identifier", "   "])


@pytest.mark.parametrize("failure", [None, RuntimeError("seed failed")])
def test_entrypoint_seeds_parsed_workbook_and_always_disposes_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Exception | None,
) -> None:
    workbook_path = tmp_path / "rules.xlsx"
    _write_workbook(workbook_path)
    database_runtime = FakeDatabaseRuntime()
    commands = FakeCommands(failure=failure)
    monkeypatch.setattr(
        rating_rule_seed_entrypoint.DatabaseRuntime,
        "from_url",
        classmethod(lambda cls, *_args, **_kwargs: database_runtime),
    )
    monkeypatch.setattr(
        rating_rule_seed_entrypoint,
        "compose_rating_rule_seed_commands",
        lambda _runtime: commands,
    )
    request = RatingRuleSeedCliRequest(
        workbook_path=workbook_path,
        source_identifier="reviewed-v2-rules",
        sheet_name=None,
    )

    if failure is None:
        receipt = rating_rule_seed_entrypoint.run_rating_rule_seed(FakeSettings(), request)  # type: ignore[arg-type]
        assert receipt.created is True
        assert len(commands.commands) == 1
        assert commands.commands[0].source_sheet_name == "Rate 기준표"
    else:
        with pytest.raises(RuntimeError, match="seed failed"):
            rating_rule_seed_entrypoint.run_rating_rule_seed(FakeSettings(), request)  # type: ignore[arg-type]

    assert database_runtime.dispose_calls == 1


def test_invalid_workbook_fails_before_database_runtime_is_created(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    invalid_path = tmp_path / "rules.xlsx"
    invalid_path.write_text("not an XLSX", encoding="utf-8")
    database_created = False

    def create_runtime(*_args: object, **_kwargs: object) -> object:
        nonlocal database_created
        database_created = True
        raise AssertionError("Database runtime must not be created for an invalid workbook.")

    monkeypatch.setattr(
        rating_rule_seed_entrypoint.DatabaseRuntime,
        "from_url",
        classmethod(lambda cls, *args, **kwargs: create_runtime(*args, **kwargs)),
    )

    with pytest.raises(RatingRuleSeedError, match="valid Rating rule workbook"):
        rating_rule_seed_entrypoint.run_rating_rule_seed(
            FakeSettings(),  # type: ignore[arg-type]
            RatingRuleSeedCliRequest(
                workbook_path=invalid_path,
                source_identifier="reviewed-v2-rules",
                sheet_name=None,
            ),
        )

    assert database_created is False
