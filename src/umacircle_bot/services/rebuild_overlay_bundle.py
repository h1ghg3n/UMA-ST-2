from __future__ import annotations

import errno
import json
import os
import stat
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import TracebackType

from umacircle_bot.services.rebuild_source_export import (
    DEFAULT_EXPECTED_SOURCE_REVISION,
    EXPECTED_REBUILD_SOURCE_MARIADB_SCHEMA_SHA256,
    REBUILD_SOURCE_FORMAT_VERSION,
    RebuildSourceExportError,
    completed_rebuild_workbook_source_groups,
    expected_rebuild_operational_tables,
)

_CORE_JSON_ARTIFACTS = frozenset(
    {
        "source_manifest.json",
        "identity_workflow_overlay.json",
        "guild_settings_overlay.json",
        "operational_state_overlay.json",
        "reconciliation.json",
    }
)
_IDENTITY_TABLES = frozenset(
    {
        "discord_accounts",
        "game_accounts",
        "identity_backfill_tasks",
        "player_link_requests",
        "player_link_operation_audits",
        "identity_source_import_records",
    }
)
_SETTINGS_TABLES = frozenset({"guild_discord_settings", "guild_discord_settings_audits"})
_SOURCE_TABLES = frozenset({"sheet_import_runs", "sheet_import_records"})
_READ_CHUNK_SIZE = 1024 * 1024


class RebuildOverlayImportError(RuntimeError):
    """Raised when a rebuild bundle or target cannot be trusted for a dry-run."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class LoadedRebuildOverlayBundle:
    manifest_sha256: str
    source_revision: str
    generated_at: str
    source_manifest: dict[str, object]
    identity_overlay: dict[str, object]
    settings_overlay: dict[str, object]
    operational_overlay: dict[str, object]
    reconciliation: dict[str, object]


@dataclass(frozen=True, slots=True)
class _ReadArtifact:
    payload: bytes
    sha256: str
    size: int


class _ProtectedBundleDirectory(AbstractContextManager["_ProtectedBundleDirectory"]):
    def __init__(self, path: Path, *, allow_unverified_windows_acl: bool) -> None:
        self._raw_path = Path(path).expanduser()
        self._allow_unverified_windows_acl = allow_unverified_windows_acl
        self._directory_fd: int | None = None
        self._resolved_path: Path | None = None
        self._directory_stat: os.stat_result | None = None

    def __enter__(self) -> _ProtectedBundleDirectory:
        if os.name == "nt" and not self._allow_unverified_windows_acl:
            raise _error(
                "windows_acl_contract_unsupported",
                "protected bundle ACL verification is unavailable on Windows",
            )
        try:
            initial = self._raw_path.lstat()
        except OSError as exc:
            raise _error("bundle_unavailable", "bundle directory is unavailable") from exc
        if stat.S_ISLNK(initial.st_mode):
            raise _error("bundle_symlink_rejected", "bundle directory must not be a symbolic link")
        if not stat.S_ISDIR(initial.st_mode):
            raise _error("bundle_not_directory", "bundle path is not a directory")

        if os.name != "nt":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                directory_fd = os.open(self._raw_path, flags)
            except OSError as exc:
                raise _error("bundle_unavailable", "bundle directory cannot be opened safely") from exc
            opened = os.fstat(directory_fd)
            if not stat.S_ISDIR(opened.st_mode) or _file_identity(initial) != _file_identity(opened):
                os.close(directory_fd)
                raise _error("bundle_directory_changed", "bundle directory changed while it was opened")
            _require_posix_mode(opened, expected=0o700)
            self._directory_fd = directory_fd
            self._directory_stat = opened
            return self

        try:
            resolved = self._raw_path.resolve(strict=True)
            opened = resolved.stat()
        except OSError as exc:
            raise _error("bundle_unavailable", "bundle directory cannot be opened safely") from exc
        if not stat.S_ISDIR(opened.st_mode) or _file_identity(initial) != _file_identity(opened):
            raise _error("bundle_directory_changed", "bundle directory changed while it was opened")
        self._resolved_path = resolved
        self._directory_stat = opened
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None

    def names(self) -> set[str]:
        try:
            if self._directory_fd is not None:
                return set(os.listdir(self._directory_fd))
            assert self._resolved_path is not None
            return set(os.listdir(self._resolved_path))
        except OSError as exc:
            raise _error("bundle_unavailable", "bundle directory cannot be listed safely") from exc

    def read_artifact(self, name: str) -> _ReadArtifact:
        if Path(name).name != name or name in {"", ".", ".."}:
            raise _error("invalid_artifact_name", "bundle manifest contains an unsafe artifact name")
        if self._directory_fd is not None:
            return self._read_posix_artifact(name)
        return self._read_windows_artifact(name)

    def _read_posix_artifact(self, name: str) -> _ReadArtifact:
        assert self._directory_fd is not None
        try:
            initial = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise _error("bundle_artifact_unavailable", "bundle artifact cannot be opened safely") from exc
        if stat.S_ISLNK(initial.st_mode):
            raise _error("bundle_symlink_rejected", "bundle artifacts must not be symbolic links")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_fd = os.open(name, flags, dir_fd=self._directory_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _error("bundle_symlink_rejected", "bundle artifacts must not be symbolic links") from exc
            raise _error("bundle_artifact_unavailable", "bundle artifact cannot be opened safely") from exc
        try:
            opened = os.fstat(file_fd)
            if _file_identity(initial) != _file_identity(opened):
                raise _error("bundle_artifact_changed", "bundle artifact changed while it was opened")
            self._validate_open_file(opened)
            return _read_open_file(file_fd, opened)
        finally:
            os.close(file_fd)

    def _read_windows_artifact(self, name: str) -> _ReadArtifact:
        assert self._resolved_path is not None
        path = self._resolved_path / name
        try:
            initial = path.lstat()
        except OSError as exc:
            raise _error("bundle_artifact_unavailable", "bundle artifact cannot be opened safely") from exc
        if stat.S_ISLNK(initial.st_mode):
            raise _error("bundle_symlink_rejected", "bundle artifacts must not be symbolic links")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            file_fd = os.open(path, flags)
        except OSError as exc:
            raise _error("bundle_artifact_unavailable", "bundle artifact cannot be opened safely") from exc
        try:
            opened = os.fstat(file_fd)
            if _file_identity(initial) != _file_identity(opened):
                raise _error("bundle_artifact_changed", "bundle artifact changed while it was opened")
            self._validate_open_file(opened)
            return _read_open_file(file_fd, opened)
        finally:
            os.close(file_fd)

    def _validate_open_file(self, opened: os.stat_result) -> None:
        if not stat.S_ISREG(opened.st_mode):
            raise _error("bundle_artifact_not_regular", "bundle artifact must be a regular file")
        if os.name != "nt":
            _require_posix_mode(opened, expected=0o600)
            assert self._directory_stat is not None
            if opened.st_uid != self._directory_stat.st_uid:
                raise _error("bundle_owner_mismatch", "bundle directory and artifacts must have one owner")


def load_rebuild_overlay_bundle(
    bundle_directory: Path,
    *,
    expected_manifest_sha256: str,
    expected_source_revision: str = DEFAULT_EXPECTED_SOURCE_REVISION,
    allow_unverified_windows_acl: bool = False,
    allow_synthetic_test_bundle: bool = False,
) -> LoadedRebuildOverlayBundle:
    expected_hash = _normalize_sha256(expected_manifest_sha256, code="invalid_expected_manifest_sha256")
    with _ProtectedBundleDirectory(
        bundle_directory,
        allow_unverified_windows_acl=allow_unverified_windows_acl,
    ) as directory:
        initial_names = directory.names()
        manifest_artifact = directory.read_artifact("bundle_manifest.json")
        if manifest_artifact.sha256 != expected_hash:
            raise _error("manifest_anchor_mismatch", "bundle manifest does not match the externally anchored hash")
        manifest = _load_json_object(manifest_artifact.payload)
        source_revision, generated_at = _validate_common_header(
            manifest,
            expected_source_revision=expected_source_revision,
        )
        artifact_manifest = _mapping(manifest.get("artifacts"), code="invalid_artifact_manifest")
        if not _CORE_JSON_ARTIFACTS.issubset(artifact_manifest):
            raise _error("missing_core_artifact", "bundle manifest is missing a required core artifact")

        artifact_names = set(artifact_manifest)
        for name in artifact_names:
            if not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."}:
                raise _error("invalid_artifact_name", "bundle manifest contains an unsafe artifact name")
        expected_names = {"bundle_manifest.json", *artifact_names}
        if initial_names != expected_names:
            raise _error("bundle_artifact_set_mismatch", "bundle contains missing or undeclared artifacts")

        payloads: dict[str, dict[str, object]] = {}
        artifact_sizes: dict[str, int] = {}
        for name, metadata_value in artifact_manifest.items():
            metadata = _mapping(metadata_value, code="invalid_artifact_manifest")
            expected_artifact_hash = _normalize_sha256(metadata.get("sha256"), code="invalid_artifact_manifest")
            artifact = directory.read_artifact(str(name))
            if artifact.sha256 != expected_artifact_hash:
                raise _error("bundle_artifact_checksum_mismatch", "bundle artifact checksum verification failed")
            artifact_sizes[str(name)] = artifact.size
            if name in _CORE_JSON_ARTIFACTS:
                if metadata.get("media_type") != "application/json":
                    raise _error("invalid_artifact_media_type", "core bundle artifact must use application/json")
                payloads[str(name)] = _load_json_object(artifact.payload)
        if directory.names() != expected_names:
            raise _error("bundle_changed_during_read", "bundle contents changed while they were read")

    for payload in payloads.values():
        payload_revision, payload_generated_at = _validate_common_header(
            payload,
            expected_source_revision=expected_source_revision,
        )
        if payload_revision != source_revision or payload_generated_at != generated_at:
            raise _error("bundle_header_mismatch", "bundle artifact headers are inconsistent")

    source_manifest = payloads["source_manifest.json"]
    identity_overlay = payloads["identity_workflow_overlay.json"]
    settings_overlay = payloads["guild_settings_overlay.json"]
    operational_overlay = payloads["operational_state_overlay.json"]
    reconciliation = payloads["reconciliation.json"]
    if operational_overlay.get("purpose") != "preservation_and_target_reconciliation_only":
        raise _error("invalid_operational_overlay_purpose", "operational overlay is not reconciliation-only")

    _validate_source_schema_contract(
        source_manifest,
        source_revision=source_revision,
        allow_synthetic_test_bundle=allow_synthetic_test_bundle,
    )
    _validate_source_file_coverage(
        source_manifest,
        artifact_manifest,
        artifact_sizes=artifact_sizes,
    )
    source_tables = _validate_table_set(source_manifest, expected=_SOURCE_TABLES, exact=True)
    identity_tables = _validate_table_set(identity_overlay, expected=_IDENTITY_TABLES, exact=True)
    settings_tables = _validate_table_set(settings_overlay, expected=_SETTINGS_TABLES, exact=True)
    operational_tables = _validate_table_set(
        operational_overlay,
        expected=expected_rebuild_operational_tables(source_revision),
        exact=True,
        require_schema=True,
    )
    _validate_identity_source_subset(source_tables, identity_tables)
    _validate_table_fingerprints(
        reconciliation,
        source_tables=source_tables,
        identity_tables=identity_tables,
        settings_tables=settings_tables,
        operational_tables=operational_tables,
    )
    return LoadedRebuildOverlayBundle(
        manifest_sha256=manifest_artifact.sha256,
        source_revision=source_revision,
        generated_at=generated_at,
        source_manifest=source_manifest,
        identity_overlay=identity_overlay,
        settings_overlay=settings_overlay,
        operational_overlay=operational_overlay,
        reconciliation=reconciliation,
    )


def _read_open_file(file_fd: int, initial: os.stat_result) -> _ReadArtifact:
    chunks: list[bytes] = []
    digest = sha256()
    while True:
        chunk = os.read(file_fd, _READ_CHUNK_SIZE)
        if not chunk:
            break
        chunks.append(chunk)
        digest.update(chunk)
    ending = os.fstat(file_fd)
    if (
        _file_identity(initial) != _file_identity(ending)
        or initial.st_size != ending.st_size
        or initial.st_mtime_ns != ending.st_mtime_ns
        or initial.st_ctime_ns != ending.st_ctime_ns
    ):
        raise _error("bundle_artifact_changed", "bundle artifact changed while it was read")
    payload = b"".join(chunks)
    if len(payload) != initial.st_size:
        raise _error("bundle_artifact_changed", "bundle artifact size changed while it was read")
    return _ReadArtifact(payload=payload, sha256=digest.hexdigest(), size=len(payload))


def _file_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _require_posix_mode(value: os.stat_result, *, expected: int) -> None:
    if stat.S_IMODE(value.st_mode) != expected:
        raise _error(
            "bundle_permission_mismatch",
            "bundle permissions do not match the protected artifact contract",
        )


def _load_json_object(payload: bytes) -> dict[str, object]:
    def reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _error("duplicate_json_key", "bundle JSON contains a duplicate object key")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except RebuildOverlayImportError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _error("invalid_bundle_json", "bundle JSON cannot be decoded") from exc
    return dict(_mapping(value, code="invalid_bundle_json"))


def _validate_common_header(
    payload: Mapping[str, object],
    *,
    expected_source_revision: str,
) -> tuple[str, str]:
    if payload.get("format_version") != REBUILD_SOURCE_FORMAT_VERSION:
        raise _error("unsupported_bundle_format", "bundle format version is unsupported")
    revision = payload.get("source_revision")
    generated_at = payload.get("generated_at")
    if revision != expected_source_revision:
        raise _error("source_revision_mismatch", "bundle source revision does not match the required revision")
    if not isinstance(generated_at, str) or not generated_at.endswith("Z"):
        raise _error("invalid_bundle_timestamp", "bundle generated_at must be an explicit UTC timestamp")
    return str(revision), generated_at


def _validate_source_schema_contract(
    source_manifest: Mapping[str, object],
    *,
    source_revision: str,
    allow_synthetic_test_bundle: bool,
) -> None:
    schema_contract = _mapping(
        source_manifest.get("database_schema_contract"),
        code="invalid_source_schema_contract",
    )
    validator = schema_contract.get("validator")
    aggregate_sha256 = _normalize_sha256(
        schema_contract.get("aggregate_sha256"),
        code="invalid_source_schema_contract",
    )
    table_schema_hashes = _mapping(
        schema_contract.get("table_sha256"),
        code="invalid_source_schema_contract",
    )
    if not table_schema_hashes:
        raise _error("invalid_source_schema_contract", "source schema contract has no table fingerprints")
    normalized_table_hashes: dict[str, str] = {}
    for table_name, checksum in table_schema_hashes.items():
        if not isinstance(table_name, str) or not table_name:
            raise _error("invalid_source_schema_contract", "source schema contract has an invalid table name")
        normalized_table_hashes[table_name] = _normalize_sha256(
            checksum,
            code="invalid_source_schema_contract",
        )
    if validator == "synthetic_test_only" and allow_synthetic_test_bundle:
        return
    if validator != "normalized_show_create_table_v1":
        raise _error("invalid_source_schema_contract", "source schema validator is unsupported")
    if _canonical_sha256(normalized_table_hashes) != aggregate_sha256:
        raise _error("source_schema_aggregate_mismatch", "source schema aggregate is internally inconsistent")
    approved = EXPECTED_REBUILD_SOURCE_MARIADB_SCHEMA_SHA256.get(source_revision, frozenset())
    if aggregate_sha256 not in approved:
        raise _error("source_schema_fingerprint_mismatch", "source MariaDB DDL profile is not approved")


def _validate_source_file_coverage(
    source_manifest: Mapping[str, object],
    artifact_manifest: Mapping[str, object],
    *,
    artifact_sizes: Mapping[str, int],
) -> None:
    external_files = _sequence(source_manifest.get("external_files"), code="invalid_source_file_coverage")
    required_sources = _sequence(source_manifest.get("required_external_sources"), code="invalid_source_file_coverage")
    source_tables = _mapping(source_manifest.get("tables"), code="invalid_source_file_coverage")
    import_run_snapshot = _mapping(source_tables.get("sheet_import_runs"), code="invalid_source_file_coverage")
    try:
        derived_required_sources = completed_rebuild_workbook_source_groups(import_run_snapshot)
    except RebuildSourceExportError as exc:
        raise _error(
            "invalid_completed_import_provenance",
            "completed workbook import provenance is invalid",
        ) from exc
    if _canonical_json(required_sources) != _canonical_json(derived_required_sources):
        raise _error(
            "required_source_provenance_mismatch",
            "required physical source coverage does not match completed import provenance",
        )
    required_by_key = {
        _source_file_key(item): item
        for item in (_mapping(value, code="invalid_source_file_coverage") for value in derived_required_sources)
    }
    external_keys: list[tuple[str, str, str]] = []
    required_keys: list[tuple[str, str, str]] = []
    workbook_artifacts: set[str] = set()
    for item_value in external_files:
        item = _mapping(item_value, code="invalid_source_file_coverage")
        key = _source_file_key(item)
        artifact_name = item.get("bundle_artifact")
        size_bytes = item.get("size_bytes")
        if (
            not isinstance(artifact_name, str)
            or Path(artifact_name).name != artifact_name
            or artifact_name in _CORE_JSON_ARTIFACTS
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
        ):
            raise _error("invalid_source_file_coverage", "source file coverage metadata is invalid")
        metadata = _mapping(artifact_manifest.get(artifact_name), code="invalid_source_file_coverage")
        if metadata.get("sha256") != key[2] or metadata.get("source_kind") != key[0]:
            raise _error("source_file_manifest_mismatch", "source workbook metadata does not match its artifact")
        if artifact_sizes.get(artifact_name) != size_bytes:
            raise _error("source_file_size_mismatch", "source workbook size does not match its manifest")
        matched_import_run_count = item.get("matched_import_run_count")
        required_source = required_by_key.get(key)
        if (
            required_source is None
            or not isinstance(matched_import_run_count, int)
            or isinstance(matched_import_run_count, bool)
            or matched_import_run_count != required_source.get("import_run_count")
        ):
            raise _error(
                "source_file_import_count_mismatch",
                "source workbook import count does not match completed provenance",
            )
        external_keys.append(key)
        workbook_artifacts.add(artifact_name)
    for item_value in required_sources:
        required_keys.append(_source_file_key(_mapping(item_value, code="invalid_source_file_coverage")))
    if len(external_keys) != len(set(external_keys)) or len(required_keys) != len(set(required_keys)):
        raise _error("duplicate_source_file_group", "source workbook provenance groups must be unique")
    if set(external_keys) != set(required_keys):
        raise _error("incomplete_source_file_coverage", "physical source workbook coverage is incomplete")
    declared_non_json = set(artifact_manifest) - _CORE_JSON_ARTIFACTS
    if workbook_artifacts != declared_non_json:
        raise _error("source_file_artifact_set_mismatch", "source workbook artifact set is inconsistent")


def _source_file_key(value: Mapping[str, object]) -> tuple[str, str, str]:
    kind = value.get("kind")
    source_identifier = value.get("source_identifier")
    checksum = value.get("sha256")
    if kind not in {"room_match", "win5"} or not isinstance(source_identifier, str) or not source_identifier:
        raise _error("invalid_source_file_coverage", "source workbook provenance is invalid")
    return str(kind), source_identifier, _normalize_sha256(checksum, code="invalid_source_file_coverage")


def _validate_table_set(
    payload: Mapping[str, object],
    *,
    expected: frozenset[str],
    exact: bool,
    require_schema: bool = False,
) -> dict[str, Mapping[str, object]]:
    raw_tables = _mapping(payload.get("tables"), code="invalid_snapshot_table_set")
    observed = set(raw_tables)
    if (exact and observed != expected) or (not exact and not expected.issubset(observed)):
        raise _error("invalid_snapshot_table_set", "bundle snapshot table set does not match the format contract")
    tables: dict[str, Mapping[str, object]] = {}
    for table_name, snapshot_value in raw_tables.items():
        if not isinstance(table_name, str) or not table_name:
            raise _error("invalid_snapshot_table_name", "bundle contains an invalid snapshot table name")
        snapshot = _mapping(snapshot_value, code="invalid_table_snapshot")
        _validate_snapshot(snapshot, require_schema=require_schema)
        tables[table_name] = snapshot
    return tables


def _validate_snapshot(snapshot: Mapping[str, object], *, require_schema: bool) -> None:
    columns_value = _sequence(snapshot.get("columns"), code="invalid_table_snapshot")
    columns = tuple(columns_value)
    if not columns or any(not isinstance(column, str) or not column for column in columns):
        raise _error("invalid_table_snapshot", "table snapshot columns are invalid")
    if len(columns) != len(set(columns)):
        raise _error("invalid_table_snapshot", "table snapshot columns contain duplicates")
    rows = _sequence(snapshot.get("rows"), code="invalid_table_snapshot")
    row_count = snapshot.get("row_count")
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count != len(rows):
        raise _error("snapshot_row_count_mismatch", "table snapshot row count is inconsistent")
    for row_value in rows:
        row = _mapping(row_value, code="invalid_table_snapshot")
        if set(row) != set(columns):
            raise _error("snapshot_column_mismatch", "table snapshot row columns are inconsistent")
    if _normalize_sha256(snapshot.get("rows_sha256"), code="invalid_table_snapshot") != _canonical_sha256(rows):
        raise _error("snapshot_rows_checksum_mismatch", "table snapshot rows checksum is inconsistent")
    if require_schema:
        schema = _sequence(snapshot.get("schema"), code="invalid_operational_schema")
        schema_names = []
        for item_value in schema:
            item = _mapping(item_value, code="invalid_operational_schema")
            name = item.get("name")
            if (
                not isinstance(name, str)
                or not isinstance(item.get("type"), str)
                or not isinstance(item.get("nullable"), bool)
            ):
                raise _error("invalid_operational_schema", "operational table schema is invalid")
            schema_names.append(name)
        if tuple(schema_names) != columns:
            raise _error("operational_schema_column_mismatch", "operational schema columns are inconsistent")
        if _normalize_sha256(snapshot.get("schema_sha256"), code="invalid_operational_schema") != _canonical_sha256(
            schema
        ):
            raise _error("operational_schema_checksum_mismatch", "operational schema checksum is inconsistent")


def _validate_identity_source_subset(
    source_tables: Mapping[str, Mapping[str, object]],
    identity_tables: Mapping[str, Mapping[str, object]],
) -> None:
    source_rows = _snapshot_rows(source_tables["sheet_import_records"])
    all_records = {_required_str(row, "source_key"): row for row in source_rows}
    identity_records = _snapshot_rows(identity_tables["identity_source_import_records"])
    if len(all_records) != len(source_rows):
        raise _error("duplicate_source_key", "source import records contain duplicate source keys")
    for row in identity_records:
        source_key = _required_str(row, "source_key")
        if all_records.get(source_key) != row:
            raise _error("identity_source_subset_mismatch", "identity source records are not an exact source subset")


def _validate_table_fingerprints(
    reconciliation: Mapping[str, object],
    *,
    source_tables: Mapping[str, Mapping[str, object]],
    identity_tables: Mapping[str, Mapping[str, object]],
    settings_tables: Mapping[str, Mapping[str, object]],
    operational_tables: Mapping[str, Mapping[str, object]],
) -> None:
    fingerprints = _mapping(reconciliation.get("table_fingerprints"), code="invalid_table_fingerprints")
    actual_tables = {
        **source_tables,
        **{name: value for name, value in identity_tables.items() if name != "identity_source_import_records"},
        **settings_tables,
        **operational_tables,
    }
    if set(fingerprints) != set(actual_tables):
        raise _error("table_fingerprint_set_mismatch", "reconciliation table fingerprints are incomplete")
    for name, snapshot in actual_tables.items():
        fingerprint = _mapping(fingerprints[name], code="invalid_table_fingerprints")
        if fingerprint != {
            "row_count": snapshot["row_count"],
            "rows_sha256": snapshot["rows_sha256"],
        }:
            raise _error("table_fingerprint_mismatch", "reconciliation table fingerprint is inconsistent")


def _snapshot_rows(snapshot: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    return tuple(
        _mapping(row, code="invalid_table_snapshot")
        for row in _sequence(snapshot.get("rows"), code="invalid_table_snapshot")
    )


def _mapping(value: object, *, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error(code, "bundle structure is invalid")
    return value


def _sequence(value: object, *, code: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise _error(code, "bundle structure is invalid")
    return value


def _required_str(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise _error("invalid_bundle_row", "bundle row contains an invalid required string")
    return item


def _normalize_sha256(value: object, *, code: str) -> str:
    if not isinstance(value, str):
        raise _error(code, "SHA-256 value is invalid")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise _error(code, "SHA-256 value is invalid")
    return normalized


def _canonical_sha256(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _error(code: str, message: str) -> RebuildOverlayImportError:
    return RebuildOverlayImportError(code, message)
