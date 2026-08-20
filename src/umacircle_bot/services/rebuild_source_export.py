from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector

from umacircle_bot.domain.imports import normalize_import_source_identifier
from umacircle_bot.sheets.legacy_identity_point_workbook import calculate_file_sha256

REBUILD_SOURCE_FORMAT_VERSION = 2
DEFAULT_EXPECTED_SOURCE_REVISION = "20260802_0021"
SUPPORTED_SOURCE_REVISIONS = frozenset(
    {
        "20260801_0019",
        DEFAULT_EXPECTED_SOURCE_REVISION,
    }
)
_SOURCE_FILE_KINDS = frozenset({"room_match", "win5"})
_IMPORT_KIND_PREFIXES = {
    "room_match": ("legacy_room_",),
    "win5": ("legacy_win5_", "win5_"),
}
_OUTPUT_DIRECTORY_ATTEMPTS = 10
_SOURCE_COPY_CHUNK_SIZE = 1024 * 1024
_PUBLICATION_LOCK_NAME = ".rebuild-source-publish.lock"
EXPECTED_REBUILD_SOURCE_MARIADB_SCHEMA_SHA256 = {
    "20260801_0019": frozenset(
        {
            "2a9c075ed75d85121b914a83d20b577054f527c2ff013d3e0466b1abda1a4887",
            "344cd0e235af8ab61e2bed16545ccd19fed8fc31d005a4c0c831874f90d22c94",
        }
    ),
    "20260802_0021": frozenset(
        {
            "7f6fdc60dfc88eeca6bf92c0103b6c534dab1d346334e4936b74b07eae716664",
            "b82b701114a06856d53994137b2ec7569f4abd2c3fb36cf3dba16161c4b02bce",
        }
    ),
}


class RebuildSourceExportError(RuntimeError):
    """Raised when a source database or artifact cannot be exported safely."""


@dataclass(frozen=True)
class RebuildSourceFile:
    kind: str
    source_identifier: str
    path: Path


@dataclass(frozen=True)
class RebuildSourceSnapshot:
    source_revision: str
    generated_at: datetime
    source_manifest: dict[str, object]
    identity_workflow_overlay: dict[str, object]
    guild_settings_overlay: dict[str, object]
    operational_state_overlay: dict[str, object]
    reconciliation: dict[str, object]
    source_files: tuple[RebuildSourceFile, ...] = ()


@dataclass(frozen=True)
class RebuildSourceExportResult:
    output_directory: Path
    source_revision: str
    manifest_sha256: str
    artifact_sha256: dict[str, str]


@dataclass(frozen=True)
class _TableSpec:
    required_columns: tuple[str, ...]
    optional_columns: tuple[str, ...] = ()
    validation_only_columns: tuple[str, ...] = ()


_TABLE_SPECS = {
    "discord_accounts": _TableSpec(("id", "discord_user_id", "discord_nickname", "created_at", "updated_at")),
    "game_accounts": _TableSpec(
        (
            "id",
            "discord_account_id",
            "uma_pid",
            "nickname",
            "ingame_name",
            "identity_status",
            "created_at",
            "updated_at",
        )
    ),
    "identity_backfill_tasks": _TableSpec(
        (
            "id",
            "game_account_id",
            "source_import_record_id",
            "status",
            "conflict_detail_json",
            "resolved_by_discord_user_id",
            "resolved_at",
            "resolution_note",
            "created_at",
            "updated_at",
        )
    ),
    "player_link_requests": _TableSpec(
        (
            "id",
            "guild_id",
            "requester_discord_user_id",
            "discord_nickname_snapshot",
            "submitted_ingame_name",
            "submitted_uma_pid",
            "submitted_nickname_chunk",
            "submitted_participation_hint",
            "requester_note",
            "status",
            "active_request_marker",
            "selected_game_account_id",
            "reviewed_by_discord_user_id",
            "review_note",
            "idempotency_key",
            "request_fingerprint",
            "resolved_at",
            "created_at",
            "updated_at",
        )
    ),
    "player_link_operation_audits": _TableSpec(
        (
            "id",
            "player_link_request_id",
            "action",
            "actor_discord_user_id",
            "idempotency_key",
            "request_fingerprint",
            "before_json",
            "after_json",
            "reason",
            "created_at",
        )
    ),
    "guild_discord_settings": _TableSpec(
        (
            "id",
            "guild_id",
            "win5_announcement_channel_id",
            "room_match_announcement_channel_id",
            "log_channel_id",
            "default_timezone",
            "win5_announcements_enabled",
            "room_match_announcements_enabled",
            "revision_number",
            "created_at",
            "updated_at",
        ),
        ("operator_role_id", "bot_manager_role_id"),
    ),
    "guild_discord_settings_audits": _TableSpec(
        (
            "id",
            "guild_discord_settings_id",
            "guild_id",
            "action",
            "actor_discord_user_id",
            "idempotency_key",
            "request_fingerprint",
            "before_json",
            "after_json",
            "reason",
            "created_at",
        )
    ),
    "sheet_import_runs": _TableSpec(
        (
            "id",
            "import_kind",
            "source_type",
            "source_identifier",
            "source_checksum",
            "started_at",
            "finished_at",
            "status",
            "summary_json",
        )
    ),
    "sheet_import_records": _TableSpec(
        (
            "id",
            "import_run_id",
            "source_key",
            "row_fingerprint",
            "source_sheet_name",
            "source_row_number",
            "record_type",
            "status",
            "target_entity_type",
            "target_entity_id",
            "detail_json",
            "created_at",
        )
    ),
}

_IDENTITY_SOURCE_RECORD_SPEC = _TableSpec(
    (
        "id",
        "import_run_id",
        "source_key",
        "row_fingerprint",
        "source_sheet_name",
        "source_row_number",
        "record_type",
        "status",
        "target_entity_type",
        "target_entity_id",
        "detail_json",
        "created_at",
    )
)

_IDENTITY_TABLES = (
    "discord_accounts",
    "game_accounts",
    "identity_backfill_tasks",
    "player_link_requests",
    "player_link_operation_audits",
)
_SETTINGS_TABLES = ("guild_discord_settings", "guild_discord_settings_audits")
_SOURCE_TABLES = ("sheet_import_runs", "sheet_import_records")
_PRE_RATING_RULE_VERSION_TABLES = frozenset(
    {
        "alembic_version",
        "bets",
        "bet_judgements",
        "discord_accounts",
        "discord_publications",
        "discord_publication_audits",
        "export_runs",
        "game_accounts",
        "game_events",
        "guild_discord_settings",
        "guild_discord_settings_audits",
        "identity_backfill_tasks",
        "match_result_submissions",
        "player_link_operation_audits",
        "player_link_requests",
        "race_conditions",
        "race_entries",
        "race_operation_audits",
        "race_rating_contexts",
        "race_results",
        "races",
        "rating_events",
        "rating_rules",
        "report_snapshots",
        "room_match_result_provenance",
        "room_match_result_publications",
        "room_point_accounts",
        "room_point_transactions",
        "sheet_export_runs",
        "sheet_import_records",
        "sheet_import_runs",
        "win5_entries",
        "win5_judgements",
        "win5_operation_audits",
        "win5_picks",
        "win5_results",
        "win5_round_races",
        "win5_rounds",
        "win5_score_events",
        "win5_scores",
        "win5_seasons",
    }
)
_RATING_RULE_VERSION_REVISIONS = frozenset(
    {
        "20260731_0017",
        "20260731_0018",
        "20260801_0019",
        "20260802_0020",
        "20260802_0021",
    }
)
_ODDS_SNAPSHOT_REVISIONS = frozenset(
    {
        "20260731_0018",
        "20260801_0019",
        "20260802_0020",
        "20260802_0021",
    }
)
_OVERLAY_EXCLUDED_TABLES = frozenset({"alembic_version", *_IDENTITY_TABLES, *_SETTINGS_TABLES, *_SOURCE_TABLES})
_NON_SUFFIX_JSON_COLUMNS = frozenset({"numbers", "result_order", "winning_numbers"})


def _expected_source_tables(revision: str) -> frozenset[str]:
    tables = set(_PRE_RATING_RULE_VERSION_TABLES)
    if revision in _RATING_RULE_VERSION_REVISIONS:
        tables.add("rating_rule_versions")
    if revision in _ODDS_SNAPSHOT_REVISIONS:
        tables.update({"room_match_odds_snapshots", "room_match_odds_snapshot_entries"})
    return frozenset(tables)


def expected_rebuild_operational_tables(revision: str) -> frozenset[str]:
    """Return the exact operational overlay table set for a supported source revision."""
    if revision not in SUPPORTED_SOURCE_REVISIONS:
        raise RebuildSourceExportError("unsupported rebuild source revision")
    return _expected_source_tables(revision) - _OVERLAY_EXCLUDED_TABLES


def _validate_mariadb_schema_contract(
    connection: Connection,
    *,
    revision: str,
    expected_tables: frozenset[str],
) -> dict[str, object] | None:
    if connection.dialect.name not in {"mysql", "mariadb"}:
        return None
    expected_sha256 = EXPECTED_REBUILD_SOURCE_MARIADB_SCHEMA_SHA256[revision]
    table_fingerprints = _mariadb_schema_table_fingerprints(connection, expected_tables=expected_tables)
    observed_sha256 = _canonical_sha256(table_fingerprints)
    if observed_sha256 not in expected_sha256:
        raise RebuildSourceExportError(
            "source MariaDB schema fingerprint mismatch: "
            f"expected one of {','.join(sorted(expected_sha256))}, found {observed_sha256}; "
            f"table fingerprints {_canonical_json(table_fingerprints)}"
        )
    return {
        "validator": "normalized_show_create_table_v1",
        "aggregate_sha256": observed_sha256,
        "table_sha256": table_fingerprints,
    }


def _mariadb_schema_table_fingerprints(
    connection: Connection,
    *,
    expected_tables: frozenset[str],
) -> dict[str, str]:
    return {
        table_name: _canonical_sha256(_mariadb_show_create_table(connection, table_name))
        for table_name in sorted(expected_tables)
    }


def _mariadb_show_create_table(
    connection: Connection,
    table_name: str,
) -> str:
    quoted_table = _quote_identifier(connection, table_name)
    row = connection.exec_driver_sql(f"SHOW CREATE TABLE {quoted_table}").one()
    return _normalize_schema_text(row[1])


def _normalize_schema_text(value: object) -> str:
    without_sequence = re.sub(
        r"\bAUTO_INCREMENT=\d+\b",
        "AUTO_INCREMENT=<VALUE>",
        str(value),
        flags=re.IGNORECASE,
    )
    return " ".join(without_sequence.split())


def _validate_source_schema_stability(
    connection: Connection,
    *,
    revision: str,
    expected_tables: frozenset[str],
    initial_schema_contract: Mapping[str, object] | None,
) -> None:
    ending_revision = _load_source_revision(connection)
    ending_tables = frozenset(inspect(connection).get_table_names())
    if ending_revision != revision or ending_tables != expected_tables:
        raise RebuildSourceExportError("source schema changed while collecting the rebuild snapshot")
    ending_schema_contract = _validate_mariadb_schema_contract(
        connection,
        revision=revision,
        expected_tables=expected_tables,
    )
    if ending_schema_contract != initial_schema_contract:
        raise RebuildSourceExportError("source schema changed while collecting the rebuild snapshot")


def collect_rebuild_source_snapshot(
    connection: Connection,
    *,
    expected_revision: str = DEFAULT_EXPECTED_SOURCE_REVISION,
    source_files: Sequence[RebuildSourceFile] = (),
    generated_at: datetime | None = None,
) -> RebuildSourceSnapshot:
    """Collect a revision-aware source snapshot without importing current ORM models."""
    revision = _load_source_revision(connection)
    if revision not in SUPPORTED_SOURCE_REVISIONS:
        raise RebuildSourceExportError(f"unsupported source revision: {revision}")
    if revision != expected_revision:
        raise RebuildSourceExportError(f"source revision mismatch: expected {expected_revision}, found {revision}")

    inspector = inspect(connection)
    available_tables = set(inspector.get_table_names())
    expected_tables = _expected_source_tables(revision)
    missing_tables = sorted(expected_tables - available_tables)
    if missing_tables:
        raise RebuildSourceExportError("source schema is missing required tables: " + ", ".join(missing_tables))
    _validate_pre_persona_source_shape(inspector, available_tables=available_tables, revision=revision)
    unexpected_tables = sorted(available_tables - expected_tables)
    if unexpected_tables:
        raise RebuildSourceExportError("source schema has unexpected tables: " + ", ".join(unexpected_tables))
    schema_contract = _validate_mariadb_schema_contract(
        connection,
        revision=revision,
        expected_tables=expected_tables,
    )

    timestamp = _normalize_generated_at(generated_at)
    snapshots = {
        table_name: _snapshot_table(connection, inspector, table_name, _TABLE_SPECS[table_name])
        for table_name in _TABLE_SPECS
    }
    external_sources, required_external_sources = _inspect_and_validate_source_files(
        source_files,
        import_run_snapshot=snapshots["sheet_import_runs"],
    )

    source_manifest = {
        "format_version": REBUILD_SOURCE_FORMAT_VERSION,
        "source_revision": revision,
        "generated_at": _serialize_datetime(timestamp),
        "sensitive": True,
        "database_schema_contract": schema_contract,
        "external_files": external_sources,
        "required_external_sources": required_external_sources,
        "tables": {table: snapshots[table] for table in _SOURCE_TABLES},
    }
    identity_tables = {table: snapshots[table] for table in _IDENTITY_TABLES}
    identity_tables["identity_source_import_records"] = _snapshot_identity_source_records(connection, inspector)
    identity_overlay = {
        "format_version": REBUILD_SOURCE_FORMAT_VERSION,
        "source_revision": revision,
        "generated_at": _serialize_datetime(timestamp),
        "sensitive": True,
        "tables": identity_tables,
    }
    settings_overlay = {
        "format_version": REBUILD_SOURCE_FORMAT_VERSION,
        "source_revision": revision,
        "generated_at": _serialize_datetime(timestamp),
        "sensitive": True,
        "tables": {table: snapshots[table] for table in _SETTINGS_TABLES},
    }
    operational_snapshots = {
        table_name: _snapshot_entire_table(connection, inspector, table_name)
        for table_name in sorted(expected_tables - _OVERLAY_EXCLUDED_TABLES)
    }
    operational_overlay = {
        "format_version": REBUILD_SOURCE_FORMAT_VERSION,
        "source_revision": revision,
        "generated_at": _serialize_datetime(timestamp),
        "sensitive": True,
        "purpose": "preservation_and_target_reconciliation_only",
        "tables": operational_snapshots,
    }
    reconciliation = _collect_reconciliation(
        connection,
        revision=revision,
        available_tables=available_tables,
        snapshots={**snapshots, **operational_snapshots},
        timestamp=timestamp,
    )
    _validate_source_schema_stability(
        connection,
        revision=revision,
        expected_tables=expected_tables,
        initial_schema_contract=schema_contract,
    )
    return RebuildSourceSnapshot(
        source_revision=revision,
        generated_at=timestamp,
        source_manifest=source_manifest,
        identity_workflow_overlay=identity_overlay,
        guild_settings_overlay=settings_overlay,
        operational_state_overlay=operational_overlay,
        reconciliation=reconciliation,
        source_files=tuple(source_files),
    )


def public_rebuild_source_summary(snapshot: RebuildSourceSnapshot) -> dict[str, object]:
    """Return a PII-free summary suitable for stdout and release evidence."""
    source_tables = snapshot.source_manifest["tables"]
    identity_tables = snapshot.identity_workflow_overlay["tables"]
    settings_tables = snapshot.guild_settings_overlay["tables"]
    operational_tables = snapshot.operational_state_overlay["tables"]
    required_external_sources = snapshot.source_manifest["required_external_sources"]
    assert isinstance(source_tables, dict)
    assert isinstance(identity_tables, dict)
    assert isinstance(settings_tables, dict)
    assert isinstance(operational_tables, dict)
    assert isinstance(required_external_sources, (list, tuple))
    public_source_files = tuple(
        {
            "kind": item["kind"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
            "matched_import_run_count": item["matched_import_run_count"],
        }
        for item in snapshot.source_manifest["external_files"]
        if isinstance(item, Mapping)
    )
    return {
        "format_version": REBUILD_SOURCE_FORMAT_VERSION,
        "source_revision": snapshot.source_revision,
        "generated_at": _serialize_datetime(snapshot.generated_at),
        "source_files": public_source_files,
        "source_file_coverage": {
            "required_group_count": len(required_external_sources),
            "provided_group_count": len(public_source_files),
            "missing_group_count": len(required_external_sources) - len(public_source_files),
        },
        "import_run_count": _snapshot_row_count(source_tables["sheet_import_runs"]),
        "import_record_count": _snapshot_row_count(source_tables["sheet_import_records"]),
        "identity_row_counts": {table: _snapshot_row_count(value) for table, value in sorted(identity_tables.items())},
        "settings_row_counts": {table: _snapshot_row_count(value) for table, value in sorted(settings_tables.items())},
        "operational_row_counts": {
            table: _snapshot_row_count(value) for table, value in sorted(operational_tables.items())
        },
        "reconciliation": snapshot.reconciliation,
    }


def write_rebuild_source_bundle(
    snapshot: RebuildSourceSnapshot,
    *,
    output_parent: Path,
) -> RebuildSourceExportResult:
    _validate_bundle_source_coverage(snapshot)
    output_parent = Path(output_parent).expanduser().resolve()
    output_parent.mkdir(parents=True, exist_ok=True)
    if not output_parent.is_dir():
        raise RebuildSourceExportError("rebuild export parent is not a directory")

    staging_directory = _reserve_staging_directory(
        output_parent,
        generated_at=snapshot.generated_at,
    )
    json_artifacts = {
        "source_manifest.json": (snapshot.source_manifest, True),
        "identity_workflow_overlay.json": (snapshot.identity_workflow_overlay, True),
        "guild_settings_overlay.json": (snapshot.guild_settings_overlay, True),
        "operational_state_overlay.json": (snapshot.operational_state_overlay, True),
        "reconciliation.json": (snapshot.reconciliation, False),
    }
    created_files: list[Path] = []
    try:
        artifact_checksums: dict[str, str] = {}
        artifact_manifest: dict[str, dict[str, object]] = {}
        for name, (payload, sensitive) in json_artifacts.items():
            output_path = staging_directory / name
            _write_json_file(output_path, payload)
            created_files.append(output_path)
            artifact_checksums[name] = calculate_file_sha256(output_path)
            artifact_manifest[name] = {
                "media_type": "application/json",
                "sensitive": sensitive,
                "sha256": artifact_checksums[name],
            }

        external_files = snapshot.source_manifest.get("external_files")
        if not isinstance(external_files, (list, tuple)) or len(external_files) != len(snapshot.source_files):
            raise RebuildSourceExportError("source workbook snapshot is incomplete")
        for source_file, source_metadata in zip(snapshot.source_files, external_files, strict=True):
            if not isinstance(source_metadata, Mapping):
                raise RebuildSourceExportError("source workbook metadata is invalid")
            artifact_name = source_metadata.get("bundle_artifact")
            expected_checksum = source_metadata.get("sha256")
            expected_size = source_metadata.get("size_bytes")
            if (
                not isinstance(artifact_name, str)
                or Path(artifact_name).name != artifact_name
                or not isinstance(expected_checksum, str)
                or not isinstance(expected_size, int)
            ):
                raise RebuildSourceExportError("source workbook metadata is invalid")
            output_path = staging_directory / artifact_name
            artifact_checksums[artifact_name] = _copy_verified_source_file(
                source_file.path,
                output_path,
                expected_checksum=expected_checksum,
                expected_size=expected_size,
            )
            created_files.append(output_path)
            artifact_manifest[artifact_name] = {
                "media_type": _source_workbook_media_type(artifact_name),
                "sensitive": True,
                "sha256": artifact_checksums[artifact_name],
                "source_kind": source_metadata.get("kind"),
            }

        manifest = {
            "format_version": REBUILD_SOURCE_FORMAT_VERSION,
            "source_revision": snapshot.source_revision,
            "generated_at": _serialize_datetime(snapshot.generated_at),
            "artifacts": artifact_manifest,
        }
        manifest_path = staging_directory / "bundle_manifest.json"
        _write_json_file(manifest_path, manifest)
        created_files.append(manifest_path)
        manifest_checksum = calculate_file_sha256(manifest_path)
        _verify_staged_artifacts(
            staging_directory,
            artifact_checksums=artifact_checksums,
            manifest_path=manifest_path,
            manifest_checksum=manifest_checksum,
        )
        output_directory = _publish_staging_directory(
            staging_directory,
            output_parent=output_parent,
            generated_at=snapshot.generated_at,
        )
        return RebuildSourceExportResult(
            output_directory=output_directory,
            source_revision=snapshot.source_revision,
            manifest_sha256=manifest_checksum,
            artifact_sha256=artifact_checksums,
        )
    except Exception:
        for path in reversed(created_files):
            path.unlink(missing_ok=True)
        if staging_directory.exists():
            staging_directory.rmdir()
        raise


def _validate_bundle_source_coverage(snapshot: RebuildSourceSnapshot) -> None:
    external_files = snapshot.source_manifest.get("external_files")
    required_sources = snapshot.source_manifest.get("required_external_sources")
    if not isinstance(external_files, (list, tuple)) or not isinstance(required_sources, (list, tuple)):
        raise RebuildSourceExportError("source workbook coverage metadata is incomplete")
    external_keys = tuple(_source_metadata_key(item) for item in external_files)
    required_keys = tuple(_source_metadata_key(item) for item in required_sources)
    if len(external_keys) != len(set(external_keys)) or len(required_keys) != len(set(required_keys)):
        raise RebuildSourceExportError("source workbook coverage metadata contains duplicate groups")
    if set(external_keys) != set(required_keys):
        missing_count = len(set(required_keys) - set(external_keys))
        raise RebuildSourceExportError(
            f"bundle requires every physical source workbook; {missing_count} provenance group(s) are missing"
        )
    if len(external_files) != len(snapshot.source_files):
        raise RebuildSourceExportError("source workbook snapshot is incomplete")


def _source_metadata_key(value: object) -> tuple[str, str, str]:
    if not isinstance(value, Mapping):
        raise RebuildSourceExportError("source workbook coverage metadata is invalid")
    kind = value.get("kind")
    source_identifier = value.get("source_identifier")
    checksum = value.get("sha256")
    if not isinstance(kind, str) or not isinstance(source_identifier, str) or not isinstance(checksum, str):
        raise RebuildSourceExportError("source workbook coverage metadata is invalid")
    return kind, source_identifier, checksum


def _source_workbook_media_type(artifact_name: str) -> str:
    if Path(artifact_name).suffix.lower() == ".xlsm":
        return "application/vnd.ms-excel.sheet.macroEnabled.12"
    return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _verify_staged_artifacts(
    staging_directory: Path,
    *,
    artifact_checksums: Mapping[str, str],
    manifest_path: Path,
    manifest_checksum: str,
) -> None:
    expected_names = {*artifact_checksums, manifest_path.name}
    observed_names = {path.name for path in staging_directory.iterdir() if path.is_file()}
    if observed_names != expected_names:
        raise RebuildSourceExportError("staged rebuild artifact set is incomplete")
    for name, expected_checksum in artifact_checksums.items():
        if Path(name).name != name or calculate_file_sha256(staging_directory / name) != expected_checksum:
            raise RebuildSourceExportError("staged rebuild artifact checksum verification failed")
    if calculate_file_sha256(manifest_path) != manifest_checksum:
        raise RebuildSourceExportError("staged rebuild manifest checksum verification failed")


def _load_source_revision(connection: Connection) -> str:
    try:
        revisions = tuple(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    except Exception as exc:
        raise RebuildSourceExportError("source Alembic revision is unavailable") from exc
    if len(revisions) != 1 or not isinstance(revisions[0], str) or not revisions[0].strip():
        raise RebuildSourceExportError("source must have exactly one Alembic revision")
    return revisions[0].strip()


def _snapshot_table(
    connection: Connection,
    inspector: Inspector,
    table_name: str,
    spec: _TableSpec,
) -> dict[str, object]:
    actual_columns = {str(item["name"]) for item in inspector.get_columns(table_name)}
    required_for_validation = set(spec.required_columns) | set(spec.validation_only_columns)
    missing_columns = sorted(required_for_validation - actual_columns)
    if missing_columns:
        raise RebuildSourceExportError(
            f"source table {table_name} is missing required columns: {', '.join(missing_columns)}"
        )
    selected_columns = spec.required_columns + tuple(
        column for column in spec.optional_columns if column in actual_columns
    )
    quoted_table = _quote_identifier(connection, table_name)
    quoted_columns = ", ".join(_quote_identifier(connection, column) for column in selected_columns)
    rows = tuple(
        _normalize_row(row)
        for row in connection.execute(
            text(f"SELECT {quoted_columns} FROM {quoted_table} ORDER BY {_quote_identifier(connection, 'id')}")
        ).mappings()
    )
    return {
        "columns": selected_columns,
        "row_count": len(rows),
        "rows_sha256": _canonical_sha256(rows),
        "rows": rows,
    }


def _snapshot_entire_table(
    connection: Connection,
    inspector: Inspector,
    table_name: str,
) -> dict[str, object]:
    reflected_columns = tuple(inspector.get_columns(table_name))
    if not reflected_columns:
        raise RebuildSourceExportError(f"source table {table_name} has no columns")

    column_names = tuple(str(item["name"]) for item in reflected_columns)
    if "id" not in column_names:
        raise RebuildSourceExportError(f"source operational table {table_name} lacks an internal id")
    schema = tuple(
        {
            "name": str(item["name"]),
            "type": str(item["type"]),
            "nullable": bool(item.get("nullable", True)),
        }
        for item in reflected_columns
    )
    json_columns = frozenset(
        name for name in column_names if name.endswith("_json") or name in _NON_SUFFIX_JSON_COLUMNS
    )
    quoted_table = _quote_identifier(connection, table_name)
    quoted_columns = ", ".join(_quote_identifier(connection, column) for column in column_names)
    normalized_rows = (
        _normalize_row(row, json_columns=json_columns)
        for row in connection.execute(text(f"SELECT {quoted_columns} FROM {quoted_table}")).mappings()
    )
    rows = tuple(sorted(normalized_rows, key=_canonical_json))
    return {
        "columns": column_names,
        "schema": schema,
        "schema_sha256": _canonical_sha256(schema),
        "row_count": len(rows),
        "rows_sha256": _canonical_sha256(rows),
        "rows": rows,
    }


def _snapshot_identity_source_records(connection: Connection, inspector: Inspector) -> dict[str, object]:
    table_name = "sheet_import_records"
    spec = _IDENTITY_SOURCE_RECORD_SPEC
    actual_columns = {str(item["name"]) for item in inspector.get_columns(table_name)}
    missing_columns = sorted(set(spec.required_columns) - actual_columns)
    if missing_columns:
        raise RebuildSourceExportError(
            f"source table {table_name} is missing identity overlay columns: {', '.join(missing_columns)}"
        )
    table = _quote_identifier(connection, table_name)
    task_table = _quote_identifier(connection, "identity_backfill_tasks")
    columns = ", ".join(_quote_identifier(connection, column) for column in spec.required_columns)
    rows = tuple(
        _normalize_row(row)
        for row in connection.execute(
            text(
                f"SELECT {columns} FROM {table} WHERE {_quote_identifier(connection, 'id')} IN "
                f"(SELECT {_quote_identifier(connection, 'source_import_record_id')} FROM {task_table} "
                f"WHERE {_quote_identifier(connection, 'source_import_record_id')} IS NOT NULL) "
                f"ORDER BY {_quote_identifier(connection, 'id')}"
            )
        ).mappings()
    )
    return {
        "columns": spec.required_columns,
        "row_count": len(rows),
        "rows_sha256": _canonical_sha256(rows),
        "rows": rows,
    }


def _validate_pre_persona_source_shape(
    inspector: Inspector,
    *,
    available_tables: set[str],
    revision: str,
) -> None:
    forbidden_tables = {
        "account_registration_operation_audits",
        "account_registration_requests",
        "persona_link_operation_audits",
        "personas",
        "room_point_scale_state",
    }
    present_forbidden = sorted(forbidden_tables & available_tables)
    if present_forbidden:
        raise RebuildSourceExportError("source revision has post-0021 tables: " + ", ".join(present_forbidden))

    columns_by_table = {
        table_name: {str(item["name"]) for item in inspector.get_columns(table_name)}
        for table_name in (
            "discord_accounts",
            "game_accounts",
            "guild_discord_settings",
            "room_point_accounts",
            "room_point_transactions",
        )
    }
    for table_name in ("discord_accounts", "game_accounts", "room_point_accounts", "room_point_transactions"):
        if "persona_id" in columns_by_table[table_name]:
            raise RebuildSourceExportError(f"source table {table_name} unexpectedly contains persona_id")
    if "game_account_id" not in columns_by_table["room_point_accounts"]:
        raise RebuildSourceExportError("source wallet table is not GameAccount-owned")
    if "game_account_id" not in columns_by_table["room_point_transactions"]:
        raise RebuildSourceExportError("source ledger table lacks GameAccount provenance")

    role_columns = {"operator_role_id", "bot_manager_role_id"}
    actual_role_columns = role_columns & columns_by_table["guild_discord_settings"]
    if revision in {"20260802_0020", "20260802_0021"} and actual_role_columns != role_columns:
        raise RebuildSourceExportError("source guild settings lack the expected Role columns")
    if revision not in {"20260802_0020", "20260802_0021"} and actual_role_columns:
        raise RebuildSourceExportError("source guild settings contain Role columns before revision 0020")


def _collect_reconciliation(
    connection: Connection,
    *,
    revision: str,
    available_tables: set[str],
    snapshots: Mapping[str, Mapping[str, object]],
    timestamp: datetime,
) -> dict[str, object]:
    wallet_count = _scalar_int(connection, "SELECT COUNT(*) FROM room_point_accounts")
    wallet_balance = _scalar_int(connection, "SELECT COALESCE(SUM(balance), 0) FROM room_point_accounts")
    ledger_count = _scalar_int(connection, "SELECT COUNT(*) FROM room_point_transactions")
    ledger_amount = _scalar_int(connection, "SELECT COALESCE(SUM(amount), 0) FROM room_point_transactions")
    reconciliation = {
        "format_version": REBUILD_SOURCE_FORMAT_VERSION,
        "source_revision": revision,
        "generated_at": _serialize_datetime(timestamp),
        "identity": {
            "discord_account_count": _scalar_int(connection, "SELECT COUNT(*) FROM discord_accounts"),
            "game_account_count": _scalar_int(connection, "SELECT COUNT(*) FROM game_accounts"),
            "distinct_linked_discord_account_count": _scalar_int(
                connection,
                "SELECT COUNT(DISTINCT discord_account_id) FROM game_accounts",
            ),
            "non_null_pid_count": _scalar_int(connection, "SELECT COUNT(uma_pid) FROM game_accounts"),
            "distinct_non_null_pid_count": _scalar_int(
                connection,
                "SELECT COUNT(DISTINCT uma_pid) FROM game_accounts",
            ),
            "game_account_status_counts": _group_counts(connection, "game_accounts", "identity_status"),
            "backfill_status_counts": _group_counts(connection, "identity_backfill_tasks", "status"),
        },
        "room_points": {
            "wallet_count": wallet_count,
            "wallet_balance_sum": wallet_balance,
            "ledger_row_count": ledger_count,
            "ledger_amount_sum": ledger_amount,
            "wallet_ledger_equal": wallet_balance == ledger_amount,
            "source_type_totals": _room_point_source_type_totals(connection),
        },
        "room_match": {
            "race_count": _scalar_int(connection, "SELECT COUNT(*) FROM races"),
            "entry_count": _scalar_int(connection, "SELECT COUNT(*) FROM race_entries"),
            "result_count": _scalar_int(connection, "SELECT COUNT(*) FROM race_results"),
            "bet_count": _scalar_int(connection, "SELECT COUNT(*) FROM bets"),
            "judgement_count": _scalar_int(connection, "SELECT COUNT(*) FROM bet_judgements"),
            "race_status_counts": _group_counts(connection, "races", "status"),
            "bet_status_counts": _group_counts(connection, "bets", "status"),
            "judgement_status_counts": _group_counts(connection, "bet_judgements", "judgement_status"),
        },
        "workflow": {
            "player_link_status_counts": _group_counts(connection, "player_link_requests", "status"),
            "player_link_action_counts": _group_counts(
                connection,
                "player_link_operation_audits",
                "action",
            ),
            "guild_settings_count": _scalar_int(connection, "SELECT COUNT(*) FROM guild_discord_settings"),
            "guild_settings_audit_count": _scalar_int(
                connection,
                "SELECT COUNT(*) FROM guild_discord_settings_audits",
            ),
        },
        "not_yet_operational": {
            "win5_entry_count": _scalar_int(connection, "SELECT COUNT(*) FROM win5_entries"),
            "win5_score_count": _scalar_int(connection, "SELECT COUNT(*) FROM win5_scores"),
            "rating_rule_version_count": (
                _scalar_int(connection, "SELECT COUNT(*) FROM rating_rule_versions")
                if "rating_rule_versions" in available_tables
                else 0
            ),
            "rating_event_count": _scalar_int(connection, "SELECT COUNT(*) FROM rating_events"),
            "discord_publication_count": _scalar_int(
                connection,
                "SELECT COUNT(*) FROM discord_publications",
            ),
            "sheet_export_run_count": _scalar_int(connection, "SELECT COUNT(*) FROM sheet_export_runs"),
            "export_run_count": _scalar_int(connection, "SELECT COUNT(*) FROM export_runs"),
        },
        "table_fingerprints": {
            table_name: {
                "row_count": _snapshot_row_count(snapshot),
                "rows_sha256": snapshot["rows_sha256"],
            }
            for table_name, snapshot in sorted(snapshots.items())
        },
    }
    return reconciliation


def _group_counts(connection: Connection, table_name: str, column_name: str) -> tuple[dict[str, object], ...]:
    table = _quote_identifier(connection, table_name)
    column = _quote_identifier(connection, column_name)
    rows = connection.execute(
        text(f"SELECT {column} AS value, COUNT(*) AS row_count FROM {table} GROUP BY {column} ORDER BY {column}")
    ).mappings()
    return tuple({"value": _normalize_value(row["value"]), "row_count": int(row["row_count"])} for row in rows)


def _room_point_source_type_totals(connection: Connection) -> tuple[dict[str, object], ...]:
    rows = connection.execute(
        text(
            "SELECT source, type, COUNT(*) AS row_count, COALESCE(SUM(amount), 0) AS amount_sum "
            "FROM room_point_transactions GROUP BY source, type ORDER BY source, type"
        )
    ).mappings()
    totals: list[dict[str, object]] = []
    for row in rows:
        provenance = {
            "source": _normalize_value(row["source"]),
            "type": _normalize_value(row["type"]),
        }
        totals.append(
            {
                "provenance_group_sha256": _canonical_sha256(provenance),
                "row_count": int(row["row_count"]),
                "amount_sum": int(row["amount_sum"]),
            }
        )
    return tuple(totals)


def _scalar_int(connection: Connection, statement: str) -> int:
    value = connection.execute(text(statement)).scalar_one()
    return int(value or 0)


def _inspect_source_file(item: RebuildSourceFile) -> dict[str, object]:
    if item.kind not in _SOURCE_FILE_KINDS:
        raise RebuildSourceExportError(f"unsupported source file kind: {item.kind}")
    try:
        source_identifier = normalize_import_source_identifier(item.source_identifier)
    except ValueError as exc:
        raise RebuildSourceExportError("source workbook identifier is invalid") from exc
    path = Path(item.path).expanduser().resolve()
    if not path.is_file():
        raise RebuildSourceExportError("source workbook is unavailable")
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise RebuildSourceExportError("source workbook must be an XLSX or XLSM file")
    before = path.stat()
    checksum = calculate_file_sha256(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RebuildSourceExportError("source workbook changed while hashing")
    return {
        "kind": item.kind,
        "source_identifier": source_identifier,
        "file_name": path.name,
        "size_bytes": before.st_size,
        "modified_at": _serialize_datetime(datetime.fromtimestamp(before.st_mtime, tz=UTC)),
        "sha256": checksum,
    }


def _inspect_and_validate_source_files(
    source_files: Sequence[RebuildSourceFile],
    *,
    import_run_snapshot: Mapping[str, object],
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    external_sources = tuple(_inspect_source_file(item) for item in source_files)
    keys = tuple(_source_metadata_key(item) for item in external_sources)
    if len(keys) != len(set(keys)):
        raise RebuildSourceExportError("source workbook provenance group must be unique")

    required_sources = _completed_workbook_source_groups(import_run_snapshot)
    required_by_key = {_source_metadata_key(item): item for item in required_sources}
    validated: list[dict[str, object]] = []
    for index, source in enumerate(external_sources, start=1):
        key = _source_metadata_key(source)
        required_source = required_by_key.get(key)
        if required_source is None:
            same_identifier_exists = any(required_key[:2] == key[:2] for required_key in required_by_key)
            if same_identifier_exists:
                raise RebuildSourceExportError("source workbook checksum does not match database import provenance")
            raise RebuildSourceExportError(f"{source['kind']} workbook source identifier has no matching import run")
        validated.append(
            {
                **source,
                "bundle_artifact": _source_workbook_artifact_name(index, source),
                "matched_import_run_count": required_source["import_run_count"],
            }
        )
    return tuple(validated), required_sources


def _completed_workbook_source_groups(
    import_run_snapshot: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    rows = import_run_snapshot.get("rows")
    if not isinstance(rows, (list, tuple)):
        raise RebuildSourceExportError("invalid import run snapshot")
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("status") != "completed":
            continue
        source_type = row.get("source_type")
        import_kind = row.get("import_kind")
        if not isinstance(source_type, str) or source_type.lower() not in {"xlsx", "xlsm"}:
            continue
        if not isinstance(import_kind, str):
            continue
        kind = next(
            (candidate for candidate, prefixes in _IMPORT_KIND_PREFIXES.items() if import_kind.startswith(prefixes)),
            None,
        )
        if kind is None:
            continue
        source_identifier = row.get("source_identifier")
        source_checksum = row.get("source_checksum")
        if not isinstance(source_identifier, str) or not isinstance(source_checksum, str):
            raise RebuildSourceExportError("completed workbook import provenance is incomplete")
        try:
            normalized_identifier = normalize_import_source_identifier(source_identifier)
        except ValueError as exc:
            raise RebuildSourceExportError("completed workbook import provenance is invalid") from exc
        normalized_checksum = source_checksum.strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", normalized_checksum) is None:
            raise RebuildSourceExportError("completed workbook import checksum is invalid")
        key = kind, normalized_identifier, normalized_checksum
        group = grouped.setdefault(
            key,
            {
                "kind": kind,
                "source_identifier": normalized_identifier,
                "sha256": normalized_checksum,
                "import_run_count": 0,
                "import_kinds": set(),
                "source_types": set(),
            },
        )
        group["import_run_count"] = int(group["import_run_count"]) + 1
        assert isinstance(group["import_kinds"], set)
        assert isinstance(group["source_types"], set)
        group["import_kinds"].add(import_kind)
        group["source_types"].add(source_type.lower())

    return tuple(
        {
            **{key: value for key, value in group.items() if key not in {"import_kinds", "source_types"}},
            "import_kinds": tuple(sorted(group["import_kinds"])),
            "source_types": tuple(sorted(group["source_types"])),
        }
        for _, group in sorted(grouped.items())
    )


def completed_rebuild_workbook_source_groups(
    import_run_snapshot: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    """Derive required physical workbook groups from completed import provenance."""
    return _completed_workbook_source_groups(import_run_snapshot)


def _source_workbook_artifact_name(index: int, source: Mapping[str, object]) -> str:
    kind = str(source["kind"]).replace("_", "-")
    suffix = Path(str(source["file_name"])).suffix.lower()
    return f"source-workbook-{index:02d}-{kind}{suffix}"


def _normalize_generated_at(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise RebuildSourceExportError("generated_at must include a timezone")
    return timestamp.astimezone(UTC)


def _normalize_row(
    row: Mapping[str, object],
    *,
    json_columns: frozenset[str] = frozenset(),
) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in row.items():
        if key.endswith("_json") or key in json_columns:
            if isinstance(value, str):
                try:
                    parsed_value = json.loads(value)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise RebuildSourceExportError(f"source table contains invalid JSON in {key}") from exc
                _normalize_json_value(parsed_value)
                normalized[str(key)] = value
            else:
                normalized[str(key)] = _normalize_json_value(value)
            continue
        normalized[str(key)] = _normalize_value(value)
    return normalized


def _normalize_json_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise RebuildSourceExportError("source table contains a non-finite JSON number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise RebuildSourceExportError("source table contains a JSON object with a non-string key")
        return {key: _normalize_json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_json_value(item) for item in value)
    raise RebuildSourceExportError(f"unsupported source JSON value type: {type(value).__name__}")


def _normalize_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite():
            raise RebuildSourceExportError("source table contains a non-finite numeric value")
        return str(decimal_value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        return {str(key): _normalize_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_value(item) for item in value)
    raise RebuildSourceExportError(f"unsupported source value type: {type(value).__name__}")


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return value.isoformat(timespec="microseconds")


def _canonical_sha256(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _snapshot_row_count(value: object) -> int:
    if not isinstance(value, Mapping):
        raise RebuildSourceExportError("invalid table snapshot")
    row_count = value.get("row_count")
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
        raise RebuildSourceExportError("invalid table snapshot row count")
    return row_count


def _quote_identifier(connection: Connection, value: str) -> str:
    return connection.dialect.identifier_preparer.quote_identifier(value)


def _reserve_staging_directory(output_parent: Path, *, generated_at: datetime) -> Path:
    stem = f".rebuild-source-staging-{generated_at:%Y%m%dT%H%M%SZ}"
    for _attempt in range(_OUTPUT_DIRECTORY_ATTEMPTS):
        staging_directory = output_parent / f"{stem}-{uuid4().hex}"
        try:
            staging_directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        try:
            os.chmod(staging_directory, 0o700)
        except OSError:
            staging_directory.rmdir()
            raise
        return staging_directory
    raise RebuildSourceExportError("could not reserve a unique rebuild staging directory")


def _publish_staging_directory(
    staging_directory: Path,
    *,
    output_parent: Path,
    generated_at: datetime,
) -> Path:
    stem = f"rebuild-source-{generated_at:%Y%m%dT%H%M%SZ}"
    with _publication_lock(output_parent):
        for _attempt in range(_OUTPUT_DIRECTORY_ATTEMPTS):
            output_directory = output_parent / f"{stem}-{uuid4().hex}"
            if output_directory.exists():
                continue
            try:
                staging_directory.rename(output_directory)
            except FileExistsError:
                continue
            return output_directory
    raise RebuildSourceExportError("could not publish a unique rebuild export directory")


@contextmanager
def _publication_lock(output_parent: Path) -> Iterator[None]:
    lock_path = output_parent / _PUBLICATION_LOCK_NAME
    with lock_path.open("a+b") as lock_file:
        os.chmod(lock_path, 0o600)
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_json_file(output_path: Path, value: object) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            mode="x",
            encoding="utf-8",
            newline="\n",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(output_path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _copy_verified_source_file(
    source_path: Path,
    output_path: Path,
    *,
    expected_checksum: str,
    expected_size: int,
) -> str:
    source_path = Path(source_path).expanduser().resolve()
    temporary_path: Path | None = None
    output_created = False
    try:
        before = source_path.stat()
        digest = sha256()
        copied_size = 0
        with (
            source_path.open("rb") as source,
            NamedTemporaryFile(
                dir=output_path.parent,
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                mode="xb",
                delete=False,
            ) as temporary,
        ):
            temporary_path = Path(temporary.name)
            while chunk := source.read(_SOURCE_COPY_CHUNK_SIZE):
                temporary.write(chunk)
                digest.update(chunk)
                copied_size += len(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
        after = source_path.stat()
        copied_checksum = digest.hexdigest()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RebuildSourceExportError("source workbook changed while copying")
        if copied_size != expected_size or copied_checksum != expected_checksum:
            raise RebuildSourceExportError("source workbook changed after snapshot collection")
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(output_path)
        temporary_path = None
        output_created = True
        output_stat = output_path.stat()
        output_checksum = calculate_file_sha256(output_path)
        if output_stat.st_size != expected_size or output_checksum != expected_checksum:
            raise RebuildSourceExportError("copied source workbook checksum verification failed")
        return output_checksum
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if output_created:
            output_path.unlink(missing_ok=True)
        raise
