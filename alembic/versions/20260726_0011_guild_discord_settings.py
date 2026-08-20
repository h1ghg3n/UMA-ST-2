"""add persisted per-guild Discord operational settings

Revision ID: 20260726_0011
Revises: 20260726_0010
Create Date: 2026-07-26 00:11:00.000000
"""

import runpy
from collections.abc import Callable, Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "20260726_0011"
down_revision: str | Sequence[str] | None = "20260726_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREVIOUS_REVISION = "20260726_0010"
BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
SETTINGS_TABLE = "guild_discord_settings"
AUDIT_TABLE = "guild_discord_settings_audits"
TARGET_TABLES = {SETTINGS_TABLE, AUDIT_TABLE}

SETTINGS_COLUMNS = {
    "id": ("integer", False, None),
    "guild_id": ("string", False, 32),
    "win5_announcement_channel_id": ("string", True, 32),
    "room_match_announcement_channel_id": ("string", True, 32),
    "log_channel_id": ("string", True, 32),
    "default_timezone": ("string", False, 3),
    "win5_announcements_enabled": ("boolean", False, None),
    "room_match_announcements_enabled": ("boolean", False, None),
    "revision_number": ("integer", False, None),
    "created_at": ("datetime", False, None),
    "updated_at": ("datetime", False, None),
}
SETTINGS_UNIQUES = {
    "uq_guild_discord_settings_guild_id": ("guild_id",),
}
SETTINGS_CHECKS = {
    "ck_guild_discord_settings_positive_revision",
    "ck_guild_discord_settings_default_timezone",
}

AUDIT_COLUMNS = {
    "id": ("integer", False, None),
    "guild_discord_settings_id": ("integer", False, None),
    "guild_id": ("string", False, 32),
    "action": ("string", False, 64),
    "actor_discord_user_id": ("string", False, 32),
    "idempotency_key": ("string", False, 128),
    "request_fingerprint": ("string", False, 64),
    "before_json": ("json", False, None),
    "after_json": ("json", False, None),
    "reason": ("string", False, 255),
    "created_at": ("datetime", False, None),
}
AUDIT_FOREIGN_KEYS = {
    "fk_guild_settings_audits_settings": (
        ("guild_discord_settings_id",),
        SETTINGS_TABLE,
        ("id",),
    ),
}
AUDIT_UNIQUES = {
    "uq_guild_discord_settings_audits_idempotency_key": ("idempotency_key",),
}
AUDIT_CHECKS = {
    "ck_guild_discord_settings_audits_action",
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
    if _has_row(connection, AUDIT_TABLE) or _has_row(connection, SETTINGS_TABLE):
        raise RuntimeError(
            "guild Discord settings downgrade refused after operational history; restore the verified backup"
        )
    op.drop_table(AUDIT_TABLE)
    op.drop_table(SETTINGS_TABLE)


def _create_target() -> None:
    op.create_table(
        SETTINGS_TABLE,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("win5_announcement_channel_id", sa.String(32), nullable=True),
        sa.Column("room_match_announcement_channel_id", sa.String(32), nullable=True),
        sa.Column("log_channel_id", sa.String(32), nullable=True),
        sa.Column("default_timezone", sa.String(3), server_default="KST", nullable=False),
        sa.Column("win5_announcements_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "room_match_announcements_enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("revision_number", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("guild_id", name="uq_guild_discord_settings_guild_id"),
        sa.CheckConstraint("revision_number > 0", name="positive_revision"),
        sa.CheckConstraint("default_timezone IN ('KST', 'UTC')", name="default_timezone"),
    )
    op.create_table(
        AUDIT_TABLE,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("guild_discord_settings_id", BIGINT, nullable=False),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("actor_discord_user_id", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=False),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["guild_discord_settings_id"],
            [f"{SETTINGS_TABLE}.id"],
            name="fk_guild_settings_audits_settings",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_guild_discord_settings_audits_idempotency_key",
        ),
        sa.CheckConstraint(
            "action = 'guild_discord_settings_update'",
            name="action",
        ),
    )


def _preflight(connection: sa.Connection) -> str:
    _require_revision(connection, PREVIOUS_REVISION)
    tables = set(sa.inspect(connection).get_table_names())
    present = tables & TARGET_TABLES
    if not present:
        _verify_exact_previous_head(connection)
        return "previous"
    if present == TARGET_TABLES:
        try:
            _verify_target(connection)
        except RuntimeError as exc:
            _unsupported(f"target-shaped guild Discord settings verification failed: {exc}")
        return "target"
    _unsupported("partial or hybrid guild Discord settings tables are present")


def _verify_exact_previous_head(connection: sa.Connection) -> None:
    previous_path = Path(__file__).with_name("20260726_0010_room_match_result_lifecycle.py")
    namespace = runpy.run_path(str(previous_path))
    verifier = namespace.get("_verify_target")
    if not isinstance(verifier, Callable):
        _unsupported("canonical 0010 verifier is unavailable")
    try:
        verifier(connection)
    except RuntimeError as exc:
        _unsupported(f"canonical 0010 target verification failed: {exc}")


def _verify_target(connection: sa.Connection) -> None:
    _verify_exact_previous_head(connection)
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if not TARGET_TABLES.issubset(tables):
        raise RuntimeError("guild Discord settings target verification failed: required tables are missing")

    _verify_table(
        inspector,
        SETTINGS_TABLE,
        columns=SETTINGS_COLUMNS,
        foreign_keys={},
        uniques=SETTINGS_UNIQUES,
        checks=SETTINGS_CHECKS,
        mariadb_indexes={"uq_guild_discord_settings_guild_id"},
    )
    _verify_table(
        inspector,
        AUDIT_TABLE,
        columns=AUDIT_COLUMNS,
        foreign_keys=AUDIT_FOREIGN_KEYS,
        uniques=AUDIT_UNIQUES,
        checks=AUDIT_CHECKS,
        mariadb_indexes={
            "fk_guild_settings_audits_settings",
            "uq_guild_discord_settings_audits_idempotency_key",
        },
    )
    _validate_semantics(connection)


def _verify_table(
    inspector: sa.Inspector,
    table_name: str,
    *,
    columns: dict[str, tuple[str, bool, int | None]],
    foreign_keys: dict[str, tuple[tuple[str, ...], str, tuple[str, ...]]],
    uniques: dict[str, tuple[str, ...]],
    checks: set[str],
    mariadb_indexes: set[str],
) -> None:
    actual_columns = inspector.get_columns(table_name)
    if {item["name"] for item in actual_columns} != set(columns):
        _target_error(f"{table_name} columns differ from the exact contract")
    for item in actual_columns:
        family, nullable, length = columns[str(item["name"])]
        if (
            not _type_matches(
                item["type"],
                family=family,
                dialect_name=inspector.bind.dialect.name,
            )
            or bool(item["nullable"]) is not nullable
        ):
            _target_error(f"{table_name}.{item['name']} type or nullability is invalid")
        if family == "string" and getattr(item["type"], "length", None) != length:
            _target_error(f"{table_name}.{item['name']} length is invalid")
        if item.get("computed") is not None or item.get("identity") is not None:
            _target_error(f"{table_name}.{item['name']} cannot be generated or identity")

    primary = inspector.get_pk_constraint(table_name)
    if tuple(primary.get("constrained_columns") or ()) != ("id",):
        _target_error(f"{table_name} primary key differs from the exact contract")
    allowed_primary_names = (
        {None, "PRIMARY", f"pk_{table_name}"}
        if inspector.bind.dialect.name in {"mariadb", "mysql"}
        else {f"pk_{table_name}"}
    )
    if primary.get("name") not in allowed_primary_names:
        _target_error(f"{table_name} primary key name differs from the exact contract")

    actual_foreign_keys = {}
    for item in inspector.get_foreign_keys(table_name):
        name = item.get("name")
        if not name:
            _target_error(f"{table_name} has an unnamed foreign key")
        actual_foreign_keys[str(name)] = (
            tuple(item.get("constrained_columns") or ()),
            str(item.get("referred_table")),
            tuple(item.get("referred_columns") or ()),
        )
    if actual_foreign_keys != foreign_keys:
        _target_error(f"{table_name} foreign keys differ from the exact contract")

    actual_uniques = {
        str(item.get("name")): tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints(table_name)
    }
    if actual_uniques != uniques:
        _target_error(f"{table_name} unique constraints differ from the exact contract")

    actual_checks = {str(item.get("name")) for item in inspector.get_check_constraints(table_name)}
    if actual_checks != checks:
        _target_error(f"{table_name} check constraints differ from the exact contract")

    expected_indexes = mariadb_indexes if inspector.bind.dialect.name in {"mariadb", "mysql"} else set()
    actual_indexes = {str(item.get("name")) for item in inspector.get_indexes(table_name)}
    if actual_indexes != expected_indexes:
        _target_error(f"{table_name} indexes differ from the exact contract")


def _validate_semantics(connection: sa.Connection) -> None:
    rows = connection.execute(
        sa.text(
            "SELECT id, guild_id, win5_announcement_channel_id, "
            "room_match_announcement_channel_id, log_channel_id, "
            "default_timezone, win5_announcements_enabled, "
            "room_match_announcements_enabled, revision_number "
            f"FROM {SETTINGS_TABLE} ORDER BY id"
        )
    ).mappings()
    for row in rows:
        if (
            not _snowflake(row["guild_id"])
            or not _optional_snowflake(row["win5_announcement_channel_id"])
            or not _optional_snowflake(row["room_match_announcement_channel_id"])
            or not _optional_snowflake(row["log_channel_id"])
            or row["default_timezone"] not in {"KST", "UTC"}
            or row["win5_announcements_enabled"] not in (0, 1, False, True)
            or row["room_match_announcements_enabled"] not in (0, 1, False, True)
            or not isinstance(row["revision_number"], int)
            or row["revision_number"] <= 0
        ):
            _target_error("persisted guild Discord settings semantics are invalid")

    audits = connection.execute(
        sa.text(
            "SELECT a.guild_id, a.action, a.actor_discord_user_id, "
            "a.idempotency_key, a.request_fingerprint, a.before_json, "
            "a.after_json, a.reason, s.guild_id AS settings_guild_id "
            f"FROM {AUDIT_TABLE} a JOIN {SETTINGS_TABLE} s "
            "ON s.id = a.guild_discord_settings_id ORDER BY a.id"
        )
    ).mappings()
    for row in audits:
        if (
            row["guild_id"] != row["settings_guild_id"]
            or row["action"] != "guild_discord_settings_update"
            or not _bounded_text(row["actor_discord_user_id"], 32)
            or not _bounded_text(row["idempotency_key"], 128)
            or not isinstance(row["request_fingerprint"], str)
            or len(row["request_fingerprint"]) != 64
            or not _bounded_text(row["reason"], 255)
            or not isinstance(row["before_json"], dict)
            or not isinstance(row["after_json"], dict)
        ):
            _target_error("persisted guild Discord settings audit semantics are invalid")


def _snowflake(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 32
        and value.isascii()
        and value.isdigit()
        and int(value) > 0
    )


def _optional_snowflake(value: object) -> bool:
    return value is None or _snowflake(value)


def _bounded_text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value.strip()) <= maximum and value.isprintable()


def _type_matches(
    value: sa.types.TypeEngine,
    *,
    family: str,
    dialect_name: str,
) -> bool:
    if family == "boolean":
        if dialect_name == "sqlite":
            return isinstance(value, (sa.Boolean, sa.Integer))
        return value.__class__.__name__.upper() == "TINYINT" and getattr(value, "display_width", None) == 1
    if family == "integer":
        return isinstance(value, sa.Integer)
    if family == "json":
        return isinstance(value, sa.JSON) or value.__class__.__name__.upper() in {"JSON", "LONGTEXT"}
    if family == "string":
        return isinstance(value, sa.String)
    if family == "datetime":
        return isinstance(value, sa.DateTime)
    return False


def _has_row(connection: sa.Connection, table_name: str) -> bool:
    return connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first() is not None


def _require_revision(connection: sa.Connection, expected: str) -> None:
    tables = set(sa.inspect(connection).get_table_names())
    if "alembic_version" not in tables:
        _unsupported("alembic_version table is missing")
    current = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    if current != expected:
        _unsupported(f"database is not at exact revision {expected}")


def _target_error(detail: str) -> None:
    raise RuntimeError(f"guild Discord settings target verification failed: {detail}")


def _unsupported(detail: str) -> None:
    raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: {detail}")
