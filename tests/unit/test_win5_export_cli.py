"""Tests for the one-shot WIN5 Season export CLI boundary."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from uma_st2 import win5_export_entrypoint
from uma_st2.adapters.cli import (
    Win5ExportCliArtifactExistsError,
    Win5ExportCliArtifactWriteError,
    Win5SeasonExportCliAdapter,
    Win5SeasonExportCliRequest,
    parse_win5_export_cli_request,
)
from uma_st2.application.exporting import (
    ExportArtifact,
    Win5SeasonExportUnavailableError,
)

NOW = datetime(2026, 8, 29, 1, 2, 3, tzinfo=UTC)


def _artifact() -> ExportArtifact:
    content = b"complete-xlsx-content"
    return ExportArtifact(
        filename="win5_7_season_20260829T010203Z.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        content=content,
        sha256_hex=sha256(content).hexdigest(),
        generated_at=NOW,
        source_cutoff=NOW,
        schema_version="win5-season-xlsx/v1",
        projection_version="win5-season-projection/v1",
        scope_type="win5_season",
        scope_id="7",
        scope_name="Season",
        row_count=12,
    )


class FakeExports:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[int] = []
        self.artifact = _artifact()

    def export_season(self, *, season_id: int) -> ExportArtifact:
        self.calls.append(season_id)
        if self.failure is not None:
            raise self.failure
        return self.artifact


class FakeSettings:
    database_url_value = "mysql+pymysql://user:password@db/test?charset=utf8mb4"


class FakeDatabaseRuntime:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


class FailingWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file_descriptor: int | None = None

    def __enter__(self) -> FailingWriter:
        self.file_descriptor = os.open(
            self.path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
        return self

    def write(self, content: bytes) -> int:
        assert self.file_descriptor is not None
        os.write(self.file_descriptor, content[:3])
        raise OSError("simulated partial write")

    def __exit__(self, *_args: object) -> None:
        assert self.file_descriptor is not None
        os.close(self.file_descriptor)


def test_cli_parser_requires_one_positive_season_id() -> None:
    assert parse_win5_export_cli_request(["--season-id", "7"]) == Win5SeasonExportCliRequest(season_id=7)

    with pytest.raises(SystemExit):
        parse_win5_export_cli_request(["--season-id", "0"])


def test_cli_adapter_writes_exact_application_artifact_without_overwrite(tmp_path: Path) -> None:
    exports = FakeExports()
    adapter = Win5SeasonExportCliAdapter(exports=exports)  # type: ignore[arg-type]
    request = Win5SeasonExportCliRequest(season_id=7)

    receipt = adapter.execute(request, output_directory=tmp_path)

    assert exports.calls == [7]
    assert receipt.path == tmp_path / exports.artifact.filename
    assert receipt.path.read_bytes() == exports.artifact.content
    assert receipt.artifact is exports.artifact

    with pytest.raises(Win5ExportCliArtifactExistsError):
        adapter.execute(request, output_directory=tmp_path)

    assert receipt.path.read_bytes() == exports.artifact.content


def test_cli_adapter_expected_application_rejection_creates_no_file(tmp_path: Path) -> None:
    exports = FakeExports(failure=Win5SeasonExportUnavailableError("unavailable"))
    adapter = Win5SeasonExportCliAdapter(exports=exports)  # type: ignore[arg-type]

    with pytest.raises(Win5SeasonExportUnavailableError):
        adapter.execute(
            Win5SeasonExportCliRequest(season_id=7),
            output_directory=tmp_path,
        )

    assert list(tmp_path.iterdir()) == []


def test_cli_adapter_removes_incomplete_target_after_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = Win5SeasonExportCliAdapter(exports=FakeExports())  # type: ignore[arg-type]
    monkeypatch.setattr(
        Path,
        "open",
        lambda path, _mode: FailingWriter(path),
    )

    with pytest.raises(Win5ExportCliArtifactWriteError):
        adapter.execute(
            Win5SeasonExportCliRequest(season_id=7),
            output_directory=tmp_path,
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", [None, RuntimeError("export failed")])
def test_cli_entrypoint_always_disposes_database_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Exception | None,
) -> None:
    database_runtime = FakeDatabaseRuntime()
    exports = FakeExports(failure=failure)
    monkeypatch.setattr(
        win5_export_entrypoint.DatabaseRuntime,
        "from_url",
        classmethod(lambda cls, *_args, **_kwargs: database_runtime),
    )
    monkeypatch.setattr(
        win5_export_entrypoint,
        "compose_win5_season_exports",
        lambda _runtime: exports,
    )

    if failure is None:
        receipt = win5_export_entrypoint.run_win5_export(
            FakeSettings(),  # type: ignore[arg-type]
            Win5SeasonExportCliRequest(season_id=7),
            output_directory=tmp_path,
        )
        assert receipt.path.read_bytes() == exports.artifact.content
    else:
        with pytest.raises(RuntimeError, match="export failed"):
            win5_export_entrypoint.run_win5_export(
                FakeSettings(),  # type: ignore[arg-type]
                Win5SeasonExportCliRequest(season_id=7),
                output_directory=tmp_path,
            )

    assert database_runtime.dispose_calls == 1
