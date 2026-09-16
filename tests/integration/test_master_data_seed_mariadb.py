"""MariaDB evidence for atomic reviewed initial master-data seeding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.engine import Engine

from uma_st2.adapters.cli import MasterDataSeedCliRequest
from uma_st2.application.master_data import MasterDataSeedConflictError
from uma_st2.config import DatabaseSettings
from uma_st2.infrastructure.database.orm import (
    StadiumCourseORM,
    StadiumORM,
    UmamusumeORM,
    UmamusumeVariantORM,
)
from uma_st2.infrastructure.master_data import MASTER_DATA_SEED_MANIFEST_SCHEMA
from uma_st2.master_data_seed_entrypoint import run_master_data_seed

pytestmark = pytest.mark.integration


def _payload(*, umamusume_name_ko: str = "우마 101") -> dict[str, object]:
    return {
        "schema": MASTER_DATA_SEED_MANIFEST_SCHEMA,
        "source_identifier": "integration-reviewed-master",
        "umamusumes": [
            {"external_id": 101, "name_jp": "Uma 101", "name_ko": umamusume_name_ko},
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


def _request(path: Path, payload: dict[str, object]) -> MasterDataSeedCliRequest:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return MasterDataSeedCliRequest(path)


def _settings(engine: Engine) -> DatabaseSettings:
    return DatabaseSettings(DATABASE_URL=engine.url.render_as_string(hide_password=False))


def _counts(engine: Engine) -> tuple[int, int, int, int]:
    with engine.connect() as connection:
        return (
            connection.scalar(select(func.count()).select_from(UmamusumeORM)) or 0,
            connection.scalar(select(func.count()).select_from(UmamusumeVariantORM)) or 0,
            connection.scalar(select(func.count()).select_from(StadiumORM)) or 0,
            connection.scalar(select(func.count()).select_from(StadiumCourseORM)) or 0,
        )


def _clear_master_data(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(delete(StadiumCourseORM))
        connection.execute(delete(UmamusumeVariantORM))
        connection.execute(delete(StadiumORM))
        connection.execute(delete(UmamusumeORM))


def test_child_flush_failure_rolls_back_all_four_master_tables(
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    assert _counts(migrated_engine) == (0, 0, 0, 0)

    def fail_course_insert(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("forced course insert failure")

    event.listen(StadiumCourseORM, "before_insert", fail_course_insert)
    try:
        with pytest.raises(RuntimeError, match="forced course insert failure"):
            run_master_data_seed(
                _settings(migrated_engine),
                _request(tmp_path / "rollback.json", _payload()),
            )
    finally:
        event.remove(StadiumCourseORM, "before_insert", fail_course_insert)

    assert _counts(migrated_engine) == (0, 0, 0, 0)


def test_one_shot_seed_creates_complete_graph_exact_retries_and_rejects_drift(
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    assert _counts(migrated_engine) == (0, 0, 0, 0)
    settings = _settings(migrated_engine)
    request = _request(tmp_path / "master-data.json", _payload())

    try:
        created = run_master_data_seed(settings, request)
        retried = run_master_data_seed(settings, request)

        assert created.created is True
        assert retried.created is False
        assert created.master_data_checksum == retried.master_data_checksum
        assert _counts(migrated_engine) == (1, 1, 1, 1)
        with migrated_engine.connect() as connection:
            variant = connection.execute(
                select(UmamusumeVariantORM.external_id, UmamusumeORM.external_id).join(
                    UmamusumeORM, UmamusumeORM.id == UmamusumeVariantORM.umamusume_id
                )
            ).one()
            course = connection.execute(
                select(StadiumCourseORM.external_id, StadiumORM.external_id).join(
                    StadiumORM, StadiumORM.id == StadiumCourseORM.stadium_id
                )
            ).one()
            assert variant == (102, 101)
            assert course == (301, 201)

        changed = _request(tmp_path / "changed.json", _payload(umamusume_name_ko="변경된 이름"))
        with pytest.raises(MasterDataSeedConflictError, match="partial or differs"):
            run_master_data_seed(settings, changed)
        assert _counts(migrated_engine) == (1, 1, 1, 1)
    finally:
        _clear_master_data(migrated_engine)
