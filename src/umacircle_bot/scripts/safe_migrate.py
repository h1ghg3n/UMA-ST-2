from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Connection,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql.type_api import TypeEngine

import umacircle_bot.db.models  # noqa: F401
from umacircle_bot.config import get_settings
from umacircle_bot.db.base import Base
from umacircle_bot.runtime_preflight import EXPECTED_ALEMBIC_HEAD, EXPECTED_ROOM_POINT_SCALE

MIGRATION_LOCK_NAME = "umacircle_safe_migrate_v1"
MIGRATION_LOCK_TIMEOUT_SECONDS = 30
_MANAGED_TABLES = frozenset({*Base.metadata.tables, "alembic_version", "room_point_scale_state"})
APPROVED_FORWARD_TRANSITIONS: Mapping[str, str] = {
    "20260814_0028": "20260815_0030",
    "20260814_0029": "20260815_0030",
}
_FORWARD_EXCLUDED_COLUMNS: Mapping[str, Mapping[str, frozenset[str]]] = {
    "20260814_0028": {
        "room_point_transactions": frozenset({"related_race_result_id"}),
    },
    "20260814_0029": {},
}
_FORWARD_EXCLUDED_CHECKS: Mapping[str, Mapping[str, frozenset[str]]] = {
    "20260814_0028": {
        "win5_rounds": frozenset(
            {
                "(round_type = 'normal' AND race_id IS NOT NULL) OR "
                "(round_type = 'breeders_cup_day' AND race_id IS NULL)",
                "(round_type = 'normal' AND race_id IS NOT NULL) OR (round_type = 'special' AND race_id IS NULL)",
            }
        ),
    },
    "20260814_0029": {
        "win5_rounds": frozenset(
            {
                "(round_type = 'normal' AND race_id IS NOT NULL) OR "
                "(round_type = 'breeders_cup_day' AND race_id IS NULL)",
                "(round_type = 'normal' AND race_id IS NOT NULL) OR (round_type = 'special' AND race_id IS NULL)",
            }
        ),
    },
}


class UnsafeMigrationStateError(RuntimeError):
    """Raised before the bot can run against an unsupported database state."""


@dataclass(frozen=True, slots=True)
class MigrationDatabaseState:
    dialect: str
    tables: tuple[str, ...]
    views: tuple[str, ...]
    alembic_revisions: tuple[str, ...]
    schema_errors: tuple[str, ...] = ()

    @property
    def is_structurally_empty(self) -> bool:
        return not self.tables and not self.views

    @property
    def is_exact_head(self) -> bool:
        return self.alembic_revisions == (EXPECTED_ALEMBIC_HEAD,) and not self.schema_errors

    @property
    def is_supported_forward(self) -> bool:
        return (
            len(self.alembic_revisions) == 1
            and _is_approved_forward_revision(self.alembic_revisions[0])
            and not self.schema_errors
        )

    @property
    def is_safe(self) -> bool:
        return self.is_structurally_empty or self.is_exact_head or self.is_supported_forward

    @property
    def disposition(self) -> str:
        if self.is_structurally_empty:
            return "fresh"
        if self.is_exact_head:
            return "current"
        if self.is_supported_forward:
            return "forward"
        return "rejected"


def inspect_migration_database(connection: Connection) -> MigrationDatabaseState:
    """Read revision and physical-schema state for the deployment migration gate."""
    inspector = inspect(connection)
    tables = tuple(sorted(inspector.get_table_names()))
    views = tuple(sorted(inspector.get_view_names()))
    revisions: tuple[str, ...] = ()
    if "alembic_version" in tables:
        revisions = tuple(
            str(revision)
            for revision in connection.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            ).scalars()
        )
    schema_errors: tuple[str, ...] = ()
    if len(revisions) == 1:
        revision = revisions[0]
        if revision == EXPECTED_ALEMBIC_HEAD:
            schema_errors = _head_schema_errors(connection, tables=tables, views=views)
        elif _is_approved_forward_revision(revision):
            schema_errors = _head_schema_errors(
                connection,
                tables=tables,
                views=views,
                excluded_columns=_FORWARD_EXCLUDED_COLUMNS[revision],
                excluded_checks=_FORWARD_EXCLUDED_CHECKS[revision],
            )
    return MigrationDatabaseState(
        dialect=connection.dialect.name,
        tables=tables,
        views=views,
        alembic_revisions=revisions,
        schema_errors=schema_errors,
    )


def require_safe_migration_state(state: MigrationDatabaseState) -> None:
    if state.is_safe:
        return
    revision = ",".join(state.alembic_revisions) if state.alembic_revisions else "none"
    detail = ""
    if state.schema_errors:
        detail = "; schema contract errors: " + ", ".join(state.schema_errors[:8])
    supported_forward = ",".join(
        f"{source}->{target}" for source, target in sorted(APPROVED_FORWARD_TRANSITIONS.items())
    )
    raise UnsafeMigrationStateError(
        "refusing deployment migration before DDL: database is nonempty and its "
        f"Alembic revision is {revision}; expected exact {EXPECTED_ALEMBIC_HEAD} physical schema "
        f"or approved forward source {supported_forward}{detail}"
    )


def _is_approved_forward_revision(revision: str) -> bool:
    return APPROVED_FORWARD_TRANSITIONS.get(revision) == EXPECTED_ALEMBIC_HEAD


def run_safe_migration(
    database_url: str,
    *,
    check_only: bool = False,
    alembic_config_path: Path | str = Path("alembic.ini"),
) -> MigrationDatabaseState:
    """Check eligibility and optionally migrate a fresh/current/approved-forward DB."""
    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection, _deployment_migration_lock(connection):
            state = inspect_migration_database(connection)
            require_safe_migration_state(state)
            if check_only:
                return state

            # Introspection starts an implicit SQLAlchemy transaction. End that
            # read transaction while retaining the connection-scoped MariaDB
            # advisory lock, then give Alembic one owned transaction boundary.
            connection.rollback()
            configuration = Config(str(alembic_config_path))
            configuration.attributes["connection"] = connection
            with connection.begin():
                command.upgrade(configuration, "head")

            deployed_state = inspect_migration_database(connection)
            if not deployed_state.is_exact_head:
                detail = ", ".join(deployed_state.schema_errors[:8]) or "revision/schema mismatch"
                raise UnsafeMigrationStateError(
                    "post-migration physical schema verification failed; bot startup must remain stopped: " + detail
                )
            return state
    finally:
        engine.dispose()


@contextmanager
def _deployment_migration_lock(connection: Connection) -> Iterator[None]:
    if connection.dialect.name not in {"mysql", "mariadb"}:
        yield
        return
    acquired = connection.execute(
        text("SELECT GET_LOCK(:name, :timeout_seconds)"),
        {
            "name": MIGRATION_LOCK_NAME,
            "timeout_seconds": MIGRATION_LOCK_TIMEOUT_SECONDS,
        },
    ).scalar_one_or_none()
    if acquired != 1:
        raise UnsafeMigrationStateError("could not acquire the deployment migration advisory lock")
    try:
        yield
    finally:
        connection.execute(
            text("SELECT RELEASE_LOCK(:name)"),
            {"name": MIGRATION_LOCK_NAME},
        ).scalar_one_or_none()


def _head_schema_errors(
    connection: Connection,
    *,
    tables: tuple[str, ...],
    views: tuple[str, ...],
    excluded_columns: Mapping[str, frozenset[str]] | None = None,
    excluded_checks: Mapping[str, frozenset[str]] | None = None,
) -> tuple[str, ...]:
    inspector = inspect(connection)
    errors: list[str] = []
    exclusions = excluded_columns or {}
    check_exclusions = excluded_checks or {}
    actual_tables = set(tables)
    missing_tables = sorted(_MANAGED_TABLES - actual_tables)
    extra_tables = sorted(actual_tables - _MANAGED_TABLES)
    if missing_tables:
        errors.append("missing_tables=" + "|".join(missing_tables))
    if extra_tables:
        errors.append("unexpected_tables=" + "|".join(extra_tables))
    if views:
        errors.append("unexpected_views=" + "|".join(views))

    for table_name, table in sorted(Base.metadata.tables.items()):
        if table_name not in actual_tables:
            continue
        table_exclusions = exclusions.get(table_name, frozenset())
        expected_columns = tuple(column for column in table.columns if column.name not in table_exclusions)
        actual_columns = {str(row["name"]): row for row in inspector.get_columns(table_name)}
        expected_names = {column.name for column in expected_columns}
        if set(actual_columns) != expected_names:
            errors.append(f"{table_name}.columns")
            continue
        for column in expected_columns:
            actual = actual_columns[column.name]
            if bool(actual["nullable"]) != bool(column.nullable):
                errors.append(f"{table_name}.{column.name}.nullable")
            if _type_signature(actual["type"], connection.dialect) != _type_signature(
                column.type,
                connection.dialect,
            ):
                errors.append(f"{table_name}.{column.name}.type")

        expected_pk = tuple(column.name for column in table.primary_key.columns)
        actual_pk = tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or ())
        if actual_pk != expected_pk:
            errors.append(f"{table_name}.primary_key")

        expected_unique = tuple(
            sorted(
                tuple(column.name for column in constraint.columns)
                for constraint in table.constraints
                if isinstance(constraint, UniqueConstraint)
                and not any(column.name in table_exclusions for column in constraint.columns)
            )
        )
        actual_unique_rows = tuple(inspector.get_unique_constraints(table_name))
        actual_unique = tuple(
            sorted(
                tuple(str(column) for column in (constraint.get("column_names") or ()))
                for constraint in actual_unique_rows
            )
        )
        if actual_unique != expected_unique:
            errors.append(f"{table_name}.unique_constraints")

        expected_foreign_keys = {
            (
                tuple(element.parent.name for element in constraint.elements),
                tuple(element.column.table.name for element in constraint.elements),
                tuple(element.column.name for element in constraint.elements),
                _referential_action(constraint.ondelete),
                _referential_action(constraint.onupdate),
            )
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
            and not any(element.parent.name in table_exclusions for element in constraint.elements)
        }
        actual_foreign_keys = {
            (
                tuple(str(column) for column in (constraint.get("constrained_columns") or ())),
                tuple(str(constraint.get("referred_table")) for _ in (constraint.get("referred_columns") or ())),
                tuple(str(column) for column in (constraint.get("referred_columns") or ())),
                _referential_action((constraint.get("options") or {}).get("ondelete")),
                _referential_action((constraint.get("options") or {}).get("onupdate")),
            )
            for constraint in inspector.get_foreign_keys(table_name)
        }
        if actual_foreign_keys != expected_foreign_keys:
            errors.append(f"{table_name}.foreign_keys")

        expected_checks = {
            _normalized_check_sql(str(constraint.sqltext))
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        actual_checks = {
            _normalized_check_sql(str(constraint.get("sqltext") or ""))
            for constraint in inspector.get_check_constraints(table_name)
        }
        excluded_check_sql = {
            _normalized_check_sql(expression) for expression in check_exclusions.get(table_name, frozenset())
        }
        expected_checks -= excluded_check_sql
        actual_checks -= excluded_check_sql
        if actual_checks != expected_checks:
            errors.append(f"{table_name}.check_constraints")

        expected_indexes = {
            (
                str(index.name),
                bool(index.unique),
                tuple(str(getattr(expression, "name", expression)) for expression in index.expressions),
            )
            for index in table.indexes
            if not any(
                str(getattr(expression, "name", expression)) in table_exclusions for expression in index.expressions
            )
        }
        actual_index_rows = tuple(inspector.get_indexes(table_name))
        actual_indexes = {
            (
                str(index.get("name")),
                bool(index.get("unique")),
                tuple(str(column) for column in (index.get("column_names") or ())),
            )
            for index in actual_index_rows
        }
        expected_unique_indexes = {
            (
                str(constraint.get("name")),
                tuple(str(column) for column in (constraint.get("column_names") or ())),
            )
            for constraint in actual_unique_rows
        } | {(name, columns) for name, unique, columns in expected_indexes if unique}
        unexpected_unique_index = any(
            bool(index.get("unique"))
            and (
                (
                    str(index.get("name")),
                    tuple(str(column) for column in (index.get("column_names") or ())),
                )
                not in expected_unique_indexes
                or bool(index.get("dialect_options"))
                or bool(index.get("expressions"))
                or bool(index.get("column_sorting"))
            )
            for index in actual_index_rows
        )
        if not expected_indexes <= actual_indexes or unexpected_unique_index:
            errors.append(f"{table_name}.indexes")

    if "alembic_version" in actual_tables:
        _compare_manual_table(
            inspector,
            connection.dialect,
            table_name="alembic_version",
            expected_columns={"version_num": (("string", 32), False)},
            expected_pk=("version_num",),
            errors=errors,
        )
    if "room_point_scale_state" in actual_tables:
        _compare_manual_table(
            inspector,
            connection.dialect,
            table_name="room_point_scale_state",
            expected_columns={
                "id": (("integer",), False),
                "scale_version": (("integer",), False),
                "migrated_at": (("datetime",), False),
            },
            expected_pk=("id",),
            errors=errors,
        )
        checks = {
            _normalized_check_sql(str(row.get("sqltext") or ""))
            for row in inspector.get_check_constraints("room_point_scale_state")
        }
        if "id=1" not in checks or "scale_version=10" not in checks:
            errors.append("room_point_scale_state.check_constraints")
        scale_rows = tuple(
            connection.execute(text("SELECT id, scale_version FROM room_point_scale_state ORDER BY id")).tuples()
        )
        if scale_rows != ((1, EXPECTED_ROOM_POINT_SCALE),):
            errors.append("room_point_scale_state.rows")

    if connection.dialect.name in {"mysql", "mariadb"}:
        charset = connection.execute(
            text("SELECT DEFAULT_CHARACTER_SET_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = DATABASE()")
        ).scalar_one_or_none()
        if str(charset).lower() != "utf8mb4":
            errors.append("database.character_set")

    return tuple(sorted(set(errors)))


def _compare_manual_table(
    inspector: object,
    dialect: Dialect,
    *,
    table_name: str,
    expected_columns: dict[str, tuple[tuple[object, ...], bool]],
    expected_pk: tuple[str, ...],
    errors: list[str],
) -> None:
    actual_columns = {str(row["name"]): row for row in inspector.get_columns(table_name)}  # type: ignore[attr-defined]
    if set(actual_columns) != set(expected_columns):
        errors.append(f"{table_name}.columns")
    else:
        for name, (expected_type, expected_nullable) in expected_columns.items():
            actual = actual_columns[name]
            if _type_signature(actual["type"], dialect) != expected_type:
                errors.append(f"{table_name}.{name}.type")
            if bool(actual["nullable"]) != expected_nullable:
                errors.append(f"{table_name}.{name}.nullable")
    actual_pk = tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or ())  # type: ignore[attr-defined]
    if actual_pk != expected_pk:
        errors.append(f"{table_name}.primary_key")


def _type_signature(value: TypeEngine[object], dialect: Dialect) -> tuple[object, ...]:
    implemented = value.dialect_impl(dialect)
    type_name = type(implemented).__name__.upper()
    if isinstance(implemented, JSON) or (
        type_name == "LONGTEXT" and str(getattr(implemented, "collation", "")).lower().endswith("_bin")
    ):
        return ("json",)
    if isinstance(implemented, Boolean) or (
        type_name == "TINYINT" and int(getattr(implemented, "display_width", 0) or 0) == 1
    ):
        return ("boolean",)
    if dialect.name == "sqlite" and (isinstance(implemented, BigInteger) or type_name == "BIGINT"):
        # SQLite gives BIGINT and INTEGER the same integer affinity.  Migrations
        # intentionally use INTEGER variants where rowid/autoincrement semantics
        # matter, while the MariaDB model contract remains BIGINT.
        return ("integer",)
    if isinstance(implemented, BigInteger) or type_name == "BIGINT":
        return ("bigint",)
    if isinstance(implemented, Integer):
        return ("integer",)
    if isinstance(implemented, Numeric):
        return ("numeric", implemented.precision, implemented.scale)
    if isinstance(implemented, DateTime):
        return ("datetime",)
    if isinstance(implemented, Text):
        return ("text",)
    if isinstance(implemented, String):
        return ("string", implemented.length)
    compiled = str(implemented.compile(dialect=dialect)).upper()
    compiled = re.sub(r"\b(DECIMAL)\b", "NUMERIC", compiled)
    compiled = re.sub(r"\b(BIGINT|INTEGER)\(\d+\)", r"\1", compiled)
    return ("compiled", " ".join(compiled.split()))


def _referential_action(value: object) -> str:
    if value is None:
        return "restrict"
    normalized = " ".join(str(value).strip().lower().replace("_", " ").split())
    return "restrict" if normalized in {"no action", "restrict"} else normalized


def _normalized_check_sql(value: str) -> str:
    tokens = _check_sql_tokens(value)
    if not tokens:
        return ""
    try:
        expression, next_index = _parse_check_or(tokens, 0)
    except ValueError:
        return "".join(tokens)
    if next_index != len(tokens):
        return "".join(tokens)
    return _serialize_check_expression(expression)


def _check_sql_tokens(value: str) -> tuple[str, ...]:
    tokens: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character.isspace():
            index += 1
            continue
        if character == "'":
            literal = [character]
            index += 1
            while index < len(value):
                literal_character = value[index]
                literal.append(literal_character)
                index += 1
                if literal_character != "'":
                    continue
                if index < len(value) and value[index] == "'":
                    literal.append("'")
                    index += 1
                    continue
                break
            else:
                raise ValueError("unterminated SQL string literal")
            tokens.append("".join(literal))
            continue
        if character in '`"[':
            closing = "]" if character == "[" else character
            end = value.find(closing, index + 1)
            if end < 0:
                raise ValueError("unterminated quoted SQL identifier")
            tokens.append(value[index + 1 : end].lower())
            index = end + 1
            continue
        if character.isalpha() or character == "_":
            end = index + 1
            while end < len(value) and (value[end].isalnum() or value[end] in "_$"):
                end += 1
            tokens.append(value[index:end].lower())
            index = end
            continue
        if character.isdigit():
            end = index + 1
            while end < len(value) and (value[end].isdigit() or value[end] == "."):
                end += 1
            tokens.append(value[index:end])
            index = end
            continue
        pair = value[index : index + 2]
        if pair in {"<=", ">=", "<>", "!=", "=="}:
            tokens.append(pair)
            index += 2
            continue
        tokens.append(character.lower())
        index += 1
    return tuple(tokens)


def _parse_check_or(tokens: tuple[str, ...], index: int) -> tuple[tuple[object, ...], int]:
    first, index = _parse_check_and(tokens, index)
    children = [first]
    while index < len(tokens) and tokens[index] == "or":
        child, index = _parse_check_and(tokens, index + 1)
        children.append(child)
    return _check_logical_node("or", children), index


def _parse_check_and(tokens: tuple[str, ...], index: int) -> tuple[tuple[object, ...], int]:
    first, index = _parse_check_primary(tokens, index)
    children = [first]
    while index < len(tokens) and tokens[index] == "and":
        child, index = _parse_check_primary(tokens, index + 1)
        children.append(child)
    return _check_logical_node("and", children), index


def _parse_check_primary(tokens: tuple[str, ...], index: int) -> tuple[tuple[object, ...], int]:
    if index >= len(tokens):
        raise ValueError("missing CHECK expression")
    if tokens[index] == "(":
        expression, index = _parse_check_or(tokens, index + 1)
        if index >= len(tokens) or tokens[index] != ")":
            raise ValueError("unbalanced CHECK expression")
        return expression, index + 1
    return _parse_check_atom(tokens, index)


def _parse_check_atom(tokens: tuple[str, ...], index: int) -> tuple[tuple[object, ...], int]:
    atom: list[str] = []
    parenthesis_depth = 0
    case_depth = 0
    between_needs_and = False
    while index < len(tokens):
        token = tokens[index]
        if parenthesis_depth == 0 and case_depth == 0:
            if token == ")" or token == "or":
                break
            if token == "and":
                if between_needs_and:
                    between_needs_and = False
                else:
                    break
            elif token == "between":
                between_needs_and = True
        if token == "case":
            case_depth += 1
        elif token == "end" and case_depth:
            case_depth -= 1
        elif token == "(":
            parenthesis_depth += 1
        elif token == ")":
            if parenthesis_depth == 0:
                break
            parenthesis_depth -= 1
        atom.append(token)
        index += 1
    if not atom or parenthesis_depth or case_depth or between_needs_and:
        raise ValueError("invalid CHECK predicate")
    return ("atom", "".join(atom)), index


def _check_logical_node(kind: str, children: list[tuple[object, ...]]) -> tuple[object, ...]:
    flattened: list[tuple[object, ...]] = []
    for child in children:
        if child[0] == kind:
            flattened.extend(child[1])  # type: ignore[arg-type]
        else:
            flattened.append(child)
    if len(flattened) == 1:
        return flattened[0]
    return kind, tuple(flattened)


def _serialize_check_expression(expression: tuple[object, ...]) -> str:
    kind = str(expression[0])
    if kind == "atom":
        return str(expression[1])
    children = expression[1]
    if not isinstance(children, tuple):
        raise ValueError("invalid CHECK expression tree")
    return f"{kind}(" + ",".join(_serialize_check_expression(child) for child in children) + ")"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed fresh, current, or explicitly approved forward deployment migration gate.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="read and validate database state without running Alembic",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("alembic.ini"),
        help="Alembic configuration path (default: alembic.ini)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        state = run_safe_migration(
            get_settings().database_url,
            check_only=args.check,
            alembic_config_path=args.config,
        )
    except UnsafeMigrationStateError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    action = "check passed" if args.check else "migration completed"
    print(f"safe migration {action}: disposition={state.disposition} expected_head={EXPECTED_ALEMBIC_HEAD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
