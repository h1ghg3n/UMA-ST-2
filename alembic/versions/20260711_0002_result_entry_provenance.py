"""separate result exclusion semantics and preserve import provenance

Revision ID: 20260711_0002
Revises: 20260710_0001
Create Date: 2026-07-11 00:02:00.000000

The previous ``race_results.is_excluded`` field was consumed by betting
judgement, so its existing values are retained as ``is_betting_excluded``.
Rating exclusion is a separate concept and starts as false for existing rows.
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260711_0002"
down_revision: str | None = "20260710_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTRY_SOURCE_CHECK = "ck_race_entries_entry_number_source"
ENTRY_SOURCE_FOREIGN_KEY = "fk_race_entries_source_import_record_id_sheet_import_records"
ENTRY_SOURCE_UNIQUE = "uq_race_entries_source_import_record_id"
RESULT_SOURCE_FOREIGN_KEY = "fk_race_results_source_import_record_id_sheet_import_records"
RESULT_SOURCE_UNIQUE = "uq_race_results_source_import_record_id"
RESULT_BETTING_INDEX = "ix_race_results_race_id_betting_excluded"
ENTRY_SOURCE_VALUES = {"declared", "payout_result", "synthetic"}


def upgrade() -> None:
    _validate_supported_partial_schema()
    _ensure_race_entry_columns()
    _ensure_race_entry_schema_objects()
    _ensure_race_result_columns()
    _migrate_legacy_betting_exclusions()
    _ensure_race_result_schema_objects()
    _validate_final_schema()


def downgrade() -> None:
    if "is_betting_excluded" in _column_names("race_results"):
        if "is_excluded" not in _column_names("race_results"):
            with op.batch_alter_table("race_results") as batch:
                batch.add_column(sa.Column("is_excluded", sa.Boolean(), nullable=False, server_default=sa.false()))
        op.execute(
            sa.text(
                "UPDATE race_results "
                "SET is_excluded = CASE "
                "WHEN is_betting_excluded OR is_result_void THEN 1 ELSE 0 END"
            )
        )
        result_foreign_key = _find_foreign_key("race_results", "source_import_record_id", "sheet_import_records", "id")
        result_unique = _find_unique_constraint("race_results", "source_import_record_id")
        with op.batch_alter_table("race_results") as batch:
            if _has_index("race_results", RESULT_BETTING_INDEX):
                batch.drop_index(RESULT_BETTING_INDEX)
            if result_foreign_key is not None:
                batch.drop_constraint(_constraint_name(result_foreign_key), type_="foreignkey")
            if result_unique is not None:
                batch.drop_constraint(_constraint_name(result_unique), type_="unique")
            if "source_import_record_id" in _column_names("race_results"):
                batch.drop_column("source_import_record_id")
            if "is_result_void" in _column_names("race_results"):
                batch.drop_column("is_result_void")
            if "is_rating_excluded" in _column_names("race_results"):
                batch.drop_column("is_rating_excluded")
            batch.drop_column("is_betting_excluded")
            if "converted_rank" in _column_names("race_results"):
                batch.drop_column("converted_rank")

    if "entry_number_source" in _column_names("race_entries"):
        entry_foreign_key = _find_foreign_key("race_entries", "source_import_record_id", "sheet_import_records", "id")
        entry_unique = _find_unique_constraint("race_entries", "source_import_record_id")
        entry_check = _find_entry_source_check_constraint("race_entries")
        with op.batch_alter_table("race_entries") as batch:
            if entry_foreign_key is not None:
                batch.drop_constraint(_constraint_name(entry_foreign_key), type_="foreignkey")
            if entry_check is not None:
                batch.drop_constraint(op.f(_constraint_name(entry_check)), type_="check")
            if entry_unique is not None:
                batch.drop_constraint(_constraint_name(entry_unique), type_="unique")
            if "source_import_record_id" in _column_names("race_entries"):
                batch.drop_column("source_import_record_id")
            batch.drop_column("entry_number_source")


def _ensure_race_entry_columns() -> None:
    missing = _missing_columns(
        "race_entries",
        {
            "entry_number_source": sa.Column(
                "entry_number_source",
                sa.String(length=32),
                nullable=False,
                server_default="declared",
            ),
            "source_import_record_id": sa.Column("source_import_record_id", sa.BigInteger(), nullable=True),
        },
    )
    if missing:
        with op.batch_alter_table("race_entries") as batch:
            for column in missing.values():
                batch.add_column(column)


def _ensure_race_entry_schema_objects() -> None:
    needs_check = _find_entry_source_check_constraint("race_entries") is None
    needs_unique = not _has_unique_constraint("race_entries", "source_import_record_id")
    needs_foreign_key = not _has_foreign_key("race_entries", "source_import_record_id", "sheet_import_records", "id")
    if not any((needs_check, needs_unique, needs_foreign_key)):
        return
    with op.batch_alter_table("race_entries") as batch:
        if needs_check:
            batch.create_check_constraint(
                "entry_number_source",
                "entry_number_source IN ('declared', 'payout_result', 'synthetic')",
            )
        if needs_unique:
            batch.create_unique_constraint(ENTRY_SOURCE_UNIQUE, ["source_import_record_id"])
        if needs_foreign_key:
            batch.create_foreign_key(
                ENTRY_SOURCE_FOREIGN_KEY,
                "sheet_import_records",
                ["source_import_record_id"],
                ["id"],
            )


def _ensure_race_result_columns() -> None:
    missing = _missing_columns(
        "race_results",
        {
            "converted_rank": sa.Column("converted_rank", sa.Integer(), nullable=True),
            "is_betting_excluded": sa.Column(
                "is_betting_excluded",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            "is_rating_excluded": sa.Column(
                "is_rating_excluded",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            "is_result_void": sa.Column(
                "is_result_void",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            "source_import_record_id": sa.Column("source_import_record_id", sa.BigInteger(), nullable=True),
        },
    )
    if missing:
        with op.batch_alter_table("race_results") as batch:
            for column in missing.values():
                batch.add_column(column)


def _migrate_legacy_betting_exclusions() -> None:
    columns = _column_names("race_results")
    if "is_excluded" in columns:
        op.execute(
            sa.text(
                "UPDATE race_results "
                "SET is_betting_excluded = CASE "
                "WHEN COALESCE(is_betting_excluded, 0) OR COALESCE(is_excluded, 0) THEN 1 ELSE 0 END"
            )
        )
        with op.batch_alter_table("race_results") as batch:
            batch.drop_column("is_excluded")
    else:
        op.execute(sa.text("UPDATE race_results SET is_betting_excluded = COALESCE(is_betting_excluded, 0)"))
    op.execute(sa.text("UPDATE race_results SET is_rating_excluded = COALESCE(is_rating_excluded, 0)"))
    op.execute(sa.text("UPDATE race_results SET is_result_void = COALESCE(is_result_void, 0)"))


def _ensure_race_result_schema_objects() -> None:
    needs_unique = not _has_unique_constraint("race_results", "source_import_record_id")
    needs_foreign_key = not _has_foreign_key("race_results", "source_import_record_id", "sheet_import_records", "id")
    needs_index = not _has_index("race_results", RESULT_BETTING_INDEX)
    if not any((needs_unique, needs_foreign_key, needs_index)):
        return
    with op.batch_alter_table("race_results") as batch:
        if needs_unique:
            batch.create_unique_constraint(RESULT_SOURCE_UNIQUE, ["source_import_record_id"])
        if needs_foreign_key:
            batch.create_foreign_key(
                RESULT_SOURCE_FOREIGN_KEY,
                "sheet_import_records",
                ["source_import_record_id"],
                ["id"],
            )
        if needs_index:
            batch.create_index(RESULT_BETTING_INDEX, ["race_id", "is_betting_excluded"], unique=False)


def _validate_final_schema() -> None:
    _require_columns("race_entries", {"entry_number_source", "source_import_record_id"})
    _require_columns(
        "race_results",
        {
            "converted_rank",
            "is_betting_excluded",
            "is_rating_excluded",
            "is_result_void",
            "source_import_record_id",
        },
    )
    if "is_excluded" in _column_names("race_results"):
        raise RuntimeError("race_results.is_excluded remains after result-entry migration")
    entry_source_checks = _find_entry_source_check_constraints("race_entries")
    if len(entry_source_checks) != 1:
        raise RuntimeError(
            "result-entry migration incomplete: race_entries must have exactly one entry_number_source check constraint"
        )
    required_objects = (
        (True, ENTRY_SOURCE_CHECK),
        (
            _has_unique_constraint("race_entries", "source_import_record_id"),
            ENTRY_SOURCE_UNIQUE,
        ),
        (
            _has_foreign_key("race_entries", "source_import_record_id", "sheet_import_records", "id"),
            ENTRY_SOURCE_FOREIGN_KEY,
        ),
        (
            _has_unique_constraint("race_results", "source_import_record_id"),
            RESULT_SOURCE_UNIQUE,
        ),
        (
            _has_foreign_key("race_results", "source_import_record_id", "sheet_import_records", "id"),
            RESULT_SOURCE_FOREIGN_KEY,
        ),
        (_has_index("race_results", RESULT_BETTING_INDEX), RESULT_BETTING_INDEX),
    )
    missing = [name for exists, name in required_objects if not exists]
    if missing:
        raise RuntimeError(f"result-entry migration incomplete: {', '.join(missing)}")


def _missing_columns(table_name: str, expected: dict[str, sa.Column]) -> dict[str, sa.Column]:
    existing = _column_names(table_name)
    return {name: column for name, column in expected.items() if name not in existing}


def _require_columns(table_name: str, expected: set[str]) -> None:
    missing = expected - _column_names(table_name)
    if missing:
        raise RuntimeError(f"result-entry migration incomplete: {table_name} missing {', '.join(sorted(missing))}")


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _validate_supported_partial_schema() -> None:
    unsupported_entry_checks = [
        constraint
        for constraint in sa.inspect(op.get_bind()).get_check_constraints("race_entries")
        if _references_entry_source(constraint) and not _is_entry_source_check_constraint(constraint)
    ]
    if unsupported_entry_checks:
        raise RuntimeError("cannot safely upgrade race_entries: unsupported entry_number_source check constraint")

    constraints: tuple[tuple[str, str, dict[str, object] | None], ...] = (
        (
            "race_entries",
            "entry_number_source check",
            _find_entry_source_check_constraint("race_entries"),
        ),
        (
            "race_entries",
            "source_import_record_id unique",
            _find_unique_constraint("race_entries", "source_import_record_id"),
        ),
        (
            "race_entries",
            "source_import_record_id foreign key",
            _find_foreign_key("race_entries", "source_import_record_id", "sheet_import_records", "id"),
        ),
        (
            "race_results",
            "source_import_record_id unique",
            _find_unique_constraint("race_results", "source_import_record_id"),
        ),
        (
            "race_results",
            "source_import_record_id foreign key",
            _find_foreign_key("race_results", "source_import_record_id", "sheet_import_records", "id"),
        ),
    )
    for table_name, description, constraint in constraints:
        if constraint is not None and not constraint.get("name"):
            raise RuntimeError(f"cannot safely upgrade {table_name}: unnamed {description} constraint")


def _find_entry_source_check_constraint(table_name: str) -> dict[str, object] | None:
    constraints = _find_entry_source_check_constraints(table_name)
    return constraints[0] if constraints else None


def _find_entry_source_check_constraints(table_name: str) -> list[dict[str, object]]:
    return [
        constraint
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if _is_entry_source_check_constraint(constraint)
    ]


def _is_entry_source_check_constraint(constraint: dict[str, object]) -> bool:
    sqltext = constraint.get("sqltext")
    if not isinstance(sqltext, str):
        return False
    expression = _strip_outer_parentheses(sqltext.strip())
    match = re.fullmatch(
        r'(?:(?:[`"\[]?[A-Za-z_][A-Za-z0-9_]*[`"\]]?)\.)?'
        r'[`"\[]?entry_number_source[`"\]]?\s+IN\s*\((?P<values>.*)\)',
        expression,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return False
    values_text = match.group("values")
    quoted_values = re.findall(r"'([^']*)'|\"([^\"]*)\"", values_text)
    values = [single or double for single, double in quoted_values]
    remainder = re.sub(r"'[^']*'|\"[^\"]*\"", "", values_text)
    if re.sub(r"[\s,]", "", remainder):
        return False
    return len(values) == len(ENTRY_SOURCE_VALUES) and set(values) == ENTRY_SOURCE_VALUES


def _references_entry_source(constraint: dict[str, object]) -> bool:
    sqltext = constraint.get("sqltext")
    return isinstance(sqltext, str) and "entry_number_source" in sqltext.lower()


def _strip_outer_parentheses(value: str) -> str:
    result = value
    while result.startswith("(") and result.endswith(")"):
        depth = 0
        wraps_entire_expression = True
        for index, character in enumerate(result):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(result) - 1:
                    wraps_entire_expression = False
                    break
        if not wraps_entire_expression or depth != 0:
            break
        result = result[1:-1].strip()
    return result


def _has_unique_constraint(table_name: str, expected_column: str) -> bool:
    return _find_unique_constraint(table_name, expected_column) is not None


def _find_unique_constraint(table_name: str, expected_column: str) -> dict[str, object] | None:
    return next(
        (
            constraint
            for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table_name)
            if tuple(constraint["column_names"]) == (expected_column,)
        ),
        None,
    )


def _has_foreign_key(table_name: str, column: str, referred_table: str, referred_column: str) -> bool:
    return _find_foreign_key(table_name, column, referred_table, referred_column) is not None


def _find_foreign_key(
    table_name: str,
    column: str,
    referred_table: str,
    referred_column: str,
) -> dict[str, object] | None:
    return next(
        (
            constraint
            for constraint in sa.inspect(op.get_bind()).get_foreign_keys(table_name)
            if tuple(constraint["constrained_columns"]) == (column,)
            and constraint["referred_table"] == referred_table
            and tuple(constraint["referred_columns"]) == (referred_column,)
        ),
        None,
    )


def _constraint_name(constraint: dict[str, object]) -> str:
    name = constraint.get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("cannot safely downgrade an unnamed result-entry constraint")
    return name


def _has_index(table_name: str, expected_name: str) -> bool:
    return any(index["name"] == expected_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))
