from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector

from umacircle_bot.runtime_preflight import EXPECTED_ALEMBIC_HEAD, EXPECTED_ROOM_POINT_SCALE
from umacircle_bot.services.rebuild_overlay_bundle import (
    RebuildOverlayImportError,
    load_rebuild_overlay_bundle,
)
from umacircle_bot.services.rebuild_overlay_reconciliation import reconcile_rebuild_target
from umacircle_bot.services.rebuild_source_export import DEFAULT_EXPECTED_SOURCE_REVISION

_TARGET_COLUMNS = {
    "alembic_version": ("version_num",),
    "room_point_scale_state": ("id", "scale_version"),
    "sheet_import_runs": (
        "id",
        "import_kind",
        "source_type",
        "source_identifier",
        "source_checksum",
        "finished_at",
        "status",
    ),
    "sheet_import_records": (
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
    ),
    "personas": ("id", "main_game_account_id"),
    "discord_accounts": ("id", "discord_user_id", "discord_nickname", "persona_id"),
    "game_accounts": (
        "id",
        "discord_account_id",
        "persona_id",
        "uma_pid",
        "nickname",
        "ingame_name",
        "identity_status",
    ),
    "bets": ("persona_id", "game_account_id"),
    "race_entries": ("owner_at_event_persona_id",),
    "race_results": ("owner_at_event_persona_id",),
    "identity_backfill_tasks": (
        "id",
        "game_account_id",
        "source_import_record_id",
        "status",
        "conflict_detail_json",
        "resolved_by_discord_user_id",
        "resolved_at",
        "resolution_note",
    ),
    "room_point_accounts": ("id", "persona_id", "balance"),
    "room_point_transactions": (
        "id",
        "persona_id",
        "game_account_id",
        "source",
        "type",
        "amount",
        "related_race_result_id",
    ),
    "player_link_requests": (
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
    ),
    "player_link_operation_audits": (
        "id",
        "player_link_request_id",
        "action",
        "actor_discord_user_id",
        "idempotency_key",
        "request_fingerprint",
        "before_json",
        "after_json",
        "reason",
    ),
    "guild_discord_settings": (
        "id",
        "guild_id",
        "win5_announcement_channel_id",
        "room_match_announcement_channel_id",
        "log_channel_id",
        "operator_role_id",
        "bot_manager_role_id",
        "default_timezone",
        "win5_announcements_enabled",
        "room_match_announcements_enabled",
        "revision_number",
    ),
    "guild_discord_settings_audits": (
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
    ),
}
_EXPECTED_TARGET_TABLES = frozenset(
    {
        "account_registration_operation_audits",
        "account_registration_requests",
        "alembic_version",
        "bet_judgements",
        "bets",
        "discord_accounts",
        "discord_publication_audits",
        "discord_publications",
        "export_runs",
        "game_accounts",
        "game_events",
        "guild_discord_settings",
        "guild_discord_settings_audits",
        "identity_backfill_tasks",
        "match_result_submissions",
        "persona_link_operation_audits",
        "personas",
        "player_link_operation_audits",
        "player_link_requests",
        "race_conditions",
        "race_entries",
        "race_operation_audits",
        "race_rating_contexts",
        "race_results",
        "races",
        "rating_events",
        "rating_rule_versions",
        "rating_rules",
        "report_snapshots",
        "room_match_odds_snapshot_entries",
        "room_match_odds_snapshots",
        "room_match_result_provenance",
        "room_match_result_publications",
        "room_point_accounts",
        "room_point_scale_state",
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
_EXPECTED_TARGET_MARIADB_SCHEMA_SHA256 = frozenset(
    {
        # MariaDB 11.4.7, Alembic 20260815_0030, utf8mb4 current-head schema.
        "b36cb51f3e522101b655e3f02cbeec636cd1cb888634449be49203084eed22a8",
    }
)
_REQUIRED_UNIQUES = {
    "discord_accounts": {("discord_user_id",)},
    "game_accounts": {("uma_pid",)},
    "guild_discord_settings": {("guild_id",)},
    "guild_discord_settings_audits": {("idempotency_key",)},
    "identity_backfill_tasks": {("game_account_id",), ("source_import_record_id",)},
    "personas": {("main_game_account_id",)},
    "player_link_operation_audits": {("idempotency_key",)},
    "player_link_requests": {
        ("idempotency_key",),
        ("selected_game_account_id",),
        ("guild_id", "requester_discord_user_id", "active_request_marker"),
    },
    "room_point_accounts": {("persona_id",)},
    "sheet_import_records": {("source_key",)},
}
_REQUIRED_FOREIGN_KEYS = {
    "bets": {
        ("game_account_id",): ("game_accounts", ("id",)),
        ("persona_id",): ("personas", ("id",)),
    },
    "discord_accounts": {("persona_id",): ("personas", ("id",))},
    "game_accounts": {
        ("discord_account_id",): ("discord_accounts", ("id",)),
        ("persona_id",): ("personas", ("id",)),
    },
    "guild_discord_settings_audits": {
        ("guild_discord_settings_id",): ("guild_discord_settings", ("id",)),
    },
    "identity_backfill_tasks": {
        ("game_account_id",): ("game_accounts", ("id",)),
        ("source_import_record_id",): ("sheet_import_records", ("id",)),
    },
    "player_link_operation_audits": {
        ("player_link_request_id",): ("player_link_requests", ("id",)),
    },
    "player_link_requests": {
        ("selected_game_account_id",): ("game_accounts", ("id",)),
    },
    "room_point_accounts": {("persona_id",): ("personas", ("id",))},
    "room_point_transactions": {
        ("game_account_id",): ("game_accounts", ("id",)),
        ("persona_id",): ("personas", ("id",)),
        ("related_race_result_id",): ("race_results", ("id",)),
    },
    "race_entries": {("owner_at_event_persona_id",): ("personas", ("id",))},
    "race_results": {("owner_at_event_persona_id",): ("personas", ("id",))},
}
_IMPORT_RECORD_COMPARE_COLUMNS = (
    "source_key",
    "row_fingerprint",
    "source_sheet_name",
    "source_row_number",
    "record_type",
    "status",
    "target_entity_type",
)
_PLAYER_REQUEST_COMPARE_COLUMNS = (
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
    "reviewed_by_discord_user_id",
    "review_note",
    "idempotency_key",
    "request_fingerprint",
    "resolved_at",
)
_PLAYER_AUDIT_COMPARE_COLUMNS = (
    "action",
    "actor_discord_user_id",
    "idempotency_key",
    "request_fingerprint",
    "before_json",
    "after_json",
    "reason",
)
_SETTINGS_COMPARE_COLUMNS = (
    "guild_id",
    "win5_announcement_channel_id",
    "room_match_announcement_channel_id",
    "log_channel_id",
    "operator_role_id",
    "bot_manager_role_id",
    "default_timezone",
    "win5_announcements_enabled",
    "room_match_announcements_enabled",
    "revision_number",
)
_SETTINGS_AUDIT_COMPARE_COLUMNS = (
    "guild_id",
    "action",
    "actor_discord_user_id",
    "idempotency_key",
    "request_fingerprint",
    "before_json",
    "after_json",
    "reason",
)
_JSON_COMPARE_COLUMNS = frozenset({"before_json", "after_json", "conflict_detail_json"})
_IMPLEMENTATION_COVERAGE = {
    "bundle_artifacts": "complete",
    "target_schema": "complete",
    "import_provenance": "complete",
    "identity_workflow_settings": "complete",
    "room_points": "complete",
    "room_match": "aggregate_only",
    "not_yet_operational": "count_only",
    "operational_overlay_row_remap": "not_implemented",
}
_UNIMPLEMENTED_CATEGORIES = (
    "apply_transaction",
    "operational_overlay_row_remap",
)


@dataclass(frozen=True, slots=True)
class RebuildOverlayRemapPlan:
    source_revision: str
    target_revision: str
    manifest_sha256: str
    plan_sha256: str
    point_scale_version: int
    apply_supported: bool
    conflict_free_within_implemented_coverage: bool
    coverage: dict[str, str]
    unimplemented_categories: tuple[str, ...]
    conflict_counts: dict[str, int]
    import_mapping: dict[str, int]
    identity_mapping: dict[str, int]
    workflow_actions: dict[str, dict[str, int]]
    settings_actions: dict[str, dict[str, int]]
    reconciliation: dict[str, object]


class _Conflicts:
    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def add(self, code: str, count: int = 1) -> None:
        if count > 0:
            self._counts[code] += count

    def as_dict(self) -> dict[str, int]:
        return dict(sorted(self._counts.items()))

    def __bool__(self) -> bool:
        return bool(self._counts)


def build_rebuild_overlay_remap_plan(
    connection: Connection,
    *,
    bundle_directory: Path,
    expected_manifest_sha256: str,
    expected_source_revision: str = DEFAULT_EXPECTED_SOURCE_REVISION,
    allow_unverified_windows_acl: bool = False,
    allow_synthetic_test_bundle: bool = False,
) -> RebuildOverlayRemapPlan:
    """Build a PII-free, zero-write remap plan against an exact current-head target."""
    bundle = load_rebuild_overlay_bundle(
        bundle_directory,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_source_revision=expected_source_revision,
        allow_unverified_windows_acl=allow_unverified_windows_acl,
        allow_synthetic_test_bundle=allow_synthetic_test_bundle,
    )
    target_schema_sha256 = _validate_target_schema(connection)
    conflicts = _Conflicts()
    decisions: list[dict[str, object]] = []
    source_tables = _tables(bundle.source_manifest)
    identity_tables = _tables(bundle.identity_overlay)
    settings_tables = _tables(bundle.settings_overlay)

    import_mapping, target_records = _map_import_provenance(
        connection,
        source_tables=source_tables,
        conflicts=conflicts,
        decisions=decisions,
    )
    identity_mapping, source_to_target_game = _map_identity_overlay(
        connection,
        identity_tables=identity_tables,
        target_records=target_records,
        conflicts=conflicts,
        decisions=decisions,
    )
    workflow_actions = _map_player_link_workflow(
        connection,
        identity_tables=identity_tables,
        source_to_target_game=source_to_target_game,
        conflicts=conflicts,
        decisions=decisions,
    )
    settings_actions = _map_guild_settings(
        connection,
        settings_tables=settings_tables,
        conflicts=conflicts,
        decisions=decisions,
    )
    reconciliation = reconcile_rebuild_target(
        connection,
        bundle=bundle,
        source_to_target_game=source_to_target_game,
        add_conflict=conflicts.add,
    )
    if _validate_target_schema(connection) != target_schema_sha256:
        raise _error("target_schema_changed", "target schema changed during rebuild overlay planning")

    conflict_counts = conflicts.as_dict()
    plan_sha256 = _canonical_sha256(
        {
            "manifest_sha256": bundle.manifest_sha256,
            "target_revision": EXPECTED_ALEMBIC_HEAD,
            "target_schema_sha256": target_schema_sha256,
            "point_scale_version": EXPECTED_ROOM_POINT_SCALE,
            "coverage": _IMPLEMENTATION_COVERAGE,
            "unimplemented_categories": _UNIMPLEMENTED_CATEGORIES,
            "decisions": sorted(decisions, key=_canonical_json),
            "import_mapping": import_mapping,
            "identity_mapping": identity_mapping,
            "workflow_actions": workflow_actions,
            "settings_actions": settings_actions,
            "reconciliation": reconciliation,
            "conflict_counts": conflict_counts,
        }
    )
    return RebuildOverlayRemapPlan(
        source_revision=bundle.source_revision,
        target_revision=EXPECTED_ALEMBIC_HEAD,
        manifest_sha256=bundle.manifest_sha256,
        plan_sha256=plan_sha256,
        point_scale_version=EXPECTED_ROOM_POINT_SCALE,
        apply_supported=False,
        conflict_free_within_implemented_coverage=not conflicts,
        coverage=dict(_IMPLEMENTATION_COVERAGE),
        unimplemented_categories=_UNIMPLEMENTED_CATEGORIES,
        conflict_counts=conflict_counts,
        import_mapping=import_mapping,
        identity_mapping=identity_mapping,
        workflow_actions=workflow_actions,
        settings_actions=settings_actions,
        reconciliation=reconciliation,
    )


def public_rebuild_overlay_summary(plan: RebuildOverlayRemapPlan) -> dict[str, object]:
    return {
        "mode": "dry-run",
        "source_revision": plan.source_revision,
        "target_revision": plan.target_revision,
        "manifest_sha256": plan.manifest_sha256,
        "plan_sha256": plan.plan_sha256,
        "point_scale_version": plan.point_scale_version,
        "apply_supported": plan.apply_supported,
        "conflict_free_within_implemented_coverage": plan.conflict_free_within_implemented_coverage,
        "coverage": plan.coverage,
        "unimplemented_categories": plan.unimplemented_categories,
        "conflict_counts": plan.conflict_counts,
        "import_mapping": plan.import_mapping,
        "identity_mapping": plan.identity_mapping,
        "workflow_actions": plan.workflow_actions,
        "settings_actions": plan.settings_actions,
        "reconciliation": plan.reconciliation,
    }


def _validate_target_schema(connection: Connection) -> str:
    inspector = inspect(connection)
    available_tables = frozenset(inspector.get_table_names())
    if available_tables != _EXPECTED_TARGET_TABLES:
        raise _error("target_schema_table_set_mismatch", "target table set does not match the current-head contract")
    for table_name, required_columns in _TARGET_COLUMNS.items():
        actual_columns = {str(column["name"]) for column in inspector.get_columns(table_name)}
        if not set(required_columns).issubset(actual_columns):
            raise _error("target_schema_incomplete", "target schema is missing required current-head columns")
    _validate_target_unique_contracts(inspector)
    _validate_target_foreign_keys(inspector)
    revisions = tuple(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    if revisions != (EXPECTED_ALEMBIC_HEAD,):
        raise _error("target_revision_mismatch", "target must have exactly the current Alembic head")
    scale_rows = tuple(
        connection.execute(text("SELECT id, scale_version FROM room_point_scale_state ORDER BY id")).tuples()
    )
    if scale_rows != ((1, EXPECTED_ROOM_POINT_SCALE),):
        raise _error("target_point_scale_mismatch", "target Circle Point scale singleton is invalid")
    if connection.dialect.name not in {"mysql", "mariadb"}:
        return _canonical_sha256({"dialect": connection.dialect.name, "tables": sorted(available_tables)})
    observed = _target_mariadb_schema_sha256(connection, expected_tables=available_tables)
    if observed not in _EXPECTED_TARGET_MARIADB_SCHEMA_SHA256:
        raise _error("target_schema_fingerprint_mismatch", "target MariaDB DDL profile is not approved")
    return observed


def _validate_target_unique_contracts(inspector: Inspector) -> None:
    for table_name, expected in _REQUIRED_UNIQUES.items():
        observed = {
            tuple(str(column) for column in constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(table_name)
        }
        observed.update(
            tuple(str(column) for column in index.get("column_names") or ())
            for index in inspector.get_indexes(table_name)
            if index.get("unique")
        )
        if not expected.issubset(observed):
            raise _error("target_unique_contract_mismatch", "target unique constraints are incomplete")


def _validate_target_foreign_keys(inspector: Inspector) -> None:
    for table_name, expected in _REQUIRED_FOREIGN_KEYS.items():
        observed = {
            tuple(str(column) for column in foreign_key.get("constrained_columns") or ()): (
                str(foreign_key.get("referred_table")),
                tuple(str(column) for column in foreign_key.get("referred_columns") or ()),
            )
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        if any(observed.get(columns) != target for columns, target in expected.items()):
            raise _error("target_foreign_key_contract_mismatch", "target foreign keys are incomplete")


def _target_mariadb_schema_sha256(connection: Connection, *, expected_tables: frozenset[str]) -> str:
    table_hashes = {
        table_name: _canonical_sha256(_normalized_show_create_table(connection, table_name))
        for table_name in sorted(expected_tables)
    }
    return _canonical_sha256(table_hashes)


def _normalized_show_create_table(connection: Connection, table_name: str) -> str:
    quoted = connection.dialect.identifier_preparer.quote_identifier(table_name)
    row = connection.exec_driver_sql(f"SHOW CREATE TABLE {quoted}").one()
    without_sequence = re.sub(
        r"\s+AUTO_INCREMENT=\d+\b",
        "",
        str(row[1]),
        flags=re.IGNORECASE,
    )
    return " ".join(without_sequence.split())


def _map_import_provenance(
    connection: Connection,
    *,
    source_tables: Mapping[str, Mapping[str, object]],
    conflicts: _Conflicts,
    decisions: list[dict[str, object]],
) -> tuple[dict[str, int], dict[str, dict[str, object]]]:
    source_runs = _snapshot_rows(source_tables["sheet_import_runs"])
    source_records = _snapshot_rows(source_tables["sheet_import_records"])
    target_runs = _select_rows(connection, "sheet_import_runs", _TARGET_COLUMNS["sheet_import_runs"])
    target_records_rows = _select_rows(connection, "sheet_import_records", _TARGET_COLUMNS["sheet_import_records"])
    target_runs_by_key: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for row in target_runs:
        target_runs_by_key.setdefault(_import_run_key(row), []).append(row)

    source_run_to_target: dict[int, int] = {}
    source_run_keys: set[tuple[object, ...]] = set()
    mapped_target_run_ids: set[int] = set()
    for source_run in source_runs:
        source_id = _required_int(source_run, "id")
        if source_run.get("status") != "completed" or source_run.get("finished_at") is None:
            conflicts.add("source_import_run_not_completed")
            continue
        source_run_key = _import_run_key(source_run)
        if source_run_key in source_run_keys:
            conflicts.add("source_import_run_key_duplicate")
            continue
        source_run_keys.add(source_run_key)
        matches = target_runs_by_key.get(source_run_key, [])
        completed = [row for row in matches if row.get("status") == "completed" and row.get("finished_at") is not None]
        if len(completed) != 1:
            conflicts.add("target_import_run_missing" if not completed else "target_import_run_ambiguous")
            continue
        target_run_id = _required_int(completed[0], "id")
        if target_run_id in mapped_target_run_ids:
            conflicts.add("source_import_run_target_reused")
            continue
        mapped_target_run_ids.add(target_run_id)
        source_run_to_target[source_id] = target_run_id
        decisions.append({"kind": "import_run", "key": _canonical_sha256(source_run_key)})

    conflicts.add(
        "target_import_run_extra",
        len({_required_int(row, "id") for row in target_runs} - mapped_target_run_ids),
    )

    target_records_by_key = _unique_target_rows(
        target_records_rows,
        key="source_key",
        duplicate_code="target_source_key_duplicate",
        conflicts=conflicts,
    )
    source_keys: set[str] = set()
    matched_count = 0
    for source_record in source_records:
        source_key = _required_str(source_record, "source_key")
        if source_key in source_keys:
            conflicts.add("source_source_key_duplicate")
            continue
        source_keys.add(source_key)
        target_record = target_records_by_key.get(source_key)
        if target_record is None:
            conflicts.add("target_source_record_missing")
            continue
        expected_target_run_id = source_run_to_target.get(_required_int(source_record, "import_run_id"))
        if expected_target_run_id is None or target_record.get("import_run_id") != expected_target_run_id:
            conflicts.add("target_source_record_run_mismatch")
            continue
        if not _columns_equal(source_record, target_record, _IMPORT_RECORD_COMPARE_COLUMNS):
            conflicts.add("target_source_record_fingerprint_mismatch")
            continue
        matched_count += 1
        decisions.append({"kind": "import_record", "source_key": source_key})
    extra_count = len(set(target_records_by_key) - source_keys)
    conflicts.add("target_source_record_extra", extra_count)
    return (
        {
            "source_run_count": len(source_runs),
            "mapped_run_count": len(source_run_to_target),
            "source_record_count": len(source_records),
            "mapped_record_count": matched_count,
            "extra_target_record_count": extra_count,
        },
        target_records_by_key,
    )


def _map_identity_overlay(
    connection: Connection,
    *,
    identity_tables: Mapping[str, Mapping[str, object]],
    target_records: Mapping[str, dict[str, object]],
    conflicts: _Conflicts,
    decisions: list[dict[str, object]],
) -> tuple[dict[str, int], dict[int, int]]:
    source_identity_records = _snapshot_rows(identity_tables["identity_source_import_records"])
    source_games = {_required_int(row, "id"): row for row in _snapshot_rows(identity_tables["game_accounts"])}
    source_discords = {_required_int(row, "id"): row for row in _snapshot_rows(identity_tables["discord_accounts"])}
    source_tasks = _snapshot_rows(identity_tables["identity_backfill_tasks"])
    source_tasks_by_record = {
        _required_int(row, "source_import_record_id"): row
        for row in source_tasks
        if row.get("source_import_record_id") is not None
    }
    target_games = {
        _required_int(row, "id"): row
        for row in _select_rows(connection, "game_accounts", _TARGET_COLUMNS["game_accounts"])
    }
    target_discords = {
        _required_int(row, "id"): row
        for row in _select_rows(connection, "discord_accounts", _TARGET_COLUMNS["discord_accounts"])
    }
    target_discord_by_user = _unique_target_rows(
        tuple(target_discords.values()),
        key="discord_user_id",
        duplicate_code="target_discord_user_duplicate",
        conflicts=conflicts,
    )
    target_personas = {str(row["id"]): row for row in _select_rows(connection, "personas", _TARGET_COLUMNS["personas"])}
    target_tasks = _select_rows(connection, "identity_backfill_tasks", _TARGET_COLUMNS["identity_backfill_tasks"])
    target_tasks_by_record = {
        _required_int(row, "source_import_record_id"): row
        for row in target_tasks
        if row.get("source_import_record_id") is not None
    }
    target_wallets_by_persona: dict[str, list[dict[str, object]]] = {}
    for row in _select_rows(connection, "room_point_accounts", _TARGET_COLUMNS["room_point_accounts"]):
        target_wallets_by_persona.setdefault(str(row["persona_id"]), []).append(row)
    target_pid_owners = {
        str(row["uma_pid"]): _required_int(row, "id") for row in target_games.values() if row.get("uma_pid") is not None
    }

    source_to_target_game: dict[int, int] = {}
    mapped_count = 0
    existing_count = 0
    update_count = 0
    mapped_personas: set[str] = set()
    mapped_source_game_ids: set[int] = set()
    mapped_source_discord_ids: set[int] = set()
    mapped_source_task_ids: set[int] = set()
    mapped_target_game_ids: set[int] = set()
    mapped_target_discord_ids: set[int] = set()
    mapped_target_task_ids: set[int] = set()
    for source_record in source_identity_records:
        source_record_id = _required_int(source_record, "id")
        source_key = _required_str(source_record, "source_key")
        source_game_id = source_record.get("target_entity_id")
        if source_record.get("target_entity_type") != "game_account" or not isinstance(source_game_id, int):
            conflicts.add("source_identity_target_missing")
            continue
        source_game = source_games.get(source_game_id)
        source_task = source_tasks_by_record.get(source_record_id)
        target_record = target_records.get(source_key)
        if source_game is None or source_task is None or source_task.get("game_account_id") != source_game_id:
            conflicts.add("source_identity_graph_incomplete")
            continue
        if target_record is None or target_record.get("target_entity_type") != "game_account":
            conflicts.add("target_identity_source_mapping_missing")
            continue
        target_game_id = target_record.get("target_entity_id")
        if not isinstance(target_game_id, int):
            conflicts.add("target_identity_source_mapping_missing")
            continue
        target_game = target_games.get(target_game_id)
        target_task = target_tasks_by_record.get(_required_int(target_record, "id"))
        source_discord_id = source_game.get("discord_account_id")
        if target_game is None or target_task is None or not isinstance(source_discord_id, int):
            conflicts.add("target_identity_graph_incomplete")
            continue
        source_discord = source_discords.get(source_discord_id)
        target_discord_id = target_game.get("discord_account_id")
        target_persona_id = target_game.get("persona_id")
        if source_discord is None or not isinstance(target_discord_id, int) or not isinstance(target_persona_id, str):
            conflicts.add("target_identity_graph_incomplete")
            continue
        target_discord = target_discords.get(target_discord_id)
        target_persona = target_personas.get(target_persona_id)
        expected_discord = target_discord_by_user.get(str(source_discord.get("discord_user_id")))
        if (
            target_discord is None
            or target_persona is None
            or expected_discord is None
            or expected_discord.get("id") != target_discord_id
            or target_discord.get("discord_user_id") != source_discord.get("discord_user_id")
            or target_discord.get("persona_id") != target_persona_id
            or target_persona.get("main_game_account_id") != target_game_id
            or target_task.get("game_account_id") != target_game_id
            or len(target_wallets_by_persona.get(target_persona_id, ())) != 1
        ):
            conflicts.add("target_identity_graph_mismatch")
            continue
        source_pid = source_game.get("uma_pid")
        if source_pid is None:
            if target_game.get("uma_pid") is not None:
                conflicts.add("target_pid_conflict")
                continue
        elif (
            target_game.get("uma_pid") not in {None, source_pid}
            or target_pid_owners.get(str(source_pid), target_game_id) != target_game_id
        ):
            conflicts.add("target_pid_conflict")
            continue

        mutable_equal = (
            source_discord.get("discord_nickname") == target_discord.get("discord_nickname")
            and source_game.get("nickname") == target_game.get("nickname")
            and source_game.get("ingame_name") == target_game.get("ingame_name")
            and source_game.get("identity_status") == target_game.get("identity_status")
            and source_pid == target_game.get("uma_pid")
            and _task_state_equal(source_task, target_task)
        )
        action = "existing" if mutable_equal else "update"
        existing_count += int(mutable_equal)
        update_count += int(not mutable_equal)
        mapped_count += 1
        mapped_source_game_ids.add(source_game_id)
        mapped_source_discord_ids.add(source_discord_id)
        mapped_source_task_ids.add(_required_int(source_task, "id"))
        mapped_target_game_ids.add(target_game_id)
        mapped_target_discord_ids.add(target_discord_id)
        mapped_target_task_ids.add(_required_int(target_task, "id"))
        mapped_personas.add(target_persona_id)
        source_to_target_game[source_game_id] = target_game_id
        decisions.append({"kind": "identity", "source_key": source_key, "action": action})

    conflicts.add("source_game_account_unmapped", len(set(source_games) - mapped_source_game_ids))
    conflicts.add("source_discord_account_unmapped", len(set(source_discords) - mapped_source_discord_ids))
    conflicts.add(
        "source_identity_task_unmapped",
        len({_required_int(row, "id") for row in source_tasks} - mapped_source_task_ids),
    )
    conflicts.add("target_game_account_extra", len(set(target_games) - mapped_target_game_ids))
    conflicts.add("target_discord_account_extra", len(set(target_discords) - mapped_target_discord_ids))
    conflicts.add("target_persona_extra", len(set(target_personas) - mapped_personas))
    conflicts.add(
        "target_identity_task_extra",
        len({_required_int(row, "id") for row in target_tasks} - mapped_target_task_ids),
    )
    if len(mapped_personas) != mapped_count:
        conflicts.add("target_persona_mapping_not_one_to_one", mapped_count - len(mapped_personas))
    return (
        {
            "source_identity_count": len(source_identity_records),
            "mapped_identity_count": mapped_count,
            "existing_identity_count": existing_count,
            "planned_update_count": update_count,
        },
        source_to_target_game,
    )


def _map_player_link_workflow(
    connection: Connection,
    *,
    identity_tables: Mapping[str, Mapping[str, object]],
    source_to_target_game: Mapping[int, int],
    conflicts: _Conflicts,
    decisions: list[dict[str, object]],
) -> dict[str, dict[str, int]]:
    source_requests = _snapshot_rows(identity_tables["player_link_requests"])
    source_audits = _snapshot_rows(identity_tables["player_link_operation_audits"])
    target_requests = _select_rows(connection, "player_link_requests", _TARGET_COLUMNS["player_link_requests"])
    target_audits = _select_rows(
        connection,
        "player_link_operation_audits",
        _TARGET_COLUMNS["player_link_operation_audits"],
    )
    target_requests_by_key = _unique_target_rows(
        target_requests,
        key="idempotency_key",
        duplicate_code="target_player_request_key_duplicate",
        conflicts=conflicts,
    )
    target_audits_by_key = _unique_target_rows(
        target_audits,
        key="idempotency_key",
        duplicate_code="target_player_audit_key_duplicate",
        conflicts=conflicts,
    )
    active_owners: dict[tuple[object, ...], int] = {}
    selected_owners: dict[int, int] = {}
    for target_request in target_requests:
        target_id = _required_int(target_request, "id")
        active_key = _active_request_key(target_request)
        if active_key is not None:
            if active_key in active_owners:
                conflicts.add("target_player_active_requester_duplicate")
            active_owners[active_key] = target_id
        selected_game = target_request.get("selected_game_account_id")
        if isinstance(selected_game, int):
            if selected_game in selected_owners:
                conflicts.add("target_player_selected_game_duplicate")
            selected_owners[selected_game] = target_id

    source_request_id_to_target: dict[int, int | None] = {}
    request_counts = Counter[str]()
    source_request_keys: set[str] = set()
    for source_request in source_requests:
        source_id = _required_int(source_request, "id")
        key = _required_str(source_request, "idempotency_key")
        if key in source_request_keys:
            conflicts.add("source_player_request_key_duplicate")
            continue
        source_request_keys.add(key)
        selected_source_game = source_request.get("selected_game_account_id")
        selected_target_game: int | None = None
        if selected_source_game is not None:
            if not isinstance(selected_source_game, int) or selected_source_game not in source_to_target_game:
                conflicts.add("player_request_game_mapping_missing")
                continue
            selected_target_game = source_to_target_game[selected_source_game]
        target_request = target_requests_by_key.get(key)
        target_id = _required_int(target_request, "id") if target_request is not None else None
        if target_request is None:
            action = "create"
        elif (
            target_request.get("request_fingerprint") == source_request.get("request_fingerprint")
            and target_request.get("selected_game_account_id") == selected_target_game
            and _columns_equal(source_request, target_request, _PLAYER_REQUEST_COMPARE_COLUMNS)
        ):
            action = "existing"
        else:
            conflicts.add("player_request_existing_conflict")
            continue

        active_key = _active_request_key(source_request)
        if active_key is not None and active_owners.get(active_key) not in {None, target_id}:
            conflicts.add("player_request_active_requester_conflict")
            continue
        if selected_target_game is not None and selected_owners.get(selected_target_game) not in {None, target_id}:
            conflicts.add("player_request_selected_game_conflict")
            continue
        if active_key is not None:
            active_owners[active_key] = target_id if target_id is not None else -source_id
        if selected_target_game is not None:
            selected_owners[selected_target_game] = target_id if target_id is not None else -source_id
        source_request_id_to_target[source_id] = target_id
        request_counts[action] += 1
        decisions.append({"kind": "player_request", "key": _canonical_sha256(key), "action": action})

    audit_counts = Counter[str]()
    source_audit_keys: set[str] = set()
    for source_audit in source_audits:
        source_request_id = _required_int(source_audit, "player_link_request_id")
        if source_request_id not in source_request_id_to_target:
            conflicts.add("player_audit_request_mapping_missing")
            continue
        key = _required_str(source_audit, "idempotency_key")
        if key in source_audit_keys:
            conflicts.add("source_player_audit_key_duplicate")
            continue
        source_audit_keys.add(key)
        target_audit = target_audits_by_key.get(key)
        target_request_id = source_request_id_to_target[source_request_id]
        if target_audit is None:
            action = "create"
        elif (
            target_request_id is not None
            and target_audit.get("player_link_request_id") == target_request_id
            and _columns_equal(source_audit, target_audit, _PLAYER_AUDIT_COMPARE_COLUMNS)
        ):
            action = "existing"
        else:
            conflicts.add("player_audit_existing_conflict")
            continue
        audit_counts[action] += 1
        decisions.append({"kind": "player_audit", "key": _canonical_sha256(key), "action": action})
    conflicts.add(
        "target_player_request_extra",
        len(set(target_requests_by_key) - source_request_keys),
    )
    conflicts.add(
        "target_player_audit_extra",
        len(set(target_audits_by_key) - source_audit_keys),
    )
    return {
        "player_link_requests": _action_summary(len(source_requests), request_counts),
        "player_link_operation_audits": _action_summary(len(source_audits), audit_counts),
    }


def _map_guild_settings(
    connection: Connection,
    *,
    settings_tables: Mapping[str, Mapping[str, object]],
    conflicts: _Conflicts,
    decisions: list[dict[str, object]],
) -> dict[str, dict[str, int]]:
    source_settings = _snapshot_rows(settings_tables["guild_discord_settings"])
    source_audits = _snapshot_rows(settings_tables["guild_discord_settings_audits"])
    target_settings = _select_rows(connection, "guild_discord_settings", _TARGET_COLUMNS["guild_discord_settings"])
    target_audits = _select_rows(
        connection,
        "guild_discord_settings_audits",
        _TARGET_COLUMNS["guild_discord_settings_audits"],
    )
    target_settings_by_guild = _unique_target_rows(
        target_settings,
        key="guild_id",
        duplicate_code="target_guild_settings_duplicate",
        conflicts=conflicts,
    )
    target_audits_by_key = _unique_target_rows(
        target_audits,
        key="idempotency_key",
        duplicate_code="target_settings_audit_key_duplicate",
        conflicts=conflicts,
    )
    source_settings_id_to_target: dict[int, int | None] = {}
    source_settings_id_to_guild: dict[int, str] = {}
    settings_counts = Counter[str]()
    source_setting_guild_ids: set[str] = set()
    for source_row in source_settings:
        source_id = _required_int(source_row, "id")
        guild_id = _required_str(source_row, "guild_id")
        if guild_id in source_setting_guild_ids:
            conflicts.add("source_guild_settings_duplicate")
            continue
        source_setting_guild_ids.add(guild_id)
        target_row = target_settings_by_guild.get(guild_id)
        if target_row is None:
            action = "create"
            target_id = None
        elif _columns_equal(source_row, target_row, _SETTINGS_COMPARE_COLUMNS):
            action = "existing"
            target_id = _required_int(target_row, "id")
        elif _safe_int(target_row.get("revision_number")) < _safe_int(source_row.get("revision_number")):
            action = "update"
            target_id = _required_int(target_row, "id")
        else:
            conflicts.add("guild_settings_existing_conflict")
            continue
        source_settings_id_to_target[source_id] = target_id
        source_settings_id_to_guild[source_id] = guild_id
        settings_counts[action] += 1
        decisions.append({"kind": "guild_settings", "key": _canonical_sha256(guild_id), "action": action})

    audit_counts = Counter[str]()
    source_audit_keys: set[str] = set()
    for source_row in source_audits:
        source_settings_id = _required_int(source_row, "guild_discord_settings_id")
        if source_settings_id not in source_settings_id_to_target:
            conflicts.add("settings_audit_parent_mapping_missing")
            continue
        if _required_str(source_row, "guild_id") != source_settings_id_to_guild[source_settings_id]:
            conflicts.add("settings_audit_parent_guild_mismatch")
            continue
        key = _required_str(source_row, "idempotency_key")
        if key in source_audit_keys:
            conflicts.add("source_settings_audit_key_duplicate")
            continue
        source_audit_keys.add(key)
        target_row = target_audits_by_key.get(key)
        target_settings_id = source_settings_id_to_target[source_settings_id]
        if target_row is None:
            action = "create"
        elif (
            target_settings_id is not None
            and target_row.get("guild_discord_settings_id") == target_settings_id
            and _columns_equal(source_row, target_row, _SETTINGS_AUDIT_COMPARE_COLUMNS)
        ):
            action = "existing"
        else:
            conflicts.add("settings_audit_existing_conflict")
            continue
        audit_counts[action] += 1
        decisions.append({"kind": "settings_audit", "key": _canonical_sha256(key), "action": action})
    conflicts.add(
        "target_guild_settings_extra",
        len(set(target_settings_by_guild) - source_setting_guild_ids),
    )
    conflicts.add(
        "target_settings_audit_extra",
        len(set(target_audits_by_key) - source_audit_keys),
    )
    return {
        "guild_discord_settings": _action_summary(len(source_settings), settings_counts),
        "guild_discord_settings_audits": _action_summary(len(source_audits), audit_counts),
    }


def _active_request_key(row: Mapping[str, object]) -> tuple[object, ...] | None:
    marker = row.get("active_request_marker")
    if marker is None:
        return None
    return row.get("guild_id"), row.get("requester_discord_user_id"), marker


def _select_rows(connection: Connection, table_name: str, columns: Sequence[str]) -> tuple[dict[str, object], ...]:
    quoted_table = connection.dialect.identifier_preparer.quote_identifier(table_name)
    quoted_columns = ", ".join(connection.dialect.identifier_preparer.quote_identifier(column) for column in columns)
    rows = connection.execute(text(f"SELECT {quoted_columns} FROM {quoted_table}")).mappings()
    return tuple(dict(row) for row in rows)


def _unique_target_rows(
    rows: Sequence[dict[str, object]],
    *,
    key: str,
    duplicate_code: str,
    conflicts: _Conflicts,
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        value = row.get(key)
        if not isinstance(value, str) or not value or value in result:
            conflicts.add(duplicate_code)
            continue
        result[value] = row
    return result


def _import_run_key(row: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(row.get(column) for column in ("import_kind", "source_type", "source_identifier", "source_checksum"))


def _task_state_equal(source: Mapping[str, object], target: Mapping[str, object]) -> bool:
    return _columns_equal(
        source,
        target,
        ("status", "conflict_detail_json", "resolved_by_discord_user_id", "resolved_at", "resolution_note"),
    )


def _columns_equal(source: Mapping[str, object], target: Mapping[str, object], columns: Sequence[str]) -> bool:
    return all(
        _comparable_value(source.get(column), column) == _comparable_value(target.get(column), column)
        for column in columns
    )


def _comparable_value(value: object, column: str) -> object:
    if column in _JSON_COMPARE_COLUMNS and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _action_summary(source_count: int, counts: Counter[str]) -> dict[str, int]:
    return {
        "source_count": source_count,
        "create_count": counts["create"],
        "update_count": counts["update"],
        "existing_count": counts["existing"],
    }


def _snapshot_rows(snapshot: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    return tuple(
        _mapping(row, code="invalid_table_snapshot")
        for row in _sequence(snapshot.get("rows"), code="invalid_table_snapshot")
    )


def _tables(payload: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    return {
        str(name): _mapping(value, code="invalid_snapshot_table_set")
        for name, value in _mapping(payload.get("tables"), code="invalid_snapshot_table_set").items()
    }


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


def _required_int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise _error("invalid_bundle_row", "bundle row contains an invalid required integer")
    return item


def _safe_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _canonical_sha256(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _error(code: str, message: str) -> RebuildOverlayImportError:
    return RebuildOverlayImportError(code, message)
