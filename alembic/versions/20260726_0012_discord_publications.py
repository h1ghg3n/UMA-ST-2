"""add generic durable Discord publications

Revision ID: 20260726_0012
Revises: 20260726_0011
Create Date: 2026-07-26 00:12:00.000000
"""

import re
import runpy
from collections.abc import Callable, Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "20260726_0012"
down_revision: str | Sequence[str] | None = "20260726_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREVIOUS_REVISION = "20260726_0011"
PUBLICATION_TABLE = "discord_publications"
AUDIT_TABLE = "discord_publication_audits"
TARGET_TABLES = {PUBLICATION_TABLE, AUDIT_TABLE}
BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

ColumnSignature = tuple[str, int | None, bool, str | None, bool]

PUBLICATION_COLUMNS: dict[str, ColumnSignature] = {
    "id": ("bigint", 64, False, None, True),
    "guild_id": ("string", 32, False, None, False),
    "destination_kind": ("string", 32, False, None, False),
    "event_type": ("string", 64, False, None, False),
    "event_key": ("string", 128, False, None, False),
    "source_kind": ("string", 32, False, None, False),
    "source_id": ("bigint", 64, False, None, False),
    "target_channel_id": ("string", 32, True, None, False),
    "payload_json": ("json", None, False, None, False),
    "payload_fingerprint": ("string", 64, False, None, False),
    "status": ("string", 32, False, None, False),
    "attempt_count": ("integer", 32, False, "0", False),
    "discord_message_id": ("string", 32, True, None, False),
    "last_error_code": ("string", 64, True, None, False),
    "failure_stage": ("string", 16, True, None, False),
    "attempt_started_at": ("datetime", None, True, None, False),
    "published_at": ("datetime", None, True, None, False),
    "created_at": ("datetime", None, False, "current_timestamp", False),
    "updated_at": ("datetime", None, False, "current_timestamp", False),
}
AUDIT_COLUMNS: dict[str, ColumnSignature] = {
    "id": ("bigint", 64, False, None, True),
    "publication_id": ("bigint", 64, False, None, False),
    "action": ("string", 64, False, None, False),
    "actor_discord_user_id": ("string", 32, False, None, False),
    "idempotency_key": ("string", 128, False, None, False),
    "request_fingerprint": ("string", 64, False, None, False),
    "before_json": ("json", None, True, None, False),
    "after_json": ("json", None, False, None, False),
    "reason": ("string", 255, True, None, False),
    "created_at": ("datetime", None, False, "current_timestamp", False),
}

DESTINATION_CHECK = "destination_kind IN ('win5_announcement', 'room_match_announcement', 'log_mirror')"
STATUS_CHECK = "status IN ('suppressed', 'awaiting_channel', 'ready', 'pending', 'sent', 'failed', 'delivery_unknown')"
NONNEGATIVE_ATTEMPT_COUNT_CHECK = "attempt_count >= 0"
STATE_CHECK = (
    "(status IN ('suppressed', 'awaiting_channel') AND target_channel_id IS NULL "
    "AND attempt_count = 0 AND discord_message_id IS NULL AND last_error_code IS NULL "
    "AND failure_stage IS NULL AND attempt_started_at IS NULL AND published_at IS NULL) OR "
    "(status = 'ready' AND target_channel_id IS NOT NULL AND attempt_count = 0 "
    "AND discord_message_id IS NULL AND last_error_code IS NULL AND failure_stage IS NULL "
    "AND attempt_started_at IS NULL AND published_at IS NULL) OR "
    "(status = 'pending' AND target_channel_id IS NOT NULL AND attempt_count > 0 "
    "AND discord_message_id IS NULL AND last_error_code IS NULL AND failure_stage IS NULL "
    "AND attempt_started_at IS NOT NULL AND published_at IS NULL) OR "
    "(status = 'sent' AND target_channel_id IS NOT NULL AND attempt_count > 0 "
    "AND discord_message_id IS NOT NULL AND last_error_code IS NULL AND failure_stage IS NULL "
    "AND attempt_started_at IS NOT NULL AND published_at IS NOT NULL) OR "
    "(status = 'failed' AND discord_message_id IS NULL AND last_error_code IS NOT NULL "
    "AND published_at IS NULL AND ((failure_stage = 'channel' AND target_channel_id IS NULL "
    "AND attempt_count = 0 AND attempt_started_at IS NULL) OR "
    "(failure_stage = 'send' AND target_channel_id IS NOT NULL AND attempt_count > 0 "
    "AND attempt_started_at IS NOT NULL))) OR "
    "(status = 'delivery_unknown' AND target_channel_id IS NOT NULL AND attempt_count > 0 "
    "AND discord_message_id IS NULL AND last_error_code IS NOT NULL AND failure_stage = 'send' "
    "AND attempt_started_at IS NOT NULL AND published_at IS NULL)"
)
MARIADB_STATE_CHECK = (
    "status IN ('suppressed', 'awaiting_channel') AND target_channel_id IS NULL "
    "AND attempt_count = 0 AND discord_message_id IS NULL AND last_error_code IS NULL "
    "AND failure_stage IS NULL AND attempt_started_at IS NULL AND published_at IS NULL OR "
    "status = 'ready' AND target_channel_id IS NOT NULL AND attempt_count = 0 "
    "AND discord_message_id IS NULL AND last_error_code IS NULL AND failure_stage IS NULL "
    "AND attempt_started_at IS NULL AND published_at IS NULL OR "
    "status = 'pending' AND target_channel_id IS NOT NULL AND attempt_count > 0 "
    "AND discord_message_id IS NULL AND last_error_code IS NULL AND failure_stage IS NULL "
    "AND attempt_started_at IS NOT NULL AND published_at IS NULL OR "
    "status = 'sent' AND target_channel_id IS NOT NULL AND attempt_count > 0 "
    "AND discord_message_id IS NOT NULL AND last_error_code IS NULL AND failure_stage IS NULL "
    "AND attempt_started_at IS NOT NULL AND published_at IS NOT NULL OR "
    "status = 'failed' AND discord_message_id IS NULL AND last_error_code IS NOT NULL "
    "AND published_at IS NULL AND (failure_stage = 'channel' AND target_channel_id IS NULL "
    "AND attempt_count = 0 AND attempt_started_at IS NULL OR "
    "failure_stage = 'send' AND target_channel_id IS NOT NULL AND attempt_count > 0 "
    "AND attempt_started_at IS NOT NULL) OR "
    "status = 'delivery_unknown' AND target_channel_id IS NOT NULL AND attempt_count > 0 "
    "AND discord_message_id IS NULL AND last_error_code IS NOT NULL AND failure_stage = 'send' "
    "AND attempt_started_at IS NOT NULL AND published_at IS NULL"
)
PUBLICATION_CHECKS = {
    "ck_discord_publications_destination_kind": DESTINATION_CHECK,
    "ck_discord_publications_status": STATUS_CHECK,
    "ck_discord_publications_nonnegative_attempt_count": NONNEGATIVE_ATTEMPT_COUNT_CHECK,
    "ck_discord_publications_state": STATE_CHECK,
}


def upgrade() -> None:
    connection = op.get_bind()
    mode = _preflight(connection)
    if mode == "previous":
        _create_target()
    _verify_target(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _require_revision(connection, revision)
    _verify_target(connection)
    if _has_row(connection, AUDIT_TABLE) or _has_row(connection, PUBLICATION_TABLE):
        raise RuntimeError("Discord publication downgrade refused after operational history")
    op.drop_table(AUDIT_TABLE)
    op.drop_table(PUBLICATION_TABLE)


def _create_target() -> None:
    op.create_table(
        PUBLICATION_TABLE,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("destination_kind", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("event_key", sa.String(128), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_id", BIGINT, nullable=False),
        sa.Column("target_channel_id", sa.String(32), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("discord_message_id", sa.String(32), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("failure_stage", sa.String(16), nullable=True),
        sa.Column("attempt_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "guild_id",
            "destination_kind",
            "event_key",
            name="uq_discord_publications_guild_destination_event",
        ),
        sa.CheckConstraint(DESTINATION_CHECK, name="destination_kind"),
        sa.CheckConstraint(STATUS_CHECK, name="status"),
        sa.CheckConstraint(NONNEGATIVE_ATTEMPT_COUNT_CHECK, name="nonnegative_attempt_count"),
        sa.CheckConstraint(STATE_CHECK, name="state"),
        sa.Index(
            "ix_discord_publications_status_attempt_started_at",
            "status",
            "attempt_started_at",
            "id",
        ),
    )
    op.create_table(
        AUDIT_TABLE,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("publication_id", BIGINT, nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("actor_discord_user_id", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            [f"{PUBLICATION_TABLE}.id"],
            name="fk_discord_publication_audits_publication",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_discord_publication_audits_idempotency_key",
        ),
    )


def _preflight(connection: sa.Connection) -> str:
    _require_revision(connection, PREVIOUS_REVISION)
    tables = set(sa.inspect(connection).get_table_names())
    present = tables & TARGET_TABLES
    if not present:
        _verify_exact_previous(connection)
        return "previous"
    if present != TARGET_TABLES:
        _unsupported("partial Discord publication target is present")
    try:
        _verify_target(connection)
    except RuntimeError as exc:
        _unsupported(f"target-shaped Discord publication verification failed: {exc}")
    return "target"


def _verify_exact_previous(connection: sa.Connection) -> None:
    namespace = runpy.run_path(str(Path(__file__).with_name("20260726_0011_guild_discord_settings.py")))
    verifier = namespace.get("_verify_target")
    if not isinstance(verifier, Callable):
        _unsupported("canonical 0011 verifier is unavailable")
    try:
        verifier(connection)
    except RuntimeError as exc:
        _unsupported(f"canonical 0011 target verification failed: {exc}")


def _verify_target(connection: sa.Connection) -> None:
    _verify_exact_previous(connection)
    inspector = sa.inspect(connection)
    if not TARGET_TABLES.issubset(set(inspector.get_table_names())):
        raise RuntimeError("Discord publication target tables are missing")
    _verify_columns(inspector, PUBLICATION_TABLE, PUBLICATION_COLUMNS)
    _verify_columns(inspector, AUDIT_TABLE, AUDIT_COLUMNS)
    _verify_primary_key(inspector, PUBLICATION_TABLE)
    _verify_primary_key(inspector, AUDIT_TABLE)
    _verify_foreign_keys(inspector, PUBLICATION_TABLE, {})
    _verify_foreign_keys(
        inspector,
        AUDIT_TABLE,
        {
            "fk_discord_publication_audits_publication": (
                ("publication_id",),
                PUBLICATION_TABLE,
                ("id",),
            )
        },
    )
    _verify_uniques(
        inspector,
        PUBLICATION_TABLE,
        {
            "uq_discord_publications_guild_destination_event": (
                "guild_id",
                "destination_kind",
                "event_key",
            )
        },
    )
    _verify_uniques(
        inspector,
        AUDIT_TABLE,
        {"uq_discord_publication_audits_idempotency_key": ("idempotency_key",)},
    )
    _verify_checks(
        inspector,
        PUBLICATION_TABLE,
        PUBLICATION_CHECKS,
        json_columns={"payload_json"},
    )
    _verify_checks(
        inspector,
        AUDIT_TABLE,
        {},
        json_columns={"before_json", "after_json"},
    )
    mariadb = inspector.bind.dialect.name in {"mariadb", "mysql"}
    _verify_indexes(
        inspector,
        PUBLICATION_TABLE,
        {
            "ix_discord_publications_status_attempt_started_at": (
                ("status", "attempt_started_at", "id"),
                False,
            ),
            **(
                {
                    "uq_discord_publications_guild_destination_event": (
                        ("guild_id", "destination_kind", "event_key"),
                        True,
                    )
                }
                if mariadb
                else {}
            ),
        },
    )
    _verify_indexes(
        inspector,
        AUDIT_TABLE,
        (
            {
                "fk_discord_publication_audits_publication": (("publication_id",), False),
                "uq_discord_publication_audits_idempotency_key": (("idempotency_key",), True),
            }
            if mariadb
            else {}
        ),
    )
    _verify_semantics(connection)


def _verify_columns(
    inspector: sa.Inspector,
    table_name: str,
    expected: dict[str, ColumnSignature],
) -> None:
    actual_rows = inspector.get_columns(table_name)
    if tuple(str(item["name"]) for item in actual_rows) != tuple(expected):
        raise RuntimeError(f"{table_name} columns or column order differ")
    dialect = inspector.bind.dialect.name
    for item in actual_rows:
        name = str(item["name"])
        family, width, nullable, default, primary_key = expected[name]
        expected_type = ("integer", 32) if dialect == "sqlite" and family == "bigint" else (family, width)
        if _type_signature(item["type"], dialect) != expected_type:
            raise RuntimeError(f"{table_name}.{name} type or width differs")
        if bool(item.get("nullable")) is not nullable:
            raise RuntimeError(f"{table_name}.{name} nullability differs")
        if _normalize_default(item.get("default")) != default:
            raise RuntimeError(f"{table_name}.{name} server default differs")
        if "primary_key" in item and bool(item.get("primary_key")) is not primary_key:
            raise RuntimeError(f"{table_name}.{name} primary-key membership differs")
        if item.get("computed") is not None or item.get("identity") is not None:
            raise RuntimeError(f"{table_name}.{name} generated-column metadata differs")
        if name != "id" and item.get("autoincrement") is True:
            raise RuntimeError(f"{table_name}.{name} must not auto increment")


def _type_signature(value: sa.types.TypeEngine, dialect: str) -> tuple[str, int | None]:
    name = type(value).__name__.upper()
    if dialect == "sqlite":
        if name == "INTEGER":
            return ("integer", 32)
    else:
        if (
            name == "BIGINT"
            and not bool(getattr(value, "unsigned", False))
            and getattr(value, "display_width", None) in {None, 20}
        ):
            return ("bigint", 64)
        if (
            name in {"INTEGER", "INT"}
            and not bool(getattr(value, "unsigned", False))
            and getattr(value, "display_width", None) in {None, 11}
        ):
            return ("integer", 32)
    if name == "VARCHAR" and isinstance(value, sa.String) and not isinstance(value, sa.Text):
        return ("string", getattr(value, "length", None))
    if name == "JSON":
        return ("json", None)
    if (
        dialect in {"mariadb", "mysql"}
        and name == "LONGTEXT"
        and str(getattr(value, "charset", "")).lower() == "utf8mb4"
        and str(getattr(value, "collation", "")).lower() == "utf8mb4_bin"
    ):
        return ("json", None)
    if name == "DATETIME" and isinstance(value, sa.DateTime) and getattr(value, "fsp", None) in {None, 0}:
        return ("datetime", None)
    return (f"unsupported:{name.lower()}", None)


def _normalize_default(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    normalized = _strip_outer_parentheses(normalized)
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        normalized = normalized[1:-1]
    normalized = re.sub(r"\s+", "", normalized)
    if normalized in {"current_timestamp", "current_timestamp()"}:
        return "current_timestamp"
    return normalized


def _verify_primary_key(inspector: sa.Inspector, table_name: str) -> None:
    primary = inspector.get_pk_constraint(table_name)
    if tuple(primary.get("constrained_columns") or ()) != ("id",):
        raise RuntimeError(f"{table_name} primary key differs")
    dialect = inspector.bind.dialect.name
    allowed_names = {None, "PRIMARY", f"pk_{table_name}"} if dialect in {"mariadb", "mysql"} else {f"pk_{table_name}"}
    if primary.get("name") not in allowed_names:
        raise RuntimeError(f"{table_name} primary-key name differs")
    identifier = next(item for item in inspector.get_columns(table_name) if item["name"] == "id")
    if dialect == "sqlite":
        if type(identifier["type"]).__name__.upper() != "INTEGER":
            raise RuntimeError(f"{table_name}.id must alias the SQLite rowid")
        create_sql = inspector.bind.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table"),
            {"table": table_name},
        ).scalar_one_or_none()
        if create_sql is None or re.search(
            r"\b(?:WITHOUT\s+ROWID|AUTOINCREMENT|STRICT)\b",
            str(create_sql),
            flags=re.IGNORECASE,
        ):
            raise RuntimeError(f"{table_name}.id rowid/autoincrement contract differs")
        primary_indexes = [
            row
            for row in inspector.bind.exec_driver_sql(f'PRAGMA index_list("{table_name}")').mappings()
            if str(row.get("origin") or "").lower() == "pk"
        ]
        if primary_indexes:
            raise RuntimeError(f"{table_name}.id does not alias the SQLite rowid")
    elif dialect in {"mariadb", "mysql"}:
        if identifier.get("autoincrement") is not True:
            raise RuntimeError(f"{table_name}.id must use AUTO_INCREMENT")
    else:
        raise RuntimeError(f"unsupported Discord publication dialect: {dialect}")


def _verify_foreign_keys(
    inspector: sa.Inspector,
    table_name: str,
    expected: dict[str, tuple[tuple[str, ...], str, tuple[str, ...]]],
) -> None:
    actual = {}
    for item in inspector.get_foreign_keys(table_name):
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError(f"{table_name} has an unnamed foreign key")
        if item.get("referred_schema") not in {None, inspector.default_schema_name}:
            raise RuntimeError(f"{table_name}.{name} refers to another schema")
        options = item.get("options") or {}
        if (
            _referential_action(options.get("ondelete")) != "restrict"
            or _referential_action(options.get("onupdate")) != "restrict"
            or options.get("match") not in {None, "NONE"}
            or options.get("deferrable") not in {None, False}
            or options.get("initially") is not None
        ):
            raise RuntimeError(f"{table_name}.{name} foreign-key action differs")
        actual[name] = (
            tuple(item.get("constrained_columns") or ()),
            str(item.get("referred_table")),
            tuple(item.get("referred_columns") or ()),
        )
    if actual != expected:
        raise RuntimeError(f"{table_name} foreign keys differ")


def _referential_action(value: object) -> str:
    normalized = str(value or "RESTRICT").strip().upper()
    return "restrict" if normalized in {"RESTRICT", "NO ACTION"} else normalized.lower()


def _verify_uniques(
    inspector: sa.Inspector,
    table_name: str,
    expected: dict[str, tuple[str, ...]],
) -> None:
    actual = {
        str(item.get("name")): tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints(table_name)
    }
    if actual != expected:
        raise RuntimeError(f"{table_name} unique constraints or column order differ")


def _verify_checks(
    inspector: sa.Inspector,
    table_name: str,
    expected: dict[str, str],
    *,
    json_columns: set[str],
) -> None:
    dialect = inspector.bind.dialect.name
    actual = {}
    implicit_json_columns = set()
    for item in inspector.get_check_constraints(table_name):
        expression = _normalize_sql(item.get("sqltext"))
        json_column = _implicit_json_check_column(expression)
        if dialect in {"mariadb", "mysql"} and json_column is not None:
            implicit_json_columns.add(json_column)
            continue
        actual[str(item.get("name"))] = expression
    normalized_expected = {
        name: _normalize_expected_check(expression, dialect) for name, expression in expected.items()
    }
    if actual != normalized_expected:
        raise RuntimeError(f"{table_name} check expressions differ")
    if dialect in {"mariadb", "mysql"} and frozenset(implicit_json_columns) not in {
        frozenset(),
        frozenset(json_columns),
    }:
        raise RuntimeError(f"{table_name} JSON validity checks differ")


def _implicit_json_check_column(expression: str) -> str | None:
    match = re.fullmatch(r"json_valid\((?P<column>[a-z0-9_]+)\)", expression)
    return match.group("column") if match else None


def _normalize_expected_check(value: str, dialect: str) -> str:
    if dialect not in {"mariadb", "mysql"}:
        return _normalize_sql(value)
    if value == STATE_CHECK:
        return _normalize_sql(MARIADB_STATE_CHECK)
    previous = None
    while previous != value:
        previous = value
        value = re.sub(
            r"\((?=[^()]*\band\b)(?![^()]*\bor\b)([^()]*)\)",
            r"\1",
            value,
            flags=re.IGNORECASE,
        )
    return _normalize_sql(value)


def _normalize_sql(value: object) -> str:
    normalized = str(value or "").lower().replace("`", "").replace('"', "").replace("_utf8mb4", "")
    normalized = re.sub(r"\s+", "", normalized)
    return _strip_outer_parentheses(normalized)


def _strip_outer_parentheses(value: str) -> str:
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        wraps_all = True
        quote: str | None = None
        for index, character in enumerate(value):
            if quote is not None:
                if character == quote:
                    quote = None
                continue
            if character in {"'", '"'}:
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    wraps_all = False
                    break
        if not wraps_all or depth != 0:
            break
        value = value[1:-1]
    return value


def _verify_indexes(
    inspector: sa.Inspector,
    table_name: str,
    expected: dict[str, tuple[tuple[str, ...], bool]],
) -> None:
    actual = {}
    for item in inspector.get_indexes(table_name):
        name = str(item.get("name"))
        columns = tuple(item.get("column_names") or ())
        expressions = tuple(item.get("expressions") or ())
        if (
            not columns
            or (expressions and expressions != columns)
            or item.get("include_columns")
            or _meaningful_index_option(item.get("dialect_options") or {})
        ):
            raise RuntimeError(f"{table_name}.{name} index options differ")
        actual[name] = (columns, bool(item.get("unique")))
    if actual != expected:
        raise RuntimeError(f"{table_name} indexes or column order differ")


def _meaningful_index_option(value: object) -> bool:
    if isinstance(value, dict):
        return any(_meaningful_index_option(item) for item in value.values())
    if isinstance(value, (tuple, list, set)):
        return any(_meaningful_index_option(item) for item in value)
    return value not in {None, "", 0}


def _verify_semantics(connection: sa.Connection) -> None:
    invalid = connection.execute(
        sa.text(
            "SELECT id FROM discord_publications WHERE payload_fingerprint IS NULL "
            "OR LENGTH(payload_fingerprint) <> 64 OR source_id <= 0 LIMIT 1"
        )
    ).first()
    if invalid is not None:
        raise RuntimeError("Discord publication persisted semantics are invalid")


def _require_revision(connection: sa.Connection, expected: str) -> None:
    if "alembic_version" not in set(sa.inspect(connection).get_table_names()):
        _unsupported("alembic_version table is missing")
    current = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    if current != expected:
        _unsupported(f"database is not at exact revision {expected}")


def _has_row(connection: sa.Connection, table_name: str) -> bool:
    return connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first() is not None


def _unsupported(detail: str) -> None:
    raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: {detail}")
