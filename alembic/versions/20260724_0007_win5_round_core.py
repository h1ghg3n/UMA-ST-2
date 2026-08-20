"""replace the WIN5 prototype with the WU13A round core

Revision ID: 20260724_0007
Revises: 20260716_0006
Create Date: 2026-07-24 00:07:00.000000

The only supported direct input is the normal empty WIN5 prototype at Alembic
revision 20260716_0006. A fresh database reaches that revision through the full
chain before this upgrade runs. Target-shaped, partially applied, hybrid, and
manually assembled inputs fail with UNSUPPORTED_DATABASE_SHAPE before the first
DDL. The migration trusts the predecessor revision's schema contract and checks
only the objects and columns used directly by this migration.

This revision is stop-the-world and forward-only. It does not resume a failed
DDL sequence or repair a database in place. Production recovery restores the
verified pre-deployment MariaDB backup and reruns the corrected migration.
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260724_0007"
down_revision: str | Sequence[str] | None = "20260716_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
PREVIOUS_REVISION = "20260716_0006"

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
MARIADB_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}
WIN5_TABLES = (
    "win5_score_events",
    "win5_scores",
    "win5_judgements",
    "win5_results",
    "win5_picks",
    "win5_entries",
    "win5_rounds",
    "win5_seasons",
)
AUDIT_TABLE = "win5_operation_audits"

SEASON_OLD = {
    "id",
    "name",
    "starts_at",
    "ends_at",
    "status",
    "created_at",
    "updated_at",
}
ROUND_OLD = {
    "id",
    "season_id",
    "event_id",
    "race_id",
    "round_number",
    "round_label",
    "status",
    "opens_at",
    "closes_at",
    "created_at",
    "updated_at",
}
ENTRY_OLD = {
    "id",
    "season_id",
    "round_id",
    "event_id",
    "game_account_id",
    "status",
    "cancelled_at",
    "created_at",
    "updated_at",
}
PICK_OLD = {"id", "win5_entry_id", "race_id", "pick_order", "entry_number", "created_at"}
UNCHANGED_COLUMNS = {
    "win5_results": {
        "id",
        "round_id",
        "event_id",
        "season_id",
        "race_id",
        "result_order",
        "created_at",
        "updated_at",
    },
    "win5_judgements": {
        "id",
        "win5_entry_id",
        "season_id",
        "hit_count",
        "score_delta",
        "judgement_detail_json",
        "judged_at",
        "judged_by_discord_user_id",
    },
    "win5_scores": {"id", "season_id", "game_account_id", "score", "created_at", "updated_at"},
    "win5_score_events": {
        "id",
        "season_id",
        "game_account_id",
        "win5_entry_id",
        "score_delta",
        "reason",
        "created_at",
    },
}
DIRECT_REFERENCE_COLUMNS = {
    "game_accounts": {"id"},
    "game_events": {"id"},
    "races": {"id"},
}
TARGET_ONLY_SCHEMA_MARKER_NAMES = frozenset(
    {
        "uq_win5_seasons_active_marker",
        "uq_win5_rounds_season_round_number",
        "uq_win5_rounds_race_id",
        "uq_win5_entries_accepted_prediction",
        "uq_win5_entries_idempotency_key",
        "uq_win5_picks_entry_order",
        "uq_win5_picks_entry_number",
        "uq_win5_operation_audits_idempotency_key",
        "ck_win5_seasons_win5_season_status",
        "ck_win5_seasons_win5_season_active_marker",
        "ck_win5_rounds_positive_win5_round_number",
        "ck_win5_rounds_win5_round_status",
        "ck_win5_entries_win5_prediction_tier",
        "ck_win5_entries_win5_entry_status",
        "ck_win5_entries_win5_entry_state",
        "ck_win5_picks_positive_win5_pick_order",
        "ck_win5_picks_positive_win5_entry_number",
    }
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    _require_previous_revision()
    _require_supported_previous_shape(inspector)
    _require_direct_reference_columns(inspector)
    _reject_prototype_rows()

    _drop_prototype_tables()
    _create_target_tables()

    fresh = sa.inspect(op.get_bind())
    _validate_schema(fresh, target=True)
    _reject_prototype_rows()


def downgrade() -> None:
    raise RuntimeError("WU13A downgrade is unsupported; restore the verified pre-deployment database snapshot")


def _require_previous_revision() -> None:
    revisions = tuple(
        str(row[0]) for row in op.get_bind().execute(sa.text("SELECT version_num FROM alembic_version")).all()
    )
    if revisions != (PREVIOUS_REVISION,):
        _unsupported_database_shape(f"expected Alembic revision {PREVIOUS_REVISION}; found {list(revisions)}")


def _require_supported_previous_shape(inspector: sa.Inspector) -> None:
    tables = set(inspector.get_table_names())
    missing = set(WIN5_TABLES) - tables
    if missing:
        _unsupported_database_shape(f"required previous-head WIN5 tables are missing: {sorted(missing)}")
    if AUDIT_TABLE in tables:
        _unsupported_database_shape(f"0007 target object already exists: {AUDIT_TABLE}")
    _reject_target_only_schema_markers(inspector)

    actual = {
        "win5_seasons": _column_names(inspector, "win5_seasons"),
        "win5_rounds": _column_names(inspector, "win5_rounds"),
        "win5_entries": _column_names(inspector, "win5_entries"),
        "win5_picks": _column_names(inspector, "win5_picks"),
    }
    old = {
        "win5_seasons": SEASON_OLD,
        "win5_rounds": ROUND_OLD,
        "win5_entries": ENTRY_OLD,
        "win5_picks": PICK_OLD,
    }
    unchanged = all(_column_names(inspector, table) == columns for table, columns in UNCHANGED_COLUMNS.items())
    if actual != old or not unchanged:
        _unsupported_database_shape("partial, target-shaped, or hybrid WIN5 schema")


def _reject_target_only_schema_markers(inspector: sa.Inspector) -> None:
    for table_name in WIN5_TABLES:
        reflected_names = {
            str(item.get("name"))
            for collection in (
                inspector.get_unique_constraints(table_name),
                inspector.get_indexes(table_name),
                inspector.get_check_constraints(table_name),
            )
            for item in collection
            if item.get("name") is not None
        }
        target_markers = sorted(reflected_names & TARGET_ONLY_SCHEMA_MARKER_NAMES)
        if target_markers:
            _unsupported_database_shape(
                f"0007 target constraint or index marker already exists: {table_name}.{target_markers}"
            )


def _require_direct_reference_columns(inspector: sa.Inspector) -> None:
    tables = set(inspector.get_table_names())
    for table_name, required_columns in DIRECT_REFERENCE_COLUMNS.items():
        if table_name not in tables:
            _unsupported_database_shape(f"required direct reference table is missing: {table_name}")
        actual_columns = _column_names(inspector, table_name)
        missing_columns = required_columns - actual_columns
        if missing_columns:
            _unsupported_database_shape(
                f"required direct reference columns are missing: {table_name}.{sorted(missing_columns)}"
            )


def _unsupported_database_shape(reason: str) -> None:
    raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: {reason}")


def _validate_schema(inspector: sa.Inspector, *, target: bool) -> None:
    tables = (*WIN5_TABLES, AUDIT_TABLE) if target else WIN5_TABLES
    for table_name in tables:
        _require_primary_key(inspector, table_name)
    _validate_columns(inspector, target=target)
    _validate_foreign_keys(inspector, target=target)
    _validate_unique_constraints(inspector, target=target)
    _validate_indexes(inspector, target=target)
    _validate_checks(inspector, target=target)
    _validate_table_options(inspector, tables)


def _validate_columns(inspector: sa.Inspector, *, target: bool) -> None:
    specs: dict[str, dict[str, tuple[str, bool, int | None, str | None]]] = {
        "win5_seasons": {
            "id": ("id", False, None, None),
            "name": ("string", False, 100, None),
            "starts_at": ("datetime", True, None, None),
            "ends_at": ("datetime", True, None, None),
            "status": ("string", False, 32, "draft" if target else None),
            "created_at": ("datetime", False, None, "current_timestamp"),
            "updated_at": ("datetime", False, None, "current_timestamp"),
        },
        "win5_rounds": {
            "id": ("id", False, None, None),
            "season_id": ("reference", False, None, None),
            "race_id": ("reference", not target, None, None),
            "round_number": ("integer", False, None, None),
            "round_label": ("string", True, 64, None),
            "status": ("string", False, 32, "setup" if target else None),
            "opens_at": ("datetime", True, None, None),
            "closes_at": ("datetime", True, None, None),
            "created_at": ("datetime", False, None, "current_timestamp"),
            "updated_at": ("datetime", False, None, "current_timestamp"),
        },
        "win5_entries": {
            "id": ("id", False, None, None),
            "season_id": ("reference", False, None, None),
            "round_id": ("reference", False, None, None),
            "game_account_id": ("reference", False, None, None),
            "status": ("string", False, 32, "accepted" if target else None),
            "cancelled_at": ("datetime", True, None, None),
            "created_at": ("datetime", False, None, "current_timestamp"),
            "updated_at": ("datetime", False, None, "current_timestamp"),
        },
        "win5_picks": {
            "id": ("id", False, None, None),
            "win5_entry_id": ("reference", False, None, None),
            "pick_order": ("integer", False, None, None),
            "entry_number": ("integer", False, None, None),
            "created_at": ("datetime", False, None, "current_timestamp"),
        },
        "win5_results": {
            "id": ("id", False, None, None),
            "round_id": ("reference", False, None, None),
            "event_id": ("reference", True, None, None),
            "season_id": ("reference", False, None, None),
            "race_id": ("reference", True, None, None),
            "result_order": ("json", False, None, None),
            "created_at": ("datetime", False, None, "current_timestamp"),
            "updated_at": ("datetime", False, None, "current_timestamp"),
        },
        "win5_judgements": {
            "id": ("id", False, None, None),
            "win5_entry_id": ("reference", False, None, None),
            "season_id": ("reference", False, None, None),
            "hit_count": ("integer", False, None, None),
            "score_delta": ("integer", False, None, None),
            "judgement_detail_json": ("json", True, None, None),
            "judged_at": ("datetime", True, None, None),
            "judged_by_discord_user_id": ("string", True, 32, None),
        },
        "win5_scores": {
            "id": ("id", False, None, None),
            "season_id": ("reference", False, None, None),
            "game_account_id": ("reference", False, None, None),
            "score": ("integer", False, None, None),
            "created_at": ("datetime", False, None, "current_timestamp"),
            "updated_at": ("datetime", False, None, "current_timestamp"),
        },
        "win5_score_events": {
            "id": ("id", False, None, None),
            "season_id": ("reference", False, None, None),
            "game_account_id": ("reference", False, None, None),
            "win5_entry_id": ("reference", True, None, None),
            "score_delta": ("integer", False, None, None),
            "reason": ("string", True, 255, None),
            "created_at": ("datetime", False, None, "current_timestamp"),
        },
    }
    if target:
        specs["win5_seasons"]["active_marker"] = ("string", True, 16, None)
        specs["win5_entries"].update(
            {
                "prediction_tier": ("string", False, 16, None),
                "idempotency_key": ("string", False, 128, None),
                "request_fingerprint": ("string", False, 64, None),
                "ordered_picks_fingerprint": ("string", False, 64, None),
                "accepted_picks_fingerprint": ("string", True, 64, None),
                "cancelled_by_discord_user_id": ("string", True, 32, None),
            }
        )
        specs[AUDIT_TABLE] = {
            "id": ("id", False, None, None),
            "action": ("string", False, 64, None),
            "capability": ("string", False, 64, None),
            "actor_discord_user_id": ("string", False, 32, None),
            "season_id": ("reference", True, None, None),
            "round_id": ("reference", True, None, None),
            "win5_entry_id": ("reference", True, None, None),
            "idempotency_key": ("string", False, 128, None),
            "request_fingerprint": ("string", False, 64, None),
            "before_json": ("json", True, None, None),
            "after_json": ("json", False, None, None),
            "reason": ("string", True, 255, None),
            "created_at": ("datetime", False, None, "current_timestamp"),
        }
    else:
        specs["win5_rounds"]["event_id"] = ("reference", True, None, None)
        specs["win5_entries"]["event_id"] = ("reference", True, None, None)
        specs["win5_picks"]["race_id"] = ("reference", True, None, None)

    for table_name, table_specs in specs.items():
        columns = {str(column["name"]): column for column in inspector.get_columns(table_name)}
        if set(columns) != set(table_specs):
            raise RuntimeError(f"cannot upgrade WU13A: {table_name} column signature mismatch")
        for column_name, spec in table_specs.items():
            _require_column(columns[column_name], table_name, column_name, *spec)
    _validate_mariadb_character_attributes(specs)
    _validate_mariadb_column_extras(specs)
    _validate_mariadb_json_checks(specs)


def _validate_foreign_keys(inspector: sa.Inspector, *, target: bool) -> None:
    expected: dict[
        str,
        set[tuple[str, tuple[str, ...], str | None, str, tuple[str, ...], str, str, bool, str]],
    ] = {
        "win5_rounds": {
            _foreign_key_signature("win5_rounds", "season_id", "win5_seasons"),
            _foreign_key_signature("win5_rounds", "race_id", "races"),
        },
        "win5_entries": {
            _foreign_key_signature("win5_entries", "season_id", "win5_seasons"),
            _foreign_key_signature("win5_entries", "round_id", "win5_rounds"),
            _foreign_key_signature("win5_entries", "game_account_id", "game_accounts"),
        },
        "win5_picks": {_foreign_key_signature("win5_picks", "win5_entry_id", "win5_entries")},
        "win5_results": {
            _foreign_key_signature("win5_results", "round_id", "win5_rounds"),
            _foreign_key_signature("win5_results", "event_id", "game_events"),
            _foreign_key_signature("win5_results", "season_id", "win5_seasons"),
            _foreign_key_signature("win5_results", "race_id", "races"),
        },
        "win5_judgements": {
            _foreign_key_signature("win5_judgements", "win5_entry_id", "win5_entries"),
            _foreign_key_signature("win5_judgements", "season_id", "win5_seasons"),
        },
        "win5_scores": {
            _foreign_key_signature("win5_scores", "season_id", "win5_seasons"),
            _foreign_key_signature("win5_scores", "game_account_id", "game_accounts"),
        },
        "win5_score_events": {
            _foreign_key_signature("win5_score_events", "season_id", "win5_seasons"),
            _foreign_key_signature("win5_score_events", "game_account_id", "game_accounts"),
            _foreign_key_signature("win5_score_events", "win5_entry_id", "win5_entries"),
        },
        "win5_seasons": set(),
    }
    if target:
        expected[AUDIT_TABLE] = {
            _foreign_key_signature(AUDIT_TABLE, "season_id", "win5_seasons"),
            _foreign_key_signature(AUDIT_TABLE, "round_id", "win5_rounds"),
            _foreign_key_signature(AUDIT_TABLE, "win5_entry_id", "win5_entries"),
        }
    else:
        expected["win5_rounds"].add(_foreign_key_signature("win5_rounds", "event_id", "game_events"))
        expected["win5_entries"].add(_foreign_key_signature("win5_entries", "event_id", "game_events"))
        expected["win5_picks"].add(_foreign_key_signature("win5_picks", "race_id", "races"))
    for table_name, expected_keys in expected.items():
        actual = {
            (
                str(key.get("name")),
                tuple(key.get("constrained_columns") or ()),
                key.get("referred_schema"),
                str(key.get("referred_table")),
                tuple(key.get("referred_columns") or ()),
                _normalize_referential_action((key.get("options") or {}).get("onupdate")),
                _normalize_referential_action((key.get("options") or {}).get("ondelete")),
                _normalize_deferrable((key.get("options") or {}).get("deferrable")),
                _normalize_initially((key.get("options") or {}).get("initially")),
            )
            for key in inspector.get_foreign_keys(table_name)
        }
        if actual != expected_keys:
            raise RuntimeError(f"cannot upgrade WU13A: {table_name} foreign-key allowlist mismatch")


def _foreign_key_signature(
    table_name: str,
    column_name: str,
    referred_table: str,
) -> tuple[str, tuple[str, ...], str | None, str, tuple[str, ...], str, str, bool, str]:
    return (
        f"fk_{table_name}_{column_name}_{referred_table}",
        (column_name,),
        None,
        referred_table,
        ("id",),
        "restrict",
        "restrict",
        False,
        "immediate",
    )


def _normalize_referential_action(value: object) -> str:
    normalized = str(value or "RESTRICT").strip().upper()
    return "restrict" if normalized in {"RESTRICT", "NO ACTION"} else normalized.lower()


def _normalize_deferrable(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "deferrable"}
    return bool(value)


def _normalize_initially(value: object) -> str:
    return str(value or "IMMEDIATE").strip().lower()


def _validate_unique_constraints(inspector: sa.Inspector, *, target: bool) -> None:
    expected: dict[str, set[tuple[str, tuple[str, ...]]]] = {
        "win5_seasons": set(),
        "win5_rounds": {("uq_win5_rounds_season_id", ("season_id", "round_number"))},
        "win5_entries": {("uq_win5_entries_round_id", ("round_id", "game_account_id"))},
        "win5_picks": set(),
        "win5_results": set(),
        "win5_judgements": set(),
        "win5_scores": {("uq_win5_scores_season_id", ("season_id", "game_account_id"))},
        "win5_score_events": set(),
    }
    if target:
        expected.update(
            {
                "win5_seasons": {("uq_win5_seasons_active_marker", ("active_marker",))},
                "win5_rounds": {
                    ("uq_win5_rounds_season_round_number", ("season_id", "round_number")),
                    ("uq_win5_rounds_race_id", ("race_id",)),
                },
                "win5_entries": {
                    (
                        "uq_win5_entries_accepted_prediction",
                        (
                            "round_id",
                            "game_account_id",
                            "prediction_tier",
                            "accepted_picks_fingerprint",
                        ),
                    ),
                    ("uq_win5_entries_idempotency_key", ("idempotency_key",)),
                },
                "win5_picks": {
                    ("uq_win5_picks_entry_order", ("win5_entry_id", "pick_order")),
                    ("uq_win5_picks_entry_number", ("win5_entry_id", "entry_number")),
                },
                AUDIT_TABLE: {("uq_win5_operation_audits_idempotency_key", ("idempotency_key",))},
            }
        )
    for table_name, expected_signatures in expected.items():
        actual = {
            (str(constraint.get("name")), tuple(constraint.get("column_names") or ()))
            for constraint in inspector.get_unique_constraints(table_name)
        }
        if actual != expected_signatures:
            raise RuntimeError(f"cannot upgrade WU13A: {table_name} UNIQUE allowlist mismatch")


def _validate_indexes(inspector: sa.Inspector, *, target: bool) -> None:
    expected_unique = _expected_unique_indexes(target=target)
    dialect = op.get_bind().dialect.name
    for table_name, unique_signatures in expected_unique.items():
        actual: set[tuple[str, tuple[str, ...], bool]] = set()
        for index in inspector.get_indexes(table_name):
            columns = tuple(index.get("column_names") or ())
            if not columns or any(column is None for column in columns):
                raise RuntimeError(f"cannot upgrade WU13A: {table_name} expression index is unsupported")
            if index.get("expressions"):
                raise RuntimeError(f"cannot upgrade WU13A: {table_name} expression index is unsupported")
            if index.get("dialect_options"):
                raise RuntimeError(f"cannot upgrade WU13A: {table_name} index options are unsupported")
            actual.add((str(index.get("name")), columns, bool(index.get("unique"))))
        expected = set()
        if dialect in {"mysql", "mariadb"}:
            expected = {(name, columns, True) for name, columns in unique_signatures}
            expected.update(
                _expected_mariadb_fk_indexes(
                    table_name,
                    target=target,
                    unique_signatures=unique_signatures,
                )
            )
        if actual != expected:
            raise RuntimeError(f"cannot upgrade WU13A: {table_name} index allowlist mismatch")


def _expected_unique_indexes(*, target: bool) -> dict[str, set[tuple[str, tuple[str, ...]]]]:
    expected: dict[str, set[tuple[str, tuple[str, ...]]]] = {
        "win5_seasons": set(),
        "win5_rounds": {("uq_win5_rounds_season_id", ("season_id", "round_number"))},
        "win5_entries": {("uq_win5_entries_round_id", ("round_id", "game_account_id"))},
        "win5_picks": set(),
        "win5_results": set(),
        "win5_judgements": set(),
        "win5_scores": {("uq_win5_scores_season_id", ("season_id", "game_account_id"))},
        "win5_score_events": set(),
    }
    if target:
        expected.update(
            {
                "win5_seasons": {("uq_win5_seasons_active_marker", ("active_marker",))},
                "win5_rounds": {
                    ("uq_win5_rounds_season_round_number", ("season_id", "round_number")),
                    ("uq_win5_rounds_race_id", ("race_id",)),
                },
                "win5_entries": {
                    (
                        "uq_win5_entries_accepted_prediction",
                        ("round_id", "game_account_id", "prediction_tier", "accepted_picks_fingerprint"),
                    ),
                    ("uq_win5_entries_idempotency_key", ("idempotency_key",)),
                },
                "win5_picks": {
                    ("uq_win5_picks_entry_order", ("win5_entry_id", "pick_order")),
                    ("uq_win5_picks_entry_number", ("win5_entry_id", "entry_number")),
                },
                AUDIT_TABLE: {("uq_win5_operation_audits_idempotency_key", ("idempotency_key",))},
            }
        )
    return expected


def _expected_mariadb_fk_indexes(
    table_name: str,
    *,
    target: bool,
    unique_signatures: set[tuple[str, tuple[str, ...]]],
) -> set[tuple[str, tuple[str, ...], bool]]:
    foreign_keys: dict[str, dict[str, str]] = {
        "win5_seasons": {},
        "win5_rounds": {"season_id": "win5_seasons", "race_id": "races"},
        "win5_entries": {
            "season_id": "win5_seasons",
            "round_id": "win5_rounds",
            "game_account_id": "game_accounts",
        },
        "win5_picks": {"win5_entry_id": "win5_entries"},
        "win5_results": {
            "round_id": "win5_rounds",
            "event_id": "game_events",
            "season_id": "win5_seasons",
            "race_id": "races",
        },
        "win5_judgements": {
            "win5_entry_id": "win5_entries",
            "season_id": "win5_seasons",
        },
        "win5_scores": {
            "season_id": "win5_seasons",
            "game_account_id": "game_accounts",
        },
        "win5_score_events": {
            "season_id": "win5_seasons",
            "game_account_id": "game_accounts",
            "win5_entry_id": "win5_entries",
        },
    }
    if target:
        foreign_keys[AUDIT_TABLE] = {
            "season_id": "win5_seasons",
            "round_id": "win5_rounds",
            "win5_entry_id": "win5_entries",
        }
    else:
        foreign_keys["win5_rounds"]["event_id"] = "game_events"
        foreign_keys["win5_entries"]["event_id"] = "game_events"
        foreign_keys["win5_picks"]["race_id"] = "races"
    covered_columns = {columns[0] for _name, columns in unique_signatures}
    return {
        (f"fk_{table_name}_{column_name}_{referred_table}", (column_name,), False)
        for column_name, referred_table in foreign_keys[table_name].items()
        if column_name not in covered_columns
    }


def _validate_checks(inspector: sa.Inspector, *, target: bool) -> None:
    tables = (*WIN5_TABLES, AUDIT_TABLE) if target else WIN5_TABLES
    expected: dict[str, dict[str, str]] = {table_name: {} for table_name in tables}
    if target:
        expected["win5_seasons"] = {
            "ck_win5_seasons_win5_season_status": "status IN ('draft', 'active', 'closed')",
            "ck_win5_seasons_win5_season_active_marker": "(status = 'active' AND active_marker = 'active') OR "
            "(status <> 'active' AND active_marker IS NULL)",
        }
        expected["win5_rounds"] = {
            "ck_win5_rounds_positive_win5_round_number": "round_number > 0",
            "ck_win5_rounds_win5_round_status": "status IN ('setup', 'open', 'closed', 'result_entered', 'scored')",
        }
        expected["win5_entries"] = {
            "ck_win5_entries_win5_prediction_tier": "prediction_tier IN ('top1', 'top3', 'top5')",
            "ck_win5_entries_win5_entry_status": "status IN ('accepted', 'cancelled')",
            "ck_win5_entries_win5_entry_state": "(status = 'accepted' AND "
            "accepted_picks_fingerprint = ordered_picks_fingerprint "
            "AND cancelled_at IS NULL AND cancelled_by_discord_user_id IS NULL) OR "
            "(status = 'cancelled' AND accepted_picks_fingerprint IS NULL "
            "AND cancelled_at IS NOT NULL AND cancelled_by_discord_user_id IS NOT NULL)",
        }
        expected["win5_picks"] = {
            "ck_win5_picks_positive_win5_pick_order": "pick_order > 0",
            "ck_win5_picks_positive_win5_entry_number": "entry_number > 0",
        }
    for table_name, checks in expected.items():
        actual = {}
        for constraint in inspector.get_check_constraints(table_name):
            expression = _normalize_sql(constraint.get("sqltext"))
            json_column = _implicit_json_check_column(expression)
            if json_column is not None and op.get_bind().dialect.name in {"mysql", "mariadb"}:
                continue
            actual[str(constraint.get("name"))] = expression
        normalized_expected = {name: _normalize_expected_check(expression) for name, expression in checks.items()}
        if actual != normalized_expected:
            raise RuntimeError(f"cannot upgrade WU13A: {table_name} CHECK allowlist mismatch")


def _implicit_json_check_column(expression: str) -> str | None:
    match = re.fullmatch(r"json_valid\((?P<column>[a-z0-9_]+)\)", expression)
    return match.group("column") if match else None


def _normalize_expected_check(value: str) -> str:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        return _normalize_sql(value)
    # MariaDB's canonical SHOW CREATE/reflection removes parentheses around
    # conjunction-only operands of OR because AND already binds more tightly.
    # Parentheses containing OR are retained, so meaning-changing regrouping is
    # still distinguishable.
    normalized = re.sub(
        r"\((?=[^()]*\band\b)(?![^()]*\bor\b)([^()]*)\)",
        r"\1",
        value,
        flags=re.IGNORECASE,
    )
    return _normalize_sql(normalized)


def _validate_table_options(inspector: sa.Inspector, tables: Sequence[str]) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table_name in tables:
            create_sql = (
                op.get_bind()
                .execute(
                    sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table_name"),
                    {"table_name": table_name},
                )
                .scalar_one_or_none()
            )
            unsupported_option = (
                r"\b(?:WITHOUT\s+ROWID|STRICT|AUTOINCREMENT|COLLATE)\b"
                r"|\bON\s+CONFLICT\b"
                r"|\bMATCH\s+(?:FULL|PARTIAL|SIMPLE)\b"
            )
            if create_sql is None or re.search(unsupported_option, str(create_sql), flags=re.IGNORECASE):
                raise RuntimeError(f"cannot upgrade WU13A: {table_name} SQLite column or table options mismatch")
        return
    if dialect not in {"mysql", "mariadb"}:
        raise RuntimeError(f"cannot upgrade WU13A: unsupported database dialect: {dialect}")
    database_name = op.get_bind().execute(sa.text("SELECT DATABASE()")).scalar_one()
    for table_name in tables:
        row = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT ENGINE, ROW_FORMAT, TABLE_COLLATION, CREATE_OPTIONS "
                    "FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table"
                ),
                {"schema": database_name, "table": table_name},
            )
            .one_or_none()
        )
        if row is None or tuple(str(value).lower() for value in row) != (
            "innodb",
            "dynamic",
            "utf8mb4_unicode_ci",
            "",
        ):
            raise RuntimeError(f"cannot upgrade WU13A: {table_name} MariaDB table options mismatch")


def _reject_prototype_rows() -> None:
    for table_name in WIN5_TABLES:
        row = op.get_bind().execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first()
        if row is not None:
            _unsupported_database_shape(
                f"prototype WIN5 operational data requires backup restoration; table={table_name}"
            )


def _drop_prototype_tables() -> None:
    for table_name in WIN5_TABLES:
        op.drop_table(table_name)


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


def _require_primary_key(inspector: sa.Inspector, table_name: str) -> None:
    primary_key = inspector.get_pk_constraint(table_name)
    if tuple(primary_key.get("constrained_columns") or ()) != ("id",):
        raise RuntimeError(f"cannot upgrade WU13A: {table_name} primary key must be (id)")
    identifier = next(column for column in inspector.get_columns(table_name) if column["name"] == "id")
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        if type(identifier["type"]).__name__.upper() != "INTEGER":
            raise RuntimeError(f"cannot upgrade WU13A: {table_name}.id must generate a SQLite rowid")
        _validate_sqlite_rowid_semantics(table_name)
    elif identifier.get("autoincrement") is not True:
        raise RuntimeError(f"cannot upgrade WU13A: {table_name}.id must be AUTO_INCREMENT")


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
        raise RuntimeError(f"cannot upgrade WU13A: {table_name} must use SQLite rowid semantics")
    primary_key_indexes = [
        row
        for row in op.get_bind().exec_driver_sql(f'PRAGMA index_list("{table_name}")').mappings()
        if str(row.get("origin") or "").lower() == "pk"
    ]
    if primary_key_indexes:
        raise RuntimeError(f"cannot upgrade WU13A: {table_name}.id must alias the SQLite rowid")


def _require_column(
    column: dict[str, object],
    table_name: str,
    column_name: str,
    family: str,
    nullable: bool,
    length: int | None,
    default: str | None,
) -> None:
    if column.get("computed") is not None:
        raise RuntimeError(f"cannot upgrade WU13A: {table_name}.{column_name} must not be generated or computed")
    if column.get("identity") is not None:
        raise RuntimeError(f"cannot upgrade WU13A: {table_name}.{column_name} must not use identity metadata")
    if bool(column["nullable"]) is not nullable:
        raise RuntimeError(f"cannot upgrade WU13A: {table_name}.{column_name} nullability mismatch")
    if not _type_matches(column["type"], family=family, length=length):
        raise RuntimeError(f"cannot upgrade WU13A: {table_name}.{column_name} type mismatch")
    actual_default = _normalize_default(column.get("default"), family=family)
    if actual_default != default:
        raise RuntimeError(f"cannot upgrade WU13A: {table_name}.{column_name} server default mismatch")


def _type_matches(actual: sa.types.TypeEngine, *, family: str, length: int | None) -> bool:
    dialect = op.get_bind().dialect.name
    if family in {"id", "reference"}:
        if dialect == "sqlite":
            return type(actual).__name__.upper() == "INTEGER"
        return (
            isinstance(actual, sa.BigInteger)
            and not bool(getattr(actual, "unsigned", False))
            and not bool(getattr(actual, "zerofill", False))
        )
    if family == "integer":
        return (
            isinstance(actual, sa.Integer)
            and not isinstance(actual, sa.BigInteger)
            and not bool(getattr(actual, "unsigned", False))
            and not bool(getattr(actual, "zerofill", False))
        )
    if family == "string":
        return (
            isinstance(actual, sa.String)
            and not isinstance(actual, sa.Text)
            and getattr(actual, "length", None) == length
            and not bool(getattr(actual, "binary", False))
        )
    if family == "text":
        return isinstance(actual, sa.Text) and not bool(getattr(actual, "binary", False))
    if family == "numeric_10_2":
        return (
            isinstance(actual, sa.Numeric)
            and getattr(actual, "precision", None) == 10
            and getattr(actual, "scale", None) == 2
            and not bool(getattr(actual, "unsigned", False))
            and not bool(getattr(actual, "zerofill", False))
        )
    if family == "datetime":
        return (
            isinstance(actual, sa.DateTime)
            and type(actual).__name__.upper() != "TIMESTAMP"
            and (dialect == "sqlite" or getattr(actual, "fsp", None) in {None, 0})
        )
    if family == "json":
        if dialect == "sqlite":
            return isinstance(actual, sa.JSON)
        return isinstance(actual, sa.JSON) or type(actual).__name__.upper() in {"LONGTEXT", "JSON"}
    return False


def _validate_mariadb_character_attributes(
    specs: dict[str, dict[str, tuple[str, bool, int | None, str | None]]],
) -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    database_name = op.get_bind().execute(sa.text("SELECT DATABASE()")).scalar_one()
    for table_name, columns in specs.items():
        for column_name, (family, _nullable, _length, _default) in columns.items():
            if family not in {"string", "text", "json"}:
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
                .one_or_none()
            )
            expected = ("utf8mb4", "utf8mb4_bin") if family == "json" else ("utf8mb4", "utf8mb4_unicode_ci")
            if row is None or tuple(str(value).lower() if value is not None else None for value in row) != expected:
                raise RuntimeError(f"cannot upgrade WU13A: {table_name}.{column_name} charset or collation mismatch")


def _validate_mariadb_column_extras(
    specs: dict[str, dict[str, tuple[str, bool, int | None, str | None]]],
) -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    database_name = op.get_bind().execute(sa.text("SELECT DATABASE()")).scalar_one()
    for table_name, columns in specs.items():
        rows = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT COLUMN_NAME, EXTRA FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table"
                ),
                {"schema": database_name, "table": table_name},
            )
            .all()
        )
        actual = {str(column_name): str(extra or "").strip().lower() for column_name, extra in rows}
        expected = {column_name: ("auto_increment" if column_name == "id" else "") for column_name in columns}
        if actual != expected:
            raise RuntimeError(f"cannot upgrade WU13A: {table_name} column EXTRA signature mismatch")


def _validate_mariadb_json_checks(
    specs: dict[str, dict[str, tuple[str, bool, int | None, str | None]]],
) -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table_name, columns in specs.items():
        json_columns = {column_name for column_name, spec in columns.items() if spec[0] == "json"}
        if not json_columns:
            continue
        show_create = str(op.get_bind().exec_driver_sql(f"SHOW CREATE TABLE `{table_name}`").one()[1])
        normalized = re.sub(r"\s+", " ", show_create.lower())
        for column_name in json_columns:
            pattern = (
                rf"`{re.escape(column_name)}`\s+longtext\s+character\s+set\s+utf8mb4\s+"
                rf"collate\s+utf8mb4_bin\b[^,\n]*\bcheck\s*\(\s*json_valid\s*"
                rf"\(\s*`{re.escape(column_name)}`\s*\)\s*\)"
            )
            if re.search(pattern, normalized) is None:
                raise RuntimeError(f"cannot upgrade WU13A: {table_name}.{column_name} JSON semantics mismatch")


def _normalize_default(value: object, *, family: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    while _has_single_outer_parentheses(normalized):
        normalized = normalized[1:-1].strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        quote = normalized[0]
        literal = normalized[1:-1].replace(quote * 2, quote)
        return f"literal:{literal}" if family != "string" else literal
    sql_expression = re.sub(r"\s+", "", normalized).lower()
    if family == "datetime" and sql_expression in {"current_timestamp", "current_timestamp()"}:
        return "current_timestamp"
    if family in {"integer", "id", "reference", "numeric_10_2"} and re.fullmatch(
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)",
        normalized,
    ):
        return f"literal:{normalized}"
    return f"sql:{sql_expression}"


def _normalize_sql(value: object) -> str:
    source = str(value or "")
    normalized: list[str] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character == "'":
            literal, index = _consume_sql_quoted_token(source, index, quote="'")
            normalized.append(literal)
            continue
        if character in {'"', "`"}:
            identifier, index = _consume_sql_quoted_token(source, index, quote=character)
            normalized.append(identifier[1:-1].replace(character * 2, character).lower())
            continue
        if source[index : index + len("_utf8mb4")].lower() == "_utf8mb4":
            index += len("_utf8mb4")
            continue
        if not character.isspace():
            normalized.append(character.lower())
        index += 1
    text = "".join(normalized)
    while _has_single_outer_parentheses(text):
        text = text[1:-1]
    return text


def _consume_sql_quoted_token(source: str, start: int, *, quote: str) -> tuple[str, int]:
    index = start + 1
    while index < len(source):
        character = source[index]
        if character == "\\" and index + 1 < len(source):
            index += 2
            continue
        if character == quote:
            if index + 1 < len(source) and source[index + 1] == quote:
                index += 2
                continue
            return source[start : index + 1], index + 1
        index += 1
    return source[start:], len(source)


def _has_single_outer_parentheses(value: str) -> bool:
    if len(value) < 2 or value[0] != "(" or value[-1] != ")":
        return False
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        if quote is not None:
            if character == "\\" and index + 1 < len(value):
                index += 2
                continue
            if character == quote and index + 1 < len(value) and value[index + 1] == quote:
                index += 2
                continue
            if character == quote:
                quote = None
        elif character in {"'", '"'}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0 and index != len(value) - 1:
                return False
            if depth < 0:
                return False
        index += 1
    return depth == 0 and quote is None


def _create_target_tables() -> None:
    op.create_table(
        "win5_seasons",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), server_default="draft", nullable=False),
        sa.Column("active_marker", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("active_marker"),
        sa.CheckConstraint("status IN ('draft', 'active', 'closed')", name="win5_season_status"),
        sa.CheckConstraint(
            "(status = 'active' AND active_marker = 'active') OR (status <> 'active' AND active_marker IS NULL)",
            name="win5_season_active_marker",
        ),
        **MARIADB_OPTIONS,
    )
    op.create_table(
        "win5_rounds",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("season_id", BIGINT, sa.ForeignKey("win5_seasons.id"), nullable=False),
        sa.Column("race_id", BIGINT, sa.ForeignKey("races.id"), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("round_label", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), server_default="setup", nullable=False),
        sa.Column("opens_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("season_id", "round_number", name="uq_win5_rounds_season_round_number"),
        sa.UniqueConstraint("race_id", name="uq_win5_rounds_race_id"),
        sa.CheckConstraint("round_number > 0", name="positive_win5_round_number"),
        sa.CheckConstraint(
            "status IN ('setup', 'open', 'closed', 'result_entered', 'scored')",
            name="win5_round_status",
        ),
        **MARIADB_OPTIONS,
    )
    op.create_table(
        "win5_entries",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("season_id", BIGINT, sa.ForeignKey("win5_seasons.id"), nullable=False),
        sa.Column("round_id", BIGINT, sa.ForeignKey("win5_rounds.id"), nullable=False),
        sa.Column("game_account_id", BIGINT, sa.ForeignKey("game_accounts.id"), nullable=False),
        sa.Column("prediction_tier", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("ordered_picks_fingerprint", sa.String(64), nullable=False),
        sa.Column("accepted_picks_fingerprint", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), server_default="accepted", nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by_discord_user_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_win5_entries_idempotency_key"),
        sa.UniqueConstraint(
            "round_id",
            "game_account_id",
            "prediction_tier",
            "accepted_picks_fingerprint",
            name="uq_win5_entries_accepted_prediction",
        ),
        sa.CheckConstraint("prediction_tier IN ('top1', 'top3', 'top5')", name="win5_prediction_tier"),
        sa.CheckConstraint("status IN ('accepted', 'cancelled')", name="win5_entry_status"),
        sa.CheckConstraint(
            "(status = 'accepted' AND accepted_picks_fingerprint = ordered_picks_fingerprint "
            "AND cancelled_at IS NULL AND cancelled_by_discord_user_id IS NULL) OR "
            "(status = 'cancelled' AND accepted_picks_fingerprint IS NULL "
            "AND cancelled_at IS NOT NULL AND cancelled_by_discord_user_id IS NOT NULL)",
            name="win5_entry_state",
        ),
        **MARIADB_OPTIONS,
    )
    op.create_table(
        "win5_picks",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("win5_entry_id", BIGINT, sa.ForeignKey("win5_entries.id"), nullable=False),
        sa.Column("pick_order", sa.Integer(), nullable=False),
        sa.Column("entry_number", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("win5_entry_id", "pick_order", name="uq_win5_picks_entry_order"),
        sa.UniqueConstraint("win5_entry_id", "entry_number", name="uq_win5_picks_entry_number"),
        sa.CheckConstraint("pick_order > 0", name="positive_win5_pick_order"),
        sa.CheckConstraint("entry_number > 0", name="positive_win5_entry_number"),
        **MARIADB_OPTIONS,
    )
    op.create_table(
        "win5_results",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("round_id", BIGINT, sa.ForeignKey("win5_rounds.id"), nullable=False),
        sa.Column("event_id", BIGINT, sa.ForeignKey("game_events.id"), nullable=True),
        sa.Column("season_id", BIGINT, sa.ForeignKey("win5_seasons.id"), nullable=False),
        sa.Column("race_id", BIGINT, sa.ForeignKey("races.id"), nullable=True),
        sa.Column("result_order", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        **MARIADB_OPTIONS,
    )
    op.create_table(
        "win5_judgements",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("win5_entry_id", BIGINT, sa.ForeignKey("win5_entries.id"), nullable=False),
        sa.Column("season_id", BIGINT, sa.ForeignKey("win5_seasons.id"), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("score_delta", sa.Integer(), nullable=False),
        sa.Column("judgement_detail_json", sa.JSON(), nullable=True),
        sa.Column("judged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("judged_by_discord_user_id", sa.String(32), nullable=True),
        **MARIADB_OPTIONS,
    )
    op.create_table(
        "win5_scores",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("season_id", BIGINT, sa.ForeignKey("win5_seasons.id"), nullable=False),
        sa.Column("game_account_id", BIGINT, sa.ForeignKey("game_accounts.id"), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("season_id", "game_account_id"),
        **MARIADB_OPTIONS,
    )
    op.create_table(
        "win5_score_events",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("season_id", BIGINT, sa.ForeignKey("win5_seasons.id"), nullable=False),
        sa.Column("game_account_id", BIGINT, sa.ForeignKey("game_accounts.id"), nullable=False),
        sa.Column("win5_entry_id", BIGINT, sa.ForeignKey("win5_entries.id"), nullable=True),
        sa.Column("score_delta", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        **MARIADB_OPTIONS,
    )
    op.create_table(
        AUDIT_TABLE,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("actor_discord_user_id", sa.String(32), nullable=False),
        sa.Column("season_id", BIGINT, sa.ForeignKey("win5_seasons.id"), nullable=True),
        sa.Column("round_id", BIGINT, sa.ForeignKey("win5_rounds.id"), nullable=True),
        sa.Column("win5_entry_id", BIGINT, sa.ForeignKey("win5_entries.id"), nullable=True),
        sa.Column("idempotency_key", sa.String(128), unique=True, nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        **MARIADB_OPTIONS,
    )
