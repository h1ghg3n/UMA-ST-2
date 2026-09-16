"""Strict manifest and one-shot CLI tests for initial master data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uma_st2 import master_data_seed_entrypoint
from uma_st2.adapters.cli import (
    MASTER_DATA_SEED_RECEIPT_SCHEMA,
    MasterDataSeedCliRequest,
    parse_master_data_seed_cli_request,
)
from uma_st2.application.master_data import MasterDataSeedReceipt, SeedMasterData
from uma_st2.infrastructure.master_data import (
    MASTER_DATA_SEED_MANIFEST_SCHEMA,
    MasterDataSeedManifestError,
    load_reviewed_master_data_seed,
    reviewed_seed_manifest,
)


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": MASTER_DATA_SEED_MANIFEST_SCHEMA,
        "source_identifier": "reviewed-master-2026-09-04",
        "umamusumes": [
            {"external_id": 101, "name_jp": "Uma 101", "name_ko": "우마 101"},
        ],
        "umamusume_variants": [
            {
                "external_id": 102,
                "umamusume_external_id": 101,
                "name_jp": "Variant 102",
                "name_ko": None,
                "release_date": "2026-01-02",
            },
        ],
        "stadiums": [
            {"external_id": 201, "name_jp": "Stadium 201", "name_ko": "경기장 201"},
        ],
        "stadium_courses": [
            {
                "external_id": 301,
                "stadium_external_id": 201,
                "surface": "turf",
                "distance": 1600,
                "direction": "left",
                "layout": "standard",
            },
        ],
    }
    payload.update(changes)
    return payload


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_manifest_parser_builds_complete_command_and_raw_checksum(tmp_path: Path) -> None:
    path = tmp_path / "master-data.json"
    _write(path, _payload())

    command = load_reviewed_master_data_seed(path)

    assert command.source_identifier == "reviewed-master-2026-09-04"
    assert len(command.manifest_checksum) == len(command.master_data_checksum) == 64
    assert command.umamusume_variants[0].release_date.isoformat() == "2026-01-02"  # type: ignore[union-attr]
    assert command.stadium_courses[0].surface.value == "turf"
    assert parse_master_data_seed_cli_request(["--manifest", str(path)]) == MasterDataSeedCliRequest(path)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {**_payload(), "unexpected": True},
        {**_payload(), "schema": "uma-st-2-master-data-seed/v2"},
        {**_payload(), "umamusumes": []},
        {**_payload(), "stadium_courses": [{"external_id": 301}]},
        {
            **_payload(),
            "stadium_courses": [
                {
                    "external_id": 301,
                    "stadium_external_id": 201,
                    "surface": "grass",
                    "distance": 1600,
                    "direction": "left",
                    "layout": "standard",
                }
            ],
        },
        {
            **_payload(),
            "umamusume_variants": [
                {
                    "external_id": 102,
                    "umamusume_external_id": 101,
                    "name_jp": "Variant 102",
                    "name_ko": None,
                    "release_date": "2026-1-2",
                }
            ],
        },
    ],
)
def test_manifest_rejects_wrong_schema_shape_and_values(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "invalid.json"
    _write(path, payload)

    with pytest.raises(MasterDataSeedManifestError):
        load_reviewed_master_data_seed(path)


def test_manifest_rejects_duplicate_keys_and_oversized_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema":"uma-st-2-master-data-seed/v1","schema":"uma-st-2-master-data-seed/v1"}',
        encoding="utf-8",
    )
    with pytest.raises(MasterDataSeedManifestError, match="duplicate JSON keys"):
        load_reviewed_master_data_seed(duplicate)

    monkeypatch.setattr(reviewed_seed_manifest, "_MAX_MANIFEST_BYTES", 16)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * 17)
    with pytest.raises(MasterDataSeedManifestError, match="size is invalid"):
        load_reviewed_master_data_seed(oversized)


class FakeSettings:
    database_url_value = "mysql+pymysql://user:password@db/app?charset=utf8mb4"


class FakeDatabaseRuntime:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


class FakeCommands:
    def __init__(self, receipt: MasterDataSeedReceipt, *, failure: Exception | None = None) -> None:
        self.receipt = receipt
        self.failure = failure
        self.commands: list[SeedMasterData] = []

    def seed(self, command: SeedMasterData) -> MasterDataSeedReceipt:
        self.commands.append(command)
        if self.failure is not None:
            raise self.failure
        return self.receipt


def _receipt() -> MasterDataSeedReceipt:
    return MasterDataSeedReceipt(
        source_identifier="reviewed-master-2026-09-04",
        manifest_checksum="a" * 64,
        master_data_checksum="b" * 64,
        umamusume_count=1,
        umamusume_variant_count=1,
        stadium_count=1,
        stadium_course_count=1,
        created=True,
    )


@pytest.mark.parametrize("failure", [None, RuntimeError("seed failed")])
def test_entrypoint_parses_before_engine_and_always_disposes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Exception | None,
) -> None:
    path = tmp_path / "master-data.json"
    _write(path, _payload())
    runtime = FakeDatabaseRuntime()
    commands = FakeCommands(_receipt(), failure=failure)
    monkeypatch.setattr(
        master_data_seed_entrypoint.DatabaseRuntime,
        "from_url",
        classmethod(lambda cls, *_args, **_kwargs: runtime),
    )
    monkeypatch.setattr(master_data_seed_entrypoint, "compose_master_data_seed_commands", lambda _runtime: commands)

    if failure is None:
        receipt = master_data_seed_entrypoint.run_master_data_seed(
            FakeSettings(),  # type: ignore[arg-type]
            MasterDataSeedCliRequest(path),
        )
        assert receipt.created is True
    else:
        with pytest.raises(RuntimeError, match="seed failed"):
            master_data_seed_entrypoint.run_master_data_seed(
                FakeSettings(),  # type: ignore[arg-type]
                MasterDataSeedCliRequest(path),
            )
    assert len(commands.commands) == 1
    assert runtime.dispose_calls == 1


def test_invalid_manifest_never_creates_database_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text("not JSON", encoding="utf-8")
    created = False

    def create_runtime(*_args: object, **_kwargs: object) -> object:
        nonlocal created
        created = True
        raise AssertionError("Database runtime must not be created.")

    monkeypatch.setattr(
        master_data_seed_entrypoint.DatabaseRuntime,
        "from_url",
        classmethod(lambda cls, *args, **kwargs: create_runtime(*args, **kwargs)),
    )

    with pytest.raises(MasterDataSeedManifestError):
        master_data_seed_entrypoint.run_master_data_seed(
            FakeSettings(),  # type: ignore[arg-type]
            MasterDataSeedCliRequest(path),
        )

    assert created is False


def test_success_output_is_one_safe_deterministic_json_object(capsys: pytest.CaptureFixture[str]) -> None:
    master_data_seed_entrypoint._print_success(_receipt())

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema": MASTER_DATA_SEED_RECEIPT_SCHEMA,
        "status": "created",
        "manifest_sha256": "a" * 64,
        "master_data_sha256": "b" * 64,
        "counts": {"stadium_courses": 1, "stadiums": 1, "umamusume_variants": 1, "umamusumes": 1},
    }
