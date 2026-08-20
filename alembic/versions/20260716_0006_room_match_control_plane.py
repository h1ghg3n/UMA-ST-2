"""add WU9R room-match control plane

Revision ID: 20260716_0006
Revises: 20260715_0005
Create Date: 2026-07-16 00:06:00.000000

The previous WU9 revision was never merged into the integration branch. This
canonical WU9R revision supports only the pre-WU9 head or a fresh database
whose metadata-created schema is already complete. Unsupported partial WU9
artifacts are rejected before the first DDL.
"""

import json
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_0006"
down_revision: str | Sequence[str] | None = "20260715_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
AUDIT_TABLE = "race_operation_audits"
ENTRY_ACCOUNT_FK = "fk_race_entries_game_account_id_game_accounts"
AUDIT_RACE_FK = "fk_race_operation_audits_race_id_races"
OBSOLETE_PARTICIPANT_TABLES = {
    "race_entry_cancellations",
    "race_entry_registrations",
    "race_participant_cancellations",
    "race_participant_registrations",
    "race_participants",
    "room_match_participants",
}
OBSOLETE_PARTICIPANT_COLUMNS = {
    "cancellation_reason",
    "cancelled_at",
    "cancelled_by_discord_user_id",
    "participant_status",
    "registered_at",
    "registered_by_discord_user_id",
    "registration_reason",
    "status",
}
RACE_COLUMNS = {"betting_window_version", "betting_opened_at", "betting_closed_at"}
ENTRY_COLUMNS = {"game_account_id"}
BET_COLUMNS = {"betting_window_version"}
RACE_BASE_COLUMNS = {
    "id": ("integer", False, None),
    "event_id": ("integer", True, None),
    "external_source": ("string", True, 200),
    "external_race_id": ("string", True, 64),
    "race_kind": ("string", False, 32),
    "name": ("string", False, 200),
    "description": ("text", True, None),
    "starts_at": ("datetime", True, None),
    "status": ("string", False, 32),
    "created_at": ("datetime", False, None),
    "updated_at": ("datetime", False, None),
}
RACE_TARGET_COLUMNS = {
    "betting_window_version": ("integer", False, None),
    "betting_opened_at": ("datetime", True, None),
    "betting_closed_at": ("datetime", True, None),
}
ENTRY_BASE_COLUMNS = {
    "id": ("integer", False, None),
    "race_id": ("integer", False, None),
    "entry_number": ("integer", False, None),
    "entry_kind": ("string", False, 32),
    "entry_number_source": ("string", False, 32),
    "source_import_record_id": ("integer", True, None),
    "player_name": ("string", True, 100),
    "horse_name_or_label": ("string", True, 100),
    "running_style": ("string", True, 32),
    "horse_age": ("integer", True, None),
    "rate": ("numeric", True, None),
    "total_count": ("integer", True, None),
    "first_count": ("integer", True, None),
    "second_count": ("integer", True, None),
    "third_count": ("integer", True, None),
    "created_at": ("datetime", False, None),
    "updated_at": ("datetime", False, None),
}
ENTRY_TARGET_COLUMNS = {"game_account_id": ("integer", True, None)}
BET_BASE_COLUMNS = {
    "id": ("integer", False, None),
    "event_id": ("integer", True, None),
    "race_id": ("integer", False, None),
    "game_account_id": ("integer", False, None),
    "betting_mode": ("string", False, 32),
    "bet_type": ("string", False, 32),
    "numbers": ("json", False, None),
    "amount": ("integer", False, None),
    "status": ("string", False, 32),
    "cancelled_at": ("datetime", True, None),
    "created_at": ("datetime", False, None),
    "updated_at": ("datetime", False, None),
}
BET_TARGET_COLUMNS = {"betting_window_version": ("integer", False, None)}
AUDIT_COLUMNS = {
    "id": ("integer", False, None),
    "race_id": ("integer", False, None),
    "action": ("string", False, 64),
    "capability": ("string", False, 64),
    "actor_discord_user_id": ("string", False, 32),
    "idempotency_key": ("string", False, 128),
    "request_fingerprint": ("string", False, 64),
    "before_json": ("json", True, None),
    "after_json": ("json", False, None),
    "reason": ("string", True, 255),
    "created_at": ("datetime", False, None),
}
CONDITION_COLUMNS = {
    "id": ("integer", False, None),
    "race_id": ("integer", False, None),
    "grade": ("string", False, 16),
    "venue": ("string", False, 64),
    "track_surface": ("string", False, 32),
    "distance": ("integer", False, None),
    "direction": ("string", False, 16),
    "season": ("string", False, 16),
    "weather": ("string", False, 32),
    "track_condition": ("string", False, 32),
    "condition_label": ("string", False, 32),
    "participant_count": ("integer", False, None),
    "created_at": ("datetime", False, None),
    "updated_at": ("datetime", False, None),
}
RESULT_COLUMNS = {
    "id": ("integer", False, None),
    "race_id": ("integer", False, None),
    "entry_number": ("integer", False, None),
    "game_account_id": ("integer", True, None),
    "character_name": ("string", True, 100),
    "rank": ("integer", False, None),
    "converted_rank": ("integer", True, None),
    "is_betting_excluded": ("boolean", False, None),
    "is_rating_excluded": ("boolean", False, None),
    "is_result_void": ("boolean", False, None),
    "source_import_record_id": ("integer", True, None),
    "raw_result_json": ("json", True, None),
    "created_at": ("datetime", False, None),
    "updated_at": ("datetime", False, None),
}
POINT_ACCOUNT_COLUMNS = {
    "id": ("integer", False, None),
    "game_account_id": ("integer", False, None),
    "balance": ("integer", False, None),
    "created_at": ("datetime", False, None),
    "updated_at": ("datetime", False, None),
}
POINT_TRANSACTION_COLUMNS = {
    "id": ("integer", False, None),
    "game_account_id": ("integer", False, None),
    "type": ("string", False, 32),
    "amount": ("integer", False, None),
    "reason": ("string", True, 255),
    "source": ("string", True, 64),
    "related_bet_id": ("integer", True, None),
    "created_by_discord_user_id": ("string", True, 32),
    "idempotency_key": ("string", True, 128),
    "created_at": ("datetime", False, None),
}
ROOM_MATCH_STATUSES = (
    "setup",
    "betting_open",
    "betting_closed",
    "result_review",
    "result_confirmed",
    "settled",
    "voided",
)
MARIADB_CHARACTER_SET = "utf8mb4"
MARIADB_COLLATION = "utf8mb4_unicode_ci"
LEGACY_ROOM_MATCH_STATUSES = (
    "scheduled",
    "draft",
    "open",
    "closed",
    "running",
    "finished",
    "completed",
    "void",
    "cancelled",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    schema_mode = _classify_and_preflight(inspector, tables)
    if schema_mode == "canonical":
        canonical = sa.inspect(bind)
        _validate_canonical_schema(canonical)
        _validate_semantic_data("canonical", set(canonical.get_table_names()))
        return

    _map_legacy_room_match_statuses()
    _upgrade_race_entries()
    _upgrade_race_conditions()
    _upgrade_races()
    _upgrade_bets()
    _create_audit_table()
    canonical = sa.inspect(bind)
    _validate_canonical_schema(canonical)
    _validate_semantic_data("canonical", set(canonical.get_table_names()))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    _reject_lossy_downgrade(inspector, tables)

    if AUDIT_TABLE in tables:
        op.drop_table(AUDIT_TABLE)

    inspector = sa.inspect(bind)
    if "race_conditions" in set(inspector.get_table_names()):
        with op.batch_alter_table("race_conditions") as batch:
            if _has_check(inspector, "race_conditions", "ck_race_conditions_nonnegative_participant_count"):
                batch.drop_constraint("nonnegative_participant_count", type_="check")

    inspector = sa.inspect(bind)
    if "bets" in set(inspector.get_table_names()):
        with op.batch_alter_table("bets") as batch:
            if _has_check(inspector, "bets", "ck_bets_positive_betting_window_version"):
                batch.drop_constraint("positive_betting_window_version", type_="check")
            if "betting_window_version" in _column_names(inspector, "bets"):
                batch.drop_column("betting_window_version")

    inspector = sa.inspect(bind)
    if "race_entries" in set(inspector.get_table_names()):
        game_account_fk_name = _matching_foreign_key_name(
            inspector,
            "race_entries",
            ("game_account_id",),
            "game_accounts",
            ("id",),
        )
        with op.batch_alter_table("race_entries") as batch:
            if _has_check(inspector, "race_entries", "ck_race_entries_positive_entry_number"):
                batch.drop_constraint("positive_entry_number", type_="check")
            if game_account_fk_name is not None:
                batch.drop_constraint(game_account_fk_name, type_="foreignkey")
            if "game_account_id" in _column_names(inspector, "race_entries"):
                batch.drop_column("game_account_id")

    inspector = sa.inspect(bind)
    if "races" in set(inspector.get_table_names()):
        with op.batch_alter_table("races") as batch:
            if _has_check(inspector, "races", "ck_races_room_match_status"):
                batch.drop_constraint("room_match_status", type_="check")
            if _has_check(inspector, "races", "ck_races_positive_betting_window_version"):
                batch.drop_constraint("positive_betting_window_version", type_="check")
            batch.alter_column(
                "status",
                existing_type=sa.String(length=32),
                nullable=False,
                server_default=None,
            )
        op.execute(
            sa.text(
                "UPDATE races SET status = CASE status "
                "WHEN 'setup' THEN 'scheduled' "
                "WHEN 'betting_open' THEN 'scheduled' "
                "WHEN 'betting_closed' THEN 'closed' ELSE status END "
                "WHERE race_kind = 'room_match'"
            )
        )
        inspector = sa.inspect(bind)
        with op.batch_alter_table("races") as batch:
            for column_name in ("betting_closed_at", "betting_opened_at", "betting_window_version"):
                if column_name in _column_names(inspector, "races"):
                    batch.drop_column(column_name)


def _classify_and_preflight(inspector: sa.Inspector, tables: set[str]) -> str:
    required = {
        "races",
        "race_entries",
        "race_results",
        "game_accounts",
        "game_events",
        "sheet_import_records",
        "bets",
        "race_conditions",
        "room_point_accounts",
        "room_point_transactions",
    }
    missing = sorted(required - tables)
    if missing:
        raise RuntimeError(f"cannot upgrade WU9R: required table is missing: {missing[0]}")
    obsolete_tables = sorted(OBSOLETE_PARTICIPANT_TABLES & tables)
    if obsolete_tables:
        raise RuntimeError(f"cannot upgrade WU9R: obsolete participant table is present: {obsolete_tables[0]}")

    _validate_columns(inspector, "races", RACE_BASE_COLUMNS)
    _validate_columns(inspector, "race_entries", ENTRY_BASE_COLUMNS)
    _validate_columns(inspector, "bets", BET_BASE_COLUMNS)
    _validate_reference_table_subset(inspector)
    _validate_exact_columns(inspector, "race_conditions", CONDITION_COLUMNS)
    _validate_exact_columns(inspector, "race_results", RESULT_COLUMNS)
    _validate_exact_columns(inspector, "room_point_accounts", POINT_ACCOUNT_COLUMNS)
    _validate_exact_columns(inspector, "room_point_transactions", POINT_TRANSACTION_COLUMNS)
    _validate_primary_key(inspector, "races")
    _validate_primary_key(inspector, "race_entries")
    _validate_primary_key(inspector, "bets")
    for table_name in ("race_conditions", "race_results", "room_point_accounts", "room_point_transactions"):
        _validate_primary_key(inspector, table_name)
    _validate_base_constraints(inspector)

    race_columns = _column_names(inspector, "races")
    entry_columns = _column_names(inspector, "race_entries")
    bet_columns = _column_names(inspector, "bets")

    _reject_unexpected_columns("races", race_columns, set(RACE_BASE_COLUMNS) | RACE_COLUMNS)
    _reject_unexpected_columns("race_entries", entry_columns, set(ENTRY_BASE_COLUMNS) | ENTRY_COLUMNS)
    _reject_unexpected_columns("bets", bet_columns, set(BET_BASE_COLUMNS) | BET_COLUMNS)
    _reject_obsolete_participant_artifacts(inspector)

    race_target = RACE_COLUMNS & race_columns
    entry_target = ENTRY_COLUMNS & entry_columns
    bet_target = BET_COLUMNS & bet_columns
    target_markers = bool(race_target), bool(entry_target), bool(bet_target), AUDIT_TABLE in tables
    if any(target_markers) and not all(target_markers):
        raise RuntimeError("cannot upgrade WU9R: unsupported partial WU9 schema; restore the pre-WU9 snapshot")
    if race_target and race_target != RACE_COLUMNS:
        raise RuntimeError("cannot upgrade WU9R: partial race control columns")
    if entry_target and entry_target != ENTRY_COLUMNS:
        raise RuntimeError("cannot upgrade WU9R: partial race entry snapshot columns")
    if bet_target and bet_target != BET_COLUMNS:
        raise RuntimeError("cannot upgrade WU9R: partial Bet window columns")

    invalid_entry = (
        op.get_bind().execute(sa.text("SELECT id FROM race_entries WHERE entry_number <= 0 LIMIT 1")).first()
    )
    if invalid_entry is not None:
        raise RuntimeError("cannot upgrade WU9R: race entry number must be positive")
    _reject_invalid_room_match_entry_names()

    if all(target_markers):
        _validate_canonical_schema(inspector)
        _validate_semantic_data("canonical", tables)
        return "canonical"

    _validate_runtime_table_constraints(inspector, canonical=False)
    _validate_semantic_data("legacy", tables)
    return "legacy"


def _validate_canonical_schema(inspector: sa.Inspector) -> None:
    _validate_exact_columns(inspector, "races", {**RACE_BASE_COLUMNS, **RACE_TARGET_COLUMNS})
    _validate_exact_columns(inspector, "race_entries", {**ENTRY_BASE_COLUMNS, **ENTRY_TARGET_COLUMNS})
    _validate_exact_columns(inspector, "bets", {**BET_BASE_COLUMNS, **BET_TARGET_COLUMNS})
    _validate_exact_columns(inspector, "race_conditions", CONDITION_COLUMNS)
    _validate_exact_columns(inspector, "race_results", RESULT_COLUMNS)
    _validate_exact_columns(inspector, "room_point_accounts", POINT_ACCOUNT_COLUMNS)
    _validate_exact_columns(inspector, "room_point_transactions", POINT_TRANSACTION_COLUMNS)
    _validate_primary_key(inspector, "races")
    _validate_primary_key(inspector, "race_entries")
    _validate_primary_key(inspector, "bets")
    _validate_base_constraints(inspector)
    _validate_runtime_table_constraints(inspector, canonical=True)
    _require_check_expression(
        inspector,
        "races",
        "ck_races_room_match_status",
        "race_kind <> 'room_match' OR status IN "
        "('setup', 'betting_open', 'betting_closed', 'result_review', "
        "'result_confirmed', 'settled', 'voided')",
    )
    _require_check_expression(
        inspector,
        "race_entries",
        "ck_race_entries_positive_entry_number",
        "entry_number > 0",
    )
    _require_check_expression(
        inspector,
        "races",
        "ck_races_positive_betting_window_version",
        "betting_window_version > 0",
    )
    _require_check_expression(
        inspector,
        "bets",
        "ck_bets_positive_betting_window_version",
        "betting_window_version > 0",
    )
    if not _has_foreign_key(inspector, "race_entries", ("game_account_id",), "game_accounts", ("id",)):
        raise RuntimeError("cannot upgrade WU9R: race entry game-account foreign key is missing")
    if set(_column_names(inspector, AUDIT_TABLE)) != set(AUDIT_COLUMNS):
        raise RuntimeError("cannot upgrade WU9R: partial race operation audit table")
    _validate_columns(inspector, AUDIT_TABLE, AUDIT_COLUMNS)
    _validate_primary_key(inspector, AUDIT_TABLE)
    audit_columns = {column["name"]: column for column in inspector.get_columns(AUDIT_TABLE)}
    if op.get_bind().dialect.name != "sqlite" and audit_columns["id"].get("autoincrement") is not True:
        raise RuntimeError("cannot upgrade WU9R: audit primary key is not auto-incrementing")
    if audit_columns["created_at"].get("default") is None:
        raise RuntimeError("cannot upgrade WU9R: audit created-at default is missing")
    _require_exact_unique_signatures(inspector, AUDIT_TABLE, {("idempotency_key",)})
    _require_exact_foreign_key_signatures(inspector, AUDIT_TABLE, {(("race_id",), "races", ("id",))})
    _require_foreign_key_name(
        inspector,
        AUDIT_TABLE,
        ("race_id",),
        "races",
        ("id",),
        AUDIT_RACE_FK,
    )
    _require_foreign_key_name(
        inspector,
        "race_entries",
        ("game_account_id",),
        "game_accounts",
        ("id",),
        ENTRY_ACCOUNT_FK,
    )
    _require_exact_check_expressions(inspector, AUDIT_TABLE, {})
    _require_default(inspector, "races", "betting_window_version", "1")
    _require_default(inspector, "bets", "betting_window_version", "1")
    if _column_default(inspector, "races", "status") is not None:
        raise RuntimeError("cannot upgrade WU9R: Race status must not have a global server default")


def _validate_base_constraints(inspector: sa.Inspector) -> None:
    required_unique = (
        ("races", ("external_source", "external_race_id")),
        ("race_entries", ("race_id", "entry_number")),
        ("race_entries", ("source_import_record_id",)),
    )
    for table_name, columns in required_unique:
        if not _has_unique(inspector, table_name, columns):
            raise RuntimeError(f"cannot upgrade WU9R: {table_name} unique constraint is missing: {','.join(columns)}")

    required_foreign_keys = (
        ("races", ("event_id",), "game_events", ("id",)),
        ("race_entries", ("race_id",), "races", ("id",)),
        ("race_entries", ("source_import_record_id",), "sheet_import_records", ("id",)),
        ("bets", ("event_id",), "game_events", ("id",)),
        ("bets", ("race_id",), "races", ("id",)),
        ("bets", ("game_account_id",), "game_accounts", ("id",)),
    )
    for table_name, columns, target, target_columns in required_foreign_keys:
        if not _has_foreign_key(inspector, table_name, columns, target, target_columns):
            raise RuntimeError(
                f"cannot upgrade WU9R: {table_name} foreign key is missing: {','.join(columns)}->{target}"
            )

    _require_check_expression(
        inspector,
        "race_entries",
        "ck_race_entries_entry_number_source",
        "entry_number_source IN ('declared', 'payout_result', 'synthetic')",
    )


def _validate_runtime_table_constraints(inspector: sa.Inspector, *, canonical: bool) -> None:
    _validate_exact_numeric_and_integer_shapes(inspector, canonical=canonical)
    _validate_mariadb_character_attributes(inspector, canonical=canonical)
    unique_allowlist = {
        "races": {("external_source", "external_race_id")},
        "race_entries": {("race_id", "entry_number"), ("source_import_record_id",)},
        "race_conditions": {("race_id",)},
        "bets": set(),
        "race_results": {("race_id", "entry_number"), ("source_import_record_id",)},
        "room_point_accounts": {("game_account_id",)},
        "room_point_transactions": {("idempotency_key",)},
    }
    for table_name, expected in unique_allowlist.items():
        _require_exact_unique_signatures(inspector, table_name, expected)

    foreign_key_allowlist = {
        "races": {(("event_id",), "game_events", ("id",))},
        "race_entries": {
            (("race_id",), "races", ("id",)),
            (("source_import_record_id",), "sheet_import_records", ("id",)),
        },
        "race_conditions": {(("race_id",), "races", ("id",))},
        "bets": {
            (("event_id",), "game_events", ("id",)),
            (("race_id",), "races", ("id",)),
            (("game_account_id",), "game_accounts", ("id",)),
        },
        "race_results": {
            (("race_id",), "races", ("id",)),
            (("game_account_id",), "game_accounts", ("id",)),
            (("source_import_record_id",), "sheet_import_records", ("id",)),
        },
        "room_point_accounts": {(("game_account_id",), "game_accounts", ("id",))},
        "room_point_transactions": {
            (("game_account_id",), "game_accounts", ("id",)),
            (("related_bet_id",), "bets", ("id",)),
        },
    }
    if canonical:
        foreign_key_allowlist["race_entries"].add((("game_account_id",), "game_accounts", ("id",)))
    for table_name, expected in foreign_key_allowlist.items():
        _require_exact_foreign_key_signatures(inspector, table_name, expected)

    expected_checks: dict[str, dict[str, str]] = {
        "races": {},
        "race_entries": {
            "ck_race_entries_entry_number_source": "entry_number_source IN ('declared', 'payout_result', 'synthetic')"
        },
        "race_conditions": {},
        "bets": {},
        "race_results": {},
        "room_point_accounts": {},
        "room_point_transactions": {},
    }
    if canonical:
        expected_checks["races"] = {
            "ck_races_room_match_status": "race_kind <> 'room_match' OR status IN "
            "('setup', 'betting_open', 'betting_closed', 'result_review', "
            "'result_confirmed', 'settled', 'voided')",
            "ck_races_positive_betting_window_version": "betting_window_version > 0",
        }
        expected_checks["race_entries"]["ck_race_entries_positive_entry_number"] = "entry_number > 0"
        expected_checks["race_conditions"]["ck_race_conditions_nonnegative_participant_count"] = (
            "participant_count >= 0"
        )
        expected_checks["bets"]["ck_bets_positive_betting_window_version"] = "betting_window_version > 0"
    for table_name, expected in expected_checks.items():
        _require_exact_check_expressions(inspector, table_name, expected)

    _validate_exact_defaults(inspector, canonical=canonical)


def _validate_exact_numeric_and_integer_shapes(inspector: sa.Inspector, *, canonical: bool) -> None:
    rate = next(column for column in inspector.get_columns("race_entries") if column["name"] == "rate")["type"]
    if not isinstance(rate, sa.Numeric) or rate.precision != 10 or rate.scale != 2:
        raise RuntimeError("cannot upgrade WU9R: race_entries.rate numeric shape is invalid")
    if bool(getattr(rate, "unsigned", False)) or bool(getattr(rate, "zerofill", False)):
        raise RuntimeError("cannot upgrade WU9R: race_entries.rate must be signed and not zerofill")
    if op.get_bind().dialect.name == "sqlite":
        return
    bigint_columns = {
        "races": {"id", "event_id"},
        "race_entries": {"id", "race_id", "source_import_record_id"},
        "race_conditions": {"id", "race_id"},
        "bets": {"id", "event_id", "race_id", "game_account_id"},
        "race_results": {"id", "race_id", "game_account_id", "source_import_record_id"},
        "room_point_accounts": {"id", "game_account_id"},
        "room_point_transactions": {"id", "game_account_id", "related_bet_id"},
    }
    if canonical:
        bigint_columns["race_entries"].add("game_account_id")
        bigint_columns[AUDIT_TABLE] = {"id", "race_id"}
    int_columns = {
        "races": {"betting_window_version"} if canonical else set(),
        "race_entries": {
            "entry_number",
            "horse_age",
            "total_count",
            "first_count",
            "second_count",
            "third_count",
        },
        "race_conditions": {"distance", "participant_count"},
        "bets": {"amount", "betting_window_version"} if canonical else {"amount"},
        "race_results": {"entry_number", "rank", "converted_rank"},
        "room_point_accounts": {"balance"},
        "room_point_transactions": {"amount"},
    }
    for table_name, column_names in bigint_columns.items():
        reflected = {column["name"]: column["type"] for column in inspector.get_columns(table_name)}
        for column_name in column_names:
            if not isinstance(reflected[column_name], sa.BigInteger):
                raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} must be BIGINT")
            if bool(getattr(reflected[column_name], "unsigned", False)):
                raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} must be signed")
            if bool(getattr(reflected[column_name], "zerofill", False)):
                raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} must not be zerofill")
    for table_name, column_names in int_columns.items():
        reflected = {column["name"]: column["type"] for column in inspector.get_columns(table_name)}
        for column_name in column_names:
            if type(reflected[column_name]).__name__.upper() not in {"INT", "INTEGER"}:
                raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} must be INT")
            if bool(getattr(reflected[column_name], "unsigned", False)):
                raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} must be signed")
            if bool(getattr(reflected[column_name], "zerofill", False)):
                raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} must not be zerofill")


def _validate_mariadb_character_attributes(inspector: sa.Inspector, *, canonical: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    table_columns = {
        "races": {**RACE_BASE_COLUMNS, **(RACE_TARGET_COLUMNS if canonical else {})},
        "race_entries": {**ENTRY_BASE_COLUMNS, **(ENTRY_TARGET_COLUMNS if canonical else {})},
        "race_conditions": CONDITION_COLUMNS,
        "bets": {**BET_BASE_COLUMNS, **(BET_TARGET_COLUMNS if canonical else {})},
        "race_results": RESULT_COLUMNS,
        "room_point_accounts": POINT_ACCOUNT_COLUMNS,
        "room_point_transactions": POINT_TRANSACTION_COLUMNS,
    }
    if canonical:
        table_columns[AUDIT_TABLE] = AUDIT_COLUMNS
    database_name = op.get_bind().execute(sa.text("SELECT DATABASE()")).scalar_one()
    for table_name, columns in table_columns.items():
        reflected = {column["name"]: column["type"] for column in inspector.get_columns(table_name)}
        for column_name, (family, _nullable, _length) in columns.items():
            if family not in {"string", "text"}:
                continue
            row = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table AND COLUMN_NAME = :column"
                    ),
                    {"schema": database_name, "table": table_name, "column": column_name},
                )
                .one()
            )
            if tuple(value.lower() if value is not None else None for value in row) != (
                MARIADB_CHARACTER_SET,
                MARIADB_COLLATION,
            ):
                raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} charset or collation is invalid")
            if bool(getattr(reflected[column_name], "binary", False)):
                raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} binary semantics are invalid")


def _validate_exact_defaults(inspector: sa.Inspector, *, canonical: bool) -> None:
    table_columns = {
        "races": set(RACE_BASE_COLUMNS) | (set(RACE_TARGET_COLUMNS) if canonical else set()),
        "race_entries": set(ENTRY_BASE_COLUMNS) | (set(ENTRY_TARGET_COLUMNS) if canonical else set()),
        "race_conditions": set(CONDITION_COLUMNS),
        "bets": set(BET_BASE_COLUMNS) | (set(BET_TARGET_COLUMNS) if canonical else set()),
        "race_results": set(RESULT_COLUMNS),
        "room_point_accounts": set(POINT_ACCOUNT_COLUMNS),
        "room_point_transactions": set(POINT_TRANSACTION_COLUMNS),
    }
    if canonical:
        table_columns[AUDIT_TABLE] = set(AUDIT_COLUMNS)
    expected: dict[tuple[str, str], set[str | None]] = {}
    for table_name, column_names in table_columns.items():
        for column_name in column_names:
            expected[(table_name, column_name)] = {None}
    for table_name in ("races", "race_entries", "race_conditions", "race_results", "bets", "room_point_accounts"):
        expected[(table_name, "created_at")] = {"current_timestamp"}
        expected[(table_name, "updated_at")] = {"current_timestamp"}
    expected[("room_point_transactions", "created_at")] = {"current_timestamp"}
    expected[("race_entries", "entry_number_source")] = {"declared"}
    for column_name in ("is_betting_excluded", "is_rating_excluded", "is_result_void"):
        expected[("race_results", column_name)] = {"0", "false"}
    if canonical:
        expected[("races", "betting_window_version")] = {"1"}
        expected[("bets", "betting_window_version")] = {"1"}
        expected[(AUDIT_TABLE, "created_at")] = {"current_timestamp"}
    else:
        expected[("races", "status")] = {None, "scheduled"}
        expected[("race_entries", "entry_kind")] = {None, "room_match"}

    for (table_name, column_name), allowed in expected.items():
        actual = _normalized_default(_column_default(inspector, table_name, column_name))
        if actual not in allowed:
            raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} default allowlist mismatch")


def _normalized_default(value: object) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", "", str(value).strip().lower().replace("'", "").replace('"', ""))
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1]
    if normalized in {"current_timestamp", "current_timestamp()"}:
        return "current_timestamp"
    return normalized


def _require_exact_unique_signatures(
    inspector: sa.Inspector,
    table_name: str,
    expected: set[tuple[str, ...]],
) -> None:
    actual = {tuple(item.get("column_names") or ()) for item in inspector.get_unique_constraints(table_name)}
    for item in inspector.get_indexes(table_name):
        if not item.get("unique"):
            continue
        columns = tuple(item.get("column_names") or ())
        if not columns or any(not isinstance(column, str) or not column for column in columns):
            raise RuntimeError(f"cannot upgrade WU9R: {table_name} UNIQUE index expression is unsupported")
        expressions = tuple(item.get("expressions") or ())
        if expressions and expressions != columns:
            raise RuntimeError(f"cannot upgrade WU9R: {table_name} UNIQUE index expression is unsupported")
        if item.get("include_columns"):
            raise RuntimeError(f"cannot upgrade WU9R: {table_name} UNIQUE index include columns are unsupported")
        dialect_options = item.get("dialect_options") or {}
        if _has_meaningful_index_option(dialect_options):
            raise RuntimeError(f"cannot upgrade WU9R: {table_name} UNIQUE index options are unsupported")
        actual.add(columns)
    if actual != expected:
        raise RuntimeError(f"cannot upgrade WU9R: {table_name} UNIQUE allowlist mismatch")


def _has_meaningful_index_option(value: object) -> bool:
    if isinstance(value, dict):
        return any(_has_meaningful_index_option(item) for item in value.values())
    if isinstance(value, (tuple, list, set)):
        return any(_has_meaningful_index_option(item) for item in value)
    return value not in {None, "", 0}


def _require_exact_foreign_key_signatures(
    inspector: sa.Inspector,
    table_name: str,
    expected: set[tuple[tuple[str, ...], str, tuple[str, ...]]],
) -> None:
    actual = set()
    for item in inspector.get_foreign_keys(table_name):
        referred_schema = item.get("referred_schema")
        if referred_schema not in {None, inspector.default_schema_name}:
            raise RuntimeError(f"cannot upgrade WU9R: {table_name} foreign key must use the current schema")
        options = item.get("options") or {}
        ondelete = _normalized_referential_action(options.get("ondelete"))
        onupdate = _normalized_referential_action(options.get("onupdate"))
        if ondelete != "restrict" or onupdate != "restrict":
            raise RuntimeError(f"cannot upgrade WU9R: {table_name} foreign-key action allowlist mismatch")
        if (
            options.get("match") not in {None, "NONE"}
            or options.get("deferrable") not in {None, False}
            or options.get("initially") is not None
        ):
            raise RuntimeError(f"cannot upgrade WU9R: {table_name} foreign-key option allowlist mismatch")
        actual.add(
            (
                tuple(item.get("constrained_columns") or ()),
                str(item.get("referred_table")),
                tuple(item.get("referred_columns") or ()),
            )
        )
    if actual != expected:
        raise RuntimeError(f"cannot upgrade WU9R: {table_name} foreign-key allowlist mismatch")


def _require_foreign_key_name(
    inspector: sa.Inspector,
    table_name: str,
    columns: tuple[str, ...],
    referred_table: str,
    referred_columns: tuple[str, ...],
    expected_name: str,
) -> None:
    actual_name = _matching_foreign_key_name(
        inspector,
        table_name,
        columns,
        referred_table,
        referred_columns,
    )
    if actual_name != expected_name:
        raise RuntimeError(f"cannot upgrade WU9R: {table_name} foreign-key name allowlist mismatch")


def _normalized_referential_action(value: object) -> str:
    normalized = str(value or "RESTRICT").strip().upper()
    return "restrict" if normalized in {"RESTRICT", "NO ACTION"} else normalized.lower()


def _require_exact_check_expressions(
    inspector: sa.Inspector,
    table_name: str,
    expected: dict[str, str],
) -> None:
    actual = {
        str(item.get("name")): _normalize_sql(item.get("sqltext"))
        for item in inspector.get_check_constraints(table_name)
    }
    normalized_expected = {name: _normalize_sql(value) for name, value in expected.items()}
    if actual != normalized_expected:
        raise RuntimeError(f"cannot upgrade WU9R: {table_name} CHECK allowlist mismatch")


def _reject_obsolete_participant_artifacts(inspector: sa.Inspector) -> None:
    entry_columns = _column_names(inspector, "race_entries")
    obsolete_columns = sorted(entry_columns & OBSOLETE_PARTICIPANT_COLUMNS)
    if obsolete_columns:
        raise RuntimeError(f"cannot upgrade WU9R: obsolete participant column is present: {obsolete_columns[0]}")
    for index in inspector.get_indexes("race_entries"):
        columns = set(index.get("column_names") or ())
        if columns & OBSOLETE_PARTICIPANT_COLUMNS or columns == {"race_id", "game_account_id"}:
            raise RuntimeError("cannot upgrade WU9R: obsolete participant index is present")
    if _has_unique(inspector, "race_entries", ("race_id", "game_account_id")):
        raise RuntimeError("cannot upgrade WU9R: obsolete participant identity constraint is present")


def _validate_columns(
    inspector: sa.Inspector,
    table_name: str,
    expected: dict[str, tuple[str, bool, int | None]],
) -> None:
    columns = {column["name"]: column for column in inspector.get_columns(table_name)}
    missing = sorted(set(expected) - set(columns))
    if missing:
        raise RuntimeError(f"cannot upgrade WU9R: {table_name} column is missing: {missing[0]}")
    for name, (family, nullable, length) in expected.items():
        column = columns[name]
        if column.get("computed") is not None:
            raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{name} must not be generated or computed")
        if column.get("identity") is not None:
            raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{name} must not use identity metadata")
        if name != "id" and bool(column["nullable"]) is not nullable:
            raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{name} nullability is invalid")
        if not _type_matches(column["type"], family=family, length=length):
            raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{name} type is invalid")
        if family == "integer" and bool(getattr(column["type"], "unsigned", False)):
            raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{name} must be signed")


def _validate_exact_columns(
    inspector: sa.Inspector,
    table_name: str,
    expected: dict[str, tuple[str, bool, int | None]],
) -> None:
    _validate_columns(inspector, table_name, expected)
    _reject_unexpected_columns(table_name, _column_names(inspector, table_name), set(expected))


def _reject_unexpected_columns(table_name: str, actual: set[str], allowed: set[str]) -> None:
    unexpected = sorted(actual - allowed)
    if unexpected:
        raise RuntimeError(f"cannot upgrade WU9R: unsupported {table_name} column is present: {unexpected[0]}")


def _type_matches(actual: sa.types.TypeEngine, *, family: str, length: int | None) -> bool:
    if family == "integer":
        return isinstance(actual, sa.Integer)
    if family == "string":
        return isinstance(actual, sa.String) and getattr(actual, "length", None) == length
    if family == "text":
        return isinstance(actual, sa.Text) and (
            op.get_bind().dialect.name == "sqlite" or type(actual).__name__.upper() == "TEXT"
        )
    if family == "datetime":
        return (
            isinstance(actual, sa.DateTime)
            and type(actual).__name__.upper() != "TIMESTAMP"
            and (op.get_bind().dialect.name == "sqlite" or getattr(actual, "fsp", None) in {None, 0})
        )
    if family == "numeric":
        return isinstance(actual, sa.Numeric)
    if family == "json":
        return isinstance(actual, sa.JSON) or type(actual).__name__.upper() in {"JSON", "LONGTEXT"}
    if family == "boolean":
        if op.get_bind().dialect.name == "sqlite":
            return isinstance(actual, (sa.Boolean, sa.Integer))
        return type(actual).__name__.upper() == "TINYINT" and getattr(actual, "display_width", None) == 1
    return False


def _validate_primary_key(inspector: sa.Inspector, table_name: str, *, generated: bool = True) -> None:
    primary_key = inspector.get_pk_constraint(table_name)
    if tuple(primary_key.get("constrained_columns") or ()) != ("id",):
        raise RuntimeError(f"cannot upgrade WU9R: {table_name} primary key is invalid")
    if not generated:
        return
    identifier = next(column for column in inspector.get_columns(table_name) if column["name"] == "id")
    if op.get_bind().dialect.name == "sqlite":
        if type(identifier["type"]).__name__.upper() != "INTEGER":
            raise RuntimeError(f"cannot upgrade WU9R: {table_name}.id must be a rowid-generating INTEGER PRIMARY KEY")
        _validate_sqlite_rowid_semantics(table_name)
    elif identifier.get("autoincrement") is not True:
        raise RuntimeError(f"cannot upgrade WU9R: {table_name}.id must be AUTO_INCREMENT")


def _validate_sqlite_rowid_semantics(table_name: str) -> None:
    create_sql = (
        op.get_bind()
        .execute(
            sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table_name"),
            {"table_name": table_name},
        )
        .scalar_one_or_none()
    )
    if create_sql is None or re.search(r"\bWITHOUT\s+ROWID\b", str(create_sql), flags=re.IGNORECASE):
        raise RuntimeError(f"cannot upgrade WU9R: {table_name} must use SQLite rowid semantics")
    primary_key_indexes = [
        row
        for row in op.get_bind().exec_driver_sql(f'PRAGMA index_list("{table_name}")').mappings()
        if str(row.get("origin") or "").lower() == "pk"
    ]
    if primary_key_indexes:
        raise RuntimeError(f"cannot upgrade WU9R: {table_name}.id must alias the SQLite rowid")


def _validate_reference_table_subset(inspector: sa.Inspector) -> None:
    for table_name in ("game_accounts", "game_events", "sheet_import_records"):
        _validate_primary_key(inspector, table_name, generated=False)
        identifier = next(column for column in inspector.get_columns(table_name) if column["name"] == "id")
        if op.get_bind().dialect.name != "sqlite" and bool(identifier["nullable"]):
            raise RuntimeError(f"cannot upgrade WU9R: {table_name}.id nullability is invalid")
        if op.get_bind().dialect.name != "sqlite" and not isinstance(identifier["type"], sa.BigInteger):
            raise RuntimeError(f"cannot upgrade WU9R: {table_name}.id must be BIGINT")
        if bool(getattr(identifier["type"], "unsigned", False)):
            raise RuntimeError(f"cannot upgrade WU9R: {table_name}.id must be signed")


def _require_check_expression(
    inspector: sa.Inspector,
    table_name: str,
    name: str,
    expected_sql: str,
) -> None:
    for constraint in inspector.get_check_constraints(table_name):
        if constraint.get("name") == name and _normalize_sql(constraint.get("sqltext")) == _normalize_sql(expected_sql):
            return
    raise RuntimeError(f"cannot upgrade WU9R: {table_name} check constraint is missing or ineffective: {name}")


def _normalize_sql(value: object) -> str:
    text = str(value or "").lower().replace("`", "").replace('"', "").replace("_utf8mb4", "")
    text = re.sub(r"\s+", "", text)
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return text


def _column_default(inspector: sa.Inspector, table_name: str, column_name: str) -> object:
    return next(column for column in inspector.get_columns(table_name) if column["name"] == column_name).get("default")


def _require_default(inspector: sa.Inspector, table_name: str, column_name: str, expected: str) -> None:
    actual = _column_default(inspector, table_name, column_name)
    normalized = re.sub(r"[\s'\"()]", "", str(actual or "")).lower()
    if normalized != expected:
        raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} default is invalid")


def _require_no_default(inspector: sa.Inspector, table_name: str, column_name: str) -> None:
    if _column_default(inspector, table_name, column_name) is not None:
        raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} must not have a server default")


def _require_false_default(inspector: sa.Inspector, table_name: str, column_name: str) -> None:
    actual = _column_default(inspector, table_name, column_name)
    normalized = re.sub(r"[\s'\"()]", "", str(actual or "")).lower()
    if normalized not in {"0", "false"}:
        raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} default is invalid")


def _require_allowed_default(
    inspector: sa.Inspector,
    table_name: str,
    column_name: str,
    expected: set[str | None],
) -> None:
    actual = _column_default(inspector, table_name, column_name)
    normalized = None if actual is None else re.sub(r"[\s'\"()]", "", str(actual)).lower()
    if normalized not in expected:
        raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} default is invalid")


def _require_current_timestamp_default(inspector: sa.Inspector, table_name: str, column_name: str) -> None:
    actual = _column_default(inspector, table_name, column_name)
    normalized = re.sub(r"[\s'\"()]", "", str(actual or "")).lower()
    if normalized not in {"current_timestamp", "current_timestamp(6)"}:
        raise RuntimeError(f"cannot upgrade WU9R: {table_name}.{column_name} timestamp default is invalid")


def _reject_invalid_room_match_entry_names() -> None:
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT e.id FROM race_entries e JOIN races r ON r.id = e.race_id "
                "WHERE r.race_kind = 'room_match' AND e.entry_kind = 'room_match' "
                "AND (e.player_name IS NULL OR TRIM(e.player_name) = '') LIMIT 1"
            )
        )
        .first()
    )
    if invalid is not None:
        raise RuntimeError("cannot upgrade WU9R: room-match entry display name requires manual correction")


STATUS_ALIASES = {
    "setup": "setup",
    "scheduled": "setup",
    "draft": "setup",
    "betting_open": "betting_open",
    "open": "betting_open",
    "betting_closed": "betting_closed",
    "closed": "betting_closed",
    "running": "betting_closed",
    "result_review": "result_review",
    "result_confirmed": "result_confirmed",
    "finished": "result_confirmed",
    "completed": "result_confirmed",
    "settled": "settled",
    "voided": "voided",
    "void": "voided",
    "cancelled": "voided",
}


def _validate_semantic_data(schema_mode: str, tables: set[str]) -> None:
    allowed = ROOM_MATCH_STATUSES if schema_mode == "canonical" else LEGACY_ROOM_MATCH_STATUSES
    _reject_unknown_statuses(tuple(allowed))
    _reject_mixed_room_match_entry_kinds()
    time_columns = (
        "r.betting_window_version, r.betting_opened_at, r.betting_closed_at"
        if schema_mode == "canonical"
        else "1 AS betting_window_version, NULL AS betting_opened_at, NULL AS betting_closed_at"
    )
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT r.id, r.status, "
                + time_columns
                + ", (SELECT COUNT(*) FROM race_entries e WHERE e.race_id = r.id "
                "AND e.entry_kind = 'room_match') AS entry_count, "
                "(SELECT COUNT(*) FROM bets b WHERE b.race_id = r.id) AS bet_count, "
                "(SELECT COUNT(*) FROM bets b WHERE b.race_id = r.id AND b.status = 'active') AS active_bet_count, "
                "(SELECT COUNT(*) FROM race_results rr WHERE rr.race_id = r.id) AS result_count "
                "FROM races r WHERE r.race_kind = 'room_match' ORDER BY r.id"
            )
        )
        .mappings()
    )
    for row in rows:
        status = STATUS_ALIASES[row["status"]]
        entries = int(row["entry_count"])
        bets = int(row["bet_count"])
        active_bets = int(row["active_bet_count"])
        results = int(row["result_count"])
        invalid = False
        if status == "setup":
            invalid = bets != 0 or results != 0
        elif status in {"betting_open", "betting_closed"}:
            invalid = entries < 1 or results != 0
        elif status == "result_review":
            invalid = True
        elif status == "result_confirmed":
            invalid = entries < 1 or results < 1
        elif status == "settled":
            invalid = entries < 1 or results < 1 or active_bets != 0
        elif status == "voided":
            invalid = bets != 0
        if invalid:
            label = (
                "canonical race lifecycle data is inconsistent"
                if schema_mode == "canonical"
                else "legacy race status requires manual reconciliation"
            )
            raise RuntimeError(f"cannot upgrade WU9R: {label}; race_id={row['id']} status={row['status']}")

        if schema_mode == "canonical":
            opened_at = row["betting_opened_at"]
            closed_at = row["betting_closed_at"]
            invalid_time = (
                (status == "setup" and (opened_at is not None or closed_at is not None))
                or (status == "betting_open" and (opened_at is None or closed_at is not None))
                or (
                    status in {"betting_closed", "result_review", "result_confirmed", "settled"}
                    and (opened_at is None or closed_at is None)
                )
                or (closed_at is not None and (opened_at is None or closed_at < opened_at))
            )
            if invalid_time:
                raise RuntimeError(
                    f"cannot upgrade WU9R: canonical race lifecycle data is inconsistent; race_id={row['id']}"
                )

    _reject_result_entry_membership()
    _reject_bet_entry_membership()
    invalid_condition = (
        op.get_bind().execute(sa.text("SELECT id FROM race_conditions WHERE participant_count < 0 LIMIT 1")).first()
    )
    if invalid_condition is not None:
        raise RuntimeError("cannot upgrade WU9R: RaceCondition participant count must be nonnegative")
    _reject_invalid_room_match_entry_names()
    _reject_window_version_semantics() if schema_mode == "canonical" else None
    _reject_participant_count_drift(tables)


def _reject_mixed_room_match_entry_kinds() -> None:
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT e.id FROM race_entries e JOIN races r ON r.id = e.race_id "
                "WHERE r.race_kind = 'room_match' AND e.entry_kind <> 'room_match' LIMIT 1"
            )
        )
        .first()
    )
    if invalid is not None:
        raise RuntimeError("cannot upgrade WU9R: room-match Race contains a non-room-match entry")


def _reject_result_entry_membership() -> None:
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT rr.id FROM race_results rr JOIN races r ON r.id = rr.race_id "
                "WHERE r.race_kind = 'room_match' AND NOT EXISTS ("
                "SELECT 1 FROM race_entries e WHERE e.race_id = rr.race_id "
                "AND e.entry_kind = 'room_match' AND e.entry_number = rr.entry_number"
                ") LIMIT 1"
            )
        )
        .first()
    )
    if invalid is not None:
        raise RuntimeError("cannot upgrade WU9R: RaceResult references an entry outside the final snapshot")


def _reject_bet_entry_membership() -> None:
    entry_rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT e.race_id, e.entry_number FROM race_entries e "
                "JOIN races r ON r.id = e.race_id "
                "WHERE r.race_kind = 'room_match' AND e.entry_kind = 'room_match' "
                "ORDER BY e.race_id, e.entry_number"
            )
        )
        .all()
    )
    entry_numbers: dict[int, set[int]] = {}
    for race_id, entry_number in entry_rows:
        entry_numbers.setdefault(int(race_id), set()).add(int(entry_number))

    bets = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT b.id, b.race_id, b.betting_mode, b.bet_type, b.numbers "
                "FROM bets b JOIN races r ON r.id = b.race_id "
                "WHERE r.race_kind = 'room_match' ORDER BY b.id"
            )
        )
        .mappings()
    )
    expected_counts = {"win": 1, "quinella": 2, "trio": 3}
    for bet in bets:
        try:
            numbers = _decode_bet_numbers(bet["numbers"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("cannot upgrade WU9R: room-match Bet numbers JSON is invalid") from exc
        bet_type = str(bet["bet_type"])
        expected_count = expected_counts.get(bet_type)
        invalid_shape = (
            str(bet["betting_mode"]) != "room_match"
            or expected_count is None
            or len(numbers) != expected_count
            or any(not isinstance(number, int) or isinstance(number, bool) or number <= 0 for number in numbers)
            or len(set(numbers)) != len(numbers)
            or (bet_type in {"quinella", "trio"} and numbers != sorted(numbers))
        )
        if invalid_shape:
            raise RuntimeError("cannot upgrade WU9R: room-match Bet payload is invalid")
        available = entry_numbers.get(int(bet["race_id"]), set())
        if any(number not in available for number in numbers):
            raise RuntimeError("cannot upgrade WU9R: room-match Bet references an entry outside the final snapshot")


def _decode_bet_numbers(value: object) -> list[object]:
    decoded = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
    if not isinstance(decoded, list):
        raise TypeError("Bet numbers must be a JSON array")
    return decoded


def _reject_window_version_semantics() -> None:
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT b.id FROM bets b JOIN races r ON r.id = b.race_id "
                "WHERE b.betting_mode = 'room_match' AND ("
                "r.betting_window_version <> 1 OR b.betting_window_version <> 1 OR "
                "b.betting_window_version > r.betting_window_version) LIMIT 1"
            )
        )
        .first()
    )
    if invalid is not None:
        raise RuntimeError("cannot upgrade WU9R: Bet window version is inconsistent with its Race")
    versioned_race = (
        op.get_bind()
        .execute(sa.text("SELECT id FROM races WHERE race_kind = 'room_match' AND betting_window_version <> 1 LIMIT 1"))
        .first()
    )
    if versioned_race is not None:
        raise RuntimeError("cannot upgrade WU9R: initial Race window version must be 1")


def _reject_participant_count_drift(tables: set[str]) -> None:
    if "race_conditions" not in tables:
        return
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT rc.id FROM race_conditions rc JOIN races r ON r.id = rc.race_id "
                "WHERE r.race_kind = 'room_match' "
                "AND rc.participant_count <> (SELECT COUNT(*) FROM race_entries e "
                "WHERE e.race_id = r.id AND e.entry_kind = 'room_match') LIMIT 1"
            )
        )
        .first()
    )
    if invalid is not None:
        raise RuntimeError("cannot upgrade WU9R: RaceCondition participant count differs from final entries")


def _reject_unknown_statuses(allowed: tuple[str, ...]) -> None:
    placeholders = ", ".join(f"'{value}'" for value in allowed)
    unknown = (
        op.get_bind()
        .execute(
            sa.text(f"SELECT id FROM races WHERE race_kind = 'room_match' AND status NOT IN ({placeholders}) LIMIT 1")
        )
        .first()
    )
    if unknown is not None:
        raise RuntimeError("cannot upgrade WU9R: unknown room-match race status")


def _map_legacy_room_match_statuses() -> None:
    op.execute(
        sa.text(
            "UPDATE races SET status = CASE status "
            "WHEN 'scheduled' THEN 'setup' "
            "WHEN 'draft' THEN 'setup' "
            "WHEN 'open' THEN 'betting_open' "
            "WHEN 'closed' THEN 'betting_closed' "
            "WHEN 'running' THEN 'betting_closed' "
            "WHEN 'finished' THEN 'result_confirmed' "
            "WHEN 'completed' THEN 'result_confirmed' "
            "WHEN 'void' THEN 'voided' "
            "WHEN 'cancelled' THEN 'voided' ELSE status END "
            "WHERE race_kind = 'room_match'"
        )
    )


def _upgrade_race_entries() -> None:
    inspector = sa.inspect(op.get_bind())
    with op.batch_alter_table("race_entries") as batch:
        batch.add_column(sa.Column("game_account_id", BIGINT, nullable=True))
        batch.alter_column(
            "entry_kind",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default=None,
        )
    inspector = sa.inspect(op.get_bind())
    with op.batch_alter_table("race_entries") as batch:
        batch.create_foreign_key(
            ENTRY_ACCOUNT_FK,
            "game_accounts",
            ["game_account_id"],
            ["id"],
        )
        if not _has_check(inspector, "race_entries", "ck_race_entries_positive_entry_number"):
            batch.create_check_constraint("positive_entry_number", "entry_number > 0")


def _upgrade_race_conditions() -> None:
    inspector = sa.inspect(op.get_bind())
    with op.batch_alter_table("race_conditions") as batch:
        if not _has_check(inspector, "race_conditions", "ck_race_conditions_nonnegative_participant_count"):
            batch.create_check_constraint("nonnegative_participant_count", "participant_count >= 0")


def _upgrade_races() -> None:
    inspector = sa.inspect(op.get_bind())
    with op.batch_alter_table("races") as batch:
        batch.add_column(sa.Column("betting_window_version", sa.Integer(), server_default=sa.text("1"), nullable=False))
        batch.add_column(sa.Column("betting_opened_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("betting_closed_at", sa.DateTime(timezone=True), nullable=True))
        batch.alter_column(
            "status",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default=None,
        )
        if not _has_check(inspector, "races", "ck_races_room_match_status"):
            batch.create_check_constraint(
                "room_match_status",
                "race_kind <> 'room_match' OR status IN "
                "('setup', 'betting_open', 'betting_closed', 'result_review', "
                "'result_confirmed', 'settled', 'voided')",
            )
        batch.create_check_constraint("positive_betting_window_version", "betting_window_version > 0")
    op.execute(
        sa.text(
            "UPDATE races SET betting_opened_at = created_at "
            "WHERE race_kind = 'room_match' AND status = 'betting_open' AND betting_opened_at IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE races SET betting_opened_at = created_at, "
            "betting_closed_at = CASE WHEN updated_at >= created_at THEN updated_at ELSE created_at END "
            "WHERE race_kind = 'room_match' AND status IN "
            "('betting_closed', 'result_review', 'result_confirmed', 'settled')"
        )
    )


def _upgrade_bets() -> None:
    with op.batch_alter_table("bets") as batch:
        batch.add_column(sa.Column("betting_window_version", sa.Integer(), server_default=sa.text("1"), nullable=False))
        batch.create_check_constraint("positive_betting_window_version", "betting_window_version > 0")


def _create_audit_table() -> None:
    op.create_table(
        AUDIT_TABLE,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("race_id", BIGINT, nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("actor_discord_user_id", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["race_id"], ["races.id"], name=AUDIT_RACE_FK),
        sa.UniqueConstraint("idempotency_key", name="uq_race_operation_audits_idempotency_key"),
    )


def _reject_lossy_downgrade(inspector: sa.Inspector, tables: set[str]) -> None:
    if AUDIT_TABLE in tables:
        audit = op.get_bind().execute(sa.text(f"SELECT id FROM {AUDIT_TABLE} LIMIT 1")).first()
        if audit is not None:
            raise RuntimeError("cannot downgrade WU9R after race operations; restore the pre-migration snapshot")
    if "race_entries" in tables and "game_account_id" in _column_names(inspector, "race_entries"):
        linked = (
            op.get_bind()
            .execute(sa.text("SELECT id FROM race_entries WHERE game_account_id IS NOT NULL LIMIT 1"))
            .first()
        )
        if linked is not None:
            raise RuntimeError("cannot downgrade WU9R linked entries; restore the pre-migration snapshot")
    if "races" in tables:
        if "betting_window_version" in _column_names(inspector, "races"):
            versioned_race = (
                op.get_bind().execute(sa.text("SELECT id FROM races WHERE betting_window_version <> 1 LIMIT 1")).first()
            )
            if versioned_race is not None:
                raise RuntimeError("cannot downgrade WU9R versioned races; restore the pre-migration snapshot")
        irreversible = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT id FROM races WHERE race_kind = 'room_match' AND status IN "
                    "('result_review', 'result_confirmed', 'settled', 'voided') LIMIT 1"
                )
            )
            .first()
        )
        if irreversible is not None:
            raise RuntimeError("cannot downgrade WU9R result lifecycle; restore the pre-migration snapshot")
    if "bets" in tables and "betting_window_version" in _column_names(inspector, "bets"):
        versioned_bet = (
            op.get_bind().execute(sa.text("SELECT id FROM bets WHERE betting_window_version <> 1 LIMIT 1")).first()
        )
        if versioned_bet is not None:
            raise RuntimeError("cannot downgrade WU9R versioned Bets; restore the pre-migration snapshot")


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _has_unique(inspector: sa.Inspector, table_name: str, columns: tuple[str, ...]) -> bool:
    constraint_match = any(
        tuple(item.get("column_names") or ()) == columns for item in inspector.get_unique_constraints(table_name)
    )
    index_match = any(
        item.get("unique") and tuple(item.get("column_names") or ()) == columns
        for item in inspector.get_indexes(table_name)
    )
    return constraint_match or index_match


def _has_check(inspector: sa.Inspector, table_name: str, name: str) -> bool:
    return any(item.get("name") == name for item in inspector.get_check_constraints(table_name))


def _has_foreign_key(
    inspector: sa.Inspector,
    table_name: str,
    columns: tuple[str, ...],
    referred_table: str,
    referred_columns: tuple[str, ...],
) -> bool:
    return any(
        tuple(item["constrained_columns"]) == columns
        and item["referred_table"] == referred_table
        and tuple(item["referred_columns"]) == referred_columns
        for item in inspector.get_foreign_keys(table_name)
    )


def _matching_foreign_key_name(
    inspector: sa.Inspector,
    table_name: str,
    columns: tuple[str, ...],
    referred_table: str,
    referred_columns: tuple[str, ...],
) -> str | None:
    for item in inspector.get_foreign_keys(table_name):
        if (
            tuple(item.get("constrained_columns") or ()) == columns
            and item.get("referred_table") == referred_table
            and tuple(item.get("referred_columns") or ()) == referred_columns
        ):
            name = item.get("name")
            return str(name) if name is not None else None
    return None
