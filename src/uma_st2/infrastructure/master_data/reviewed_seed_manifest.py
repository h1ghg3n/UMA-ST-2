"""Strict reviewed JSON adapter for the initial canonical master-data seed."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from hashlib import sha256
from pathlib import Path

from uma_st2.application.master_data import (
    MasterDataSeedError,
    SeedMasterData,
    StadiumCourseSeed,
    StadiumSeed,
    UmamusumeSeed,
    UmamusumeVariantSeed,
)
from uma_st2.domain.match import MatchDirection, MatchSurface, StadiumCourseLayout

MASTER_DATA_SEED_MANIFEST_SCHEMA = "uma-st-2-master-data-seed/v1"
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024


class MasterDataSeedManifestError(ValueError):
    """The reviewed master-data manifest cannot be safely parsed."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MasterDataSeedManifestError("The master-data seed manifest contains duplicate JSON keys.")
        result[key] = value
    return result


def _object(value: object, *, field_name: str, expected_keys: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise MasterDataSeedManifestError(f"{field_name} has invalid fields.")
    return value


def _array(value: object, *, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise MasterDataSeedManifestError(f"{field_name} must be a JSON array.")
    return value


def _enum(enum_type: type[MatchSurface] | type[MatchDirection] | type[StadiumCourseLayout], value: object) -> object:
    if not isinstance(value, str):
        raise MasterDataSeedManifestError("Master-data enum values must be strings.")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise MasterDataSeedManifestError("The master-data seed manifest contains an invalid enum value.") from exc


def _release_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MasterDataSeedManifestError("release_date must be YYYY-MM-DD or null.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise MasterDataSeedManifestError("release_date must be YYYY-MM-DD or null.") from exc
    if parsed.isoformat() != value:
        raise MasterDataSeedManifestError("release_date must use canonical YYYY-MM-DD form.")
    return parsed


def load_reviewed_master_data_seed(path: Path) -> SeedMasterData:
    """Parse one bounded manifest before target database composition."""

    try:
        with path.open("rb") as manifest:
            content = manifest.read(_MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise MasterDataSeedManifestError("Could not read the master-data seed manifest.") from exc
    if not content or len(content) > _MAX_MANIFEST_BYTES:
        raise MasterDataSeedManifestError("The master-data seed manifest size is invalid.")
    try:
        payload = json.loads(content.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except MasterDataSeedManifestError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MasterDataSeedManifestError("The master-data seed manifest is not valid UTF-8 JSON.") from exc
    root = _object(
        payload,
        field_name="master-data seed manifest",
        expected_keys={
            "schema",
            "source_identifier",
            "umamusumes",
            "umamusume_variants",
            "stadiums",
            "stadium_courses",
        },
    )
    if root["schema"] != MASTER_DATA_SEED_MANIFEST_SCHEMA:
        raise MasterDataSeedManifestError("The master-data seed manifest schema is unsupported.")
    try:
        umamusumes = tuple(
            _umamusume(item, index=index)
            for index, item in enumerate(_array(root["umamusumes"], field_name="umamusumes"))
        )
        variants = tuple(
            _variant(item, index=index)
            for index, item in enumerate(_array(root["umamusume_variants"], field_name="umamusume_variants"))
        )
        stadiums = tuple(
            _stadium(item, index=index) for index, item in enumerate(_array(root["stadiums"], field_name="stadiums"))
        )
        courses = tuple(
            _course(item, index=index)
            for index, item in enumerate(_array(root["stadium_courses"], field_name="stadium_courses"))
        )
        return SeedMasterData(
            source_identifier=root["source_identifier"],  # type: ignore[arg-type]
            manifest_checksum=sha256(content).hexdigest(),
            umamusumes=umamusumes,
            umamusume_variants=variants,
            stadiums=stadiums,
            stadium_courses=courses,
        )
    except MasterDataSeedManifestError:
        raise
    except (MasterDataSeedError, TypeError) as exc:
        raise MasterDataSeedManifestError("The master-data seed manifest values are invalid.") from exc


def _umamusume(value: object, *, index: int) -> UmamusumeSeed:
    item = _object(
        value,
        field_name=f"umamusumes[{index}]",
        expected_keys={"external_id", "name_jp", "name_ko"},
    )
    return UmamusumeSeed(
        external_id=item["external_id"],  # type: ignore[arg-type]
        name_jp=item["name_jp"],  # type: ignore[arg-type]
        name_ko=item["name_ko"],  # type: ignore[arg-type]
    )


def _variant(value: object, *, index: int) -> UmamusumeVariantSeed:
    item = _object(
        value,
        field_name=f"umamusume_variants[{index}]",
        expected_keys={"external_id", "umamusume_external_id", "name_jp", "name_ko", "release_date"},
    )
    return UmamusumeVariantSeed(
        external_id=item["external_id"],  # type: ignore[arg-type]
        umamusume_external_id=item["umamusume_external_id"],  # type: ignore[arg-type]
        name_jp=item["name_jp"],  # type: ignore[arg-type]
        name_ko=item["name_ko"],  # type: ignore[arg-type]
        release_date=_release_date(item["release_date"]),
    )


def _stadium(value: object, *, index: int) -> StadiumSeed:
    item = _object(
        value,
        field_name=f"stadiums[{index}]",
        expected_keys={"external_id", "name_jp", "name_ko"},
    )
    return StadiumSeed(
        external_id=item["external_id"],  # type: ignore[arg-type]
        name_jp=item["name_jp"],  # type: ignore[arg-type]
        name_ko=item["name_ko"],  # type: ignore[arg-type]
    )


def _course(value: object, *, index: int) -> StadiumCourseSeed:
    item = _object(
        value,
        field_name=f"stadium_courses[{index}]",
        expected_keys={"external_id", "stadium_external_id", "surface", "distance", "direction", "layout"},
    )
    return StadiumCourseSeed(
        external_id=item["external_id"],  # type: ignore[arg-type]
        stadium_external_id=item["stadium_external_id"],  # type: ignore[arg-type]
        surface=_enum(MatchSurface, item["surface"]),  # type: ignore[arg-type]
        distance=item["distance"],  # type: ignore[arg-type]
        direction=_enum(MatchDirection, item["direction"]),  # type: ignore[arg-type]
        layout=_enum(StadiumCourseLayout, item["layout"]),  # type: ignore[arg-type]
    )
