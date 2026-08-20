"""add Persona identity foundation and backfill direct account pairs

Revision ID: 20260803_0022
Revises: 20260802_0021
"""

from collections.abc import Sequence
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0022"
down_revision: str | Sequence[str] | None = "20260802_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
PERSONA_TABLE = "personas"
PERSONA_ID_LENGTH = 36
PERSONA_NAMESPACE = NAMESPACE_URL
PERSONA_CHILD_FKS = {
    "discord_accounts": "fk_discord_accounts_persona_id_personas",
    "game_accounts": "fk_game_accounts_persona_id_personas",
}
PERSONA_CHILD_INDEXES = {
    "discord_accounts": "ix_discord_accounts_persona_id",
    "game_accounts": "ix_game_accounts_persona_id",
}
PERSONA_MAIN_FK = "fk_personas_main_game_account_id_game_accounts"
PERSONA_STATUS = {
    "confirmed_identity": "active",
    "pending_identity": "inactive",
    "identity_conflict": "inactive",
    "registration_cancelled": "archived",
}
PERSONA_DISPLAY_SOURCES = {"discord", "main_game_account", "manual"}
PERSONA_STATUSES = {"active", "inactive", "suspended", "archived"}
PERSONA_COLUMNS = {
    "id",
    "display_name",
    "display_name_source",
    "status",
    "main_game_account_id",
    "created_at",
    "updated_at",
}
PERSONA_CHECKS = {
    "ck_personas_nonempty_display_name",
    "ck_personas_display_name_source",
    "ck_personas_status",
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    records = _load_and_validate_source_records(bind, tables)
    _validate_existing_target_shape(inspector, tables)

    if PERSONA_TABLE not in tables:
        _create_persona_table()
    _ensure_persona_child_columns()
    _ensure_persona_child_schema()
    _backfill_personas(bind, records)
    _ensure_main_persona_foreign_key()
    _validate_final_state(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if PERSONA_TABLE not in tables:
        if any("persona_id" in _column_names(inspector, table) for table in PERSONA_CHILD_FKS):
            raise RuntimeError("cannot downgrade Persona foundation: target schema is incomplete")
        return

    if bind.execute(sa.text("SELECT 1 FROM personas LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade Persona foundation after Persona data exists")
    for table in PERSONA_CHILD_FKS:
        if bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE persona_id IS NOT NULL LIMIT 1")).first() is not None:
            raise RuntimeError("cannot downgrade Persona foundation while account links exist")

    _drop_main_persona_foreign_key()
    for table in PERSONA_CHILD_FKS:
        inspector = sa.inspect(bind)
        with op.batch_alter_table(table) as batch:
            if _has_index(inspector, table, PERSONA_CHILD_INDEXES[table]):
                batch.drop_index(PERSONA_CHILD_INDEXES[table])
            if _has_foreign_key(inspector, table, "persona_id", PERSONA_TABLE, "id"):
                batch.drop_constraint(PERSONA_CHILD_FKS[table], type_="foreignkey")
            if "persona_id" in _column_names(inspector, table):
                batch.drop_column("persona_id")
    op.drop_table(PERSONA_TABLE)


def _create_persona_table() -> None:
    op.create_table(
        PERSONA_TABLE,
        sa.Column("id", sa.String(length=PERSONA_ID_LENGTH), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("display_name_source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("main_game_account_id", BIGINT, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("main_game_account_id", name="uq_personas_main_game_account_id"),
        sa.CheckConstraint("display_name <> ''", name="nonempty_display_name"),
        sa.CheckConstraint(
            "display_name_source IN ('discord', 'main_game_account', 'manual')",
            name="display_name_source",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'suspended', 'archived')",
            name="status",
        ),
    )


def _load_and_validate_source_records(bind: sa.Connection, tables: set[str]) -> list[dict[str, object]]:
    required_tables = {"discord_accounts", "game_accounts"}
    if not required_tables <= tables:
        missing = ", ".join(sorted(required_tables - tables))
        raise RuntimeError(f"cannot upgrade Persona foundation: missing source table(s): {missing}")

    inspector = sa.inspect(bind)
    required_columns = {
        "discord_accounts": {"id", "discord_nickname"},
        "game_accounts": {"id", "discord_account_id", "ingame_name", "identity_status"},
    }
    for table, columns in required_columns.items():
        missing = columns - _column_names(inspector, table)
        if missing:
            raise RuntimeError(
                f"cannot upgrade Persona foundation: missing source column(s) in {table}: {', '.join(sorted(missing))}"
            )

    duplicate = bind.execute(
        sa.text(
            "SELECT discord_account_id FROM game_accounts "
            "WHERE discord_account_id IS NOT NULL "
            "GROUP BY discord_account_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError("cannot upgrade Persona foundation: a Discord account has multiple game accounts")

    missing_owner = bind.execute(
        sa.text(
            "SELECT ga.id FROM game_accounts AS ga "
            "LEFT JOIN discord_accounts AS da ON da.id = ga.discord_account_id "
            "WHERE ga.discord_account_id IS NULL OR da.id IS NULL LIMIT 1"
        )
    ).first()
    if missing_owner is not None:
        raise RuntimeError("cannot upgrade Persona foundation: a game account has no valid Discord owner")

    records: list[dict[str, object]] = []
    pair_rows = bind.execute(
        sa.text(
            "SELECT ga.id AS game_account_id, ga.discord_account_id, ga.ingame_name, ga.identity_status, "
            "da.discord_nickname "
            "FROM game_accounts AS ga "
            "JOIN discord_accounts AS da ON da.id = ga.discord_account_id "
            "ORDER BY ga.id"
        )
    ).mappings()
    paired_discord_ids: set[int] = set()
    for row in pair_rows:
        game_account_id = _positive_int(row["game_account_id"], "game account id")
        discord_account_id = _positive_int(row["discord_account_id"], "Discord account id")
        identity_status = row["identity_status"]
        if identity_status not in PERSONA_STATUS:
            raise RuntimeError(
                "cannot upgrade Persona foundation: unsupported game account identity status "
                f"for game_account_id={game_account_id}"
            )
        display_name, display_name_source = _select_display_name(
            row["ingame_name"], row["discord_nickname"], game_account_id
        )
        records.append(
            {
                "persona_id": _deterministic_persona_id("game-account", game_account_id),
                "game_account_id": game_account_id,
                "discord_account_id": discord_account_id,
                "display_name": display_name,
                "display_name_source": display_name_source,
                "status": PERSONA_STATUS[identity_status],
            }
        )
        paired_discord_ids.add(discord_account_id)

    orphan_rows = bind.execute(
        sa.text(
            "SELECT da.id AS discord_account_id, da.discord_nickname "
            "FROM discord_accounts AS da "
            "LEFT JOIN game_accounts AS ga ON ga.discord_account_id = da.id "
            "WHERE ga.id IS NULL ORDER BY da.id"
        )
    ).mappings()
    for row in orphan_rows:
        discord_account_id = _positive_int(row["discord_account_id"], "Discord account id")
        if discord_account_id in paired_discord_ids:
            raise RuntimeError("cannot upgrade Persona foundation: duplicate Discord account backfill source")
        display_name = _validated_display_name(row["discord_nickname"], f"Discord account {discord_account_id}")
        records.append(
            {
                "persona_id": _deterministic_persona_id("discord-account", discord_account_id),
                "game_account_id": None,
                "discord_account_id": discord_account_id,
                "display_name": display_name,
                "display_name_source": "discord",
                "status": "inactive",
            }
        )
    return records


def _select_display_name(
    ingame_name: object,
    discord_nickname: object,
    game_account_id: int,
) -> tuple[str, str]:
    if isinstance(ingame_name, str) and ingame_name.strip():
        return _validated_display_name(ingame_name, f"game account {game_account_id}"), "main_game_account"
    return _validated_display_name(discord_nickname, f"Discord account for game account {game_account_id}"), "discord"


def _validated_display_name(value: object, source: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"cannot upgrade Persona foundation: display name is not text for {source}")
    normalized = value.strip()
    if not normalized:
        raise RuntimeError(f"cannot upgrade Persona foundation: display name is blank for {source}")
    if len(normalized) > 100:
        raise RuntimeError(f"cannot upgrade Persona foundation: display name is too long for {source}")
    return normalized


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError(f"cannot upgrade Persona foundation: invalid {label}")
    return value


def _deterministic_persona_id(kind: str, source_id: int) -> str:
    return str(uuid5(PERSONA_NAMESPACE, f"umacircle-bot/persona/{kind}/{source_id}"))


def _validate_existing_target_shape(inspector: sa.Inspector, tables: set[str]) -> None:
    persona_columns = any("persona_id" in _column_names(inspector, table) for table in PERSONA_CHILD_FKS)
    if PERSONA_TABLE not in tables:
        if persona_columns:
            raise RuntimeError(
                "cannot upgrade Persona foundation: account Persona columns exist without personas table"
            )
        return

    columns = {column["name"]: column for column in inspector.get_columns(PERSONA_TABLE)}
    if set(columns) != PERSONA_COLUMNS:
        raise RuntimeError("cannot upgrade Persona foundation: existing personas table has an unsupported shape")
    _require_string_column(columns, "id", PERSONA_ID_LENGTH, nullable=False)
    _require_string_column(columns, "display_name", 100, nullable=False)
    _require_string_column(columns, "display_name_source", 32, nullable=False)
    _require_string_column(columns, "status", 16, nullable=False)
    if columns["main_game_account_id"]["nullable"] is not True:
        raise RuntimeError("cannot upgrade Persona foundation: main_game_account_id must be nullable")
    if not _has_unique(inspector, PERSONA_TABLE, ("main_game_account_id",)):
        raise RuntimeError("cannot upgrade Persona foundation: main_game_account_id must be unique")
    check_names = {constraint.get("name") for constraint in inspector.get_check_constraints(PERSONA_TABLE)}
    if not PERSONA_CHECKS <= check_names:
        raise RuntimeError("cannot upgrade Persona foundation: personas checks are incomplete")

    for table in PERSONA_CHILD_FKS:
        if "persona_id" not in _column_names(inspector, table):
            continue
        child_column = next(column for column in inspector.get_columns(table) if column["name"] == "persona_id")
        _require_string_column(
            {"persona_id": child_column},
            "persona_id",
            PERSONA_ID_LENGTH,
            nullable=True,
        )


def _require_string_column(
    columns: dict[str, dict[str, object]],
    name: str,
    length: int,
    *,
    nullable: bool,
) -> None:
    column = columns[name]
    column_type = column["type"]
    if not isinstance(column_type, sa.String) or column_type.length != length or column["nullable"] is not nullable:
        raise RuntimeError(f"cannot upgrade Persona foundation: {name} has an unsupported physical shape")


def _ensure_persona_child_columns() -> None:
    for table in PERSONA_CHILD_FKS:
        inspector = sa.inspect(op.get_bind())
        if "persona_id" in _column_names(inspector, table):
            continue
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("persona_id", sa.String(length=PERSONA_ID_LENGTH), nullable=True))


def _ensure_persona_child_schema() -> None:
    for table, foreign_key_name in PERSONA_CHILD_FKS.items():
        inspector = sa.inspect(op.get_bind())
        needs_index = not _has_index(inspector, table, PERSONA_CHILD_INDEXES[table])
        needs_foreign_key = not _has_foreign_key(inspector, table, "persona_id", PERSONA_TABLE, "id")
        if not needs_index and not needs_foreign_key:
            continue
        with op.batch_alter_table(table) as batch:
            if needs_index:
                batch.create_index(PERSONA_CHILD_INDEXES[table], ["persona_id"], unique=False)
            if needs_foreign_key:
                batch.create_foreign_key(foreign_key_name, PERSONA_TABLE, ["persona_id"], ["id"])


def _backfill_personas(bind: sa.Connection, records: list[dict[str, object]]) -> None:
    expected_ids = {str(record["persona_id"]) for record in records}
    existing_rows = {
        str(row["id"]): row
        for row in bind.execute(
            sa.text(
                "SELECT id, display_name, display_name_source, status, main_game_account_id FROM personas ORDER BY id"
            )
        ).mappings()
    }
    unexpected_ids = set(existing_rows) - expected_ids
    if unexpected_ids:
        raise RuntimeError("cannot upgrade Persona foundation: existing Persona rows have no deterministic source")

    for record in records:
        persona_id = str(record["persona_id"])
        game_account_id = record["game_account_id"]
        discord_account_id = _positive_int(record["discord_account_id"], "Discord account id")
        expected_main = game_account_id if isinstance(game_account_id, int) else None
        existing = existing_rows.get(persona_id)
        if existing is None:
            bind.execute(
                sa.text(
                    "INSERT INTO personas "
                    "(id, display_name, display_name_source, status, main_game_account_id) "
                    "VALUES (:id, :display_name, :display_name_source, :status, NULL)"
                ),
                {
                    "id": persona_id,
                    "display_name": record["display_name"],
                    "display_name_source": record["display_name_source"],
                    "status": record["status"],
                },
            )
        else:
            if any(existing[column] != record[column] for column in ("display_name", "display_name_source", "status")):
                raise RuntimeError(
                    f"cannot upgrade Persona foundation: Persona {persona_id} conflicts with source data"
                )
            current_main = existing["main_game_account_id"]
            if current_main is not None and current_main != expected_main:
                raise RuntimeError(f"cannot upgrade Persona foundation: Persona {persona_id} has a wrong main account")

        _ensure_child_owner(bind, "discord_accounts", discord_account_id, persona_id)
        if expected_main is not None:
            _ensure_child_owner(bind, "game_accounts", expected_main, persona_id)
            bind.execute(
                sa.text(
                    "UPDATE personas SET main_game_account_id = :game_account_id "
                    "WHERE id = :persona_id AND main_game_account_id IS NULL"
                ),
                {"game_account_id": expected_main, "persona_id": persona_id},
            )


def _ensure_child_owner(bind: sa.Connection, table: str, row_id: int, persona_id: str) -> None:
    row = bind.execute(
        sa.text(f"SELECT persona_id FROM {table} WHERE id = :row_id"),
        {"row_id": row_id},
    ).first()
    if row is None:
        raise RuntimeError(f"cannot upgrade Persona foundation: missing {table} row {row_id}")
    current_persona_id = row[0]
    if current_persona_id is not None and str(current_persona_id) != persona_id:
        raise RuntimeError(f"cannot upgrade Persona foundation: {table} row {row_id} has a conflicting Persona")
    if current_persona_id is None:
        bind.execute(
            sa.text(f"UPDATE {table} SET persona_id = :persona_id WHERE id = :row_id"),
            {"persona_id": persona_id, "row_id": row_id},
        )


def _ensure_main_persona_foreign_key() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_foreign_key(inspector, PERSONA_TABLE, "main_game_account_id", "game_accounts", "id"):
        return
    with op.batch_alter_table(PERSONA_TABLE) as batch:
        batch.create_foreign_key(PERSONA_MAIN_FK, "game_accounts", ["main_game_account_id"], ["id"])


def _validate_final_state(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    _validate_existing_target_shape(inspector, set(inspector.get_table_names()))
    for table in PERSONA_CHILD_FKS:
        if not _has_index(inspector, table, PERSONA_CHILD_INDEXES[table]):
            raise RuntimeError(f"cannot upgrade Persona foundation: missing {table} Persona index")
        if not _has_foreign_key(inspector, table, "persona_id", PERSONA_TABLE, "id"):
            raise RuntimeError(f"cannot upgrade Persona foundation: missing {table} Persona foreign key")
    if not _has_foreign_key(inspector, PERSONA_TABLE, "main_game_account_id", "game_accounts", "id"):
        raise RuntimeError("cannot upgrade Persona foundation: missing main Persona foreign key")

    if bind.execute(sa.text("SELECT 1 FROM game_accounts WHERE persona_id IS NULL LIMIT 1")).first() is not None:
        raise RuntimeError("cannot upgrade Persona foundation: a game account has no Persona")
    if bind.execute(sa.text("SELECT 1 FROM discord_accounts WHERE persona_id IS NULL LIMIT 1")).first() is not None:
        raise RuntimeError("cannot upgrade Persona foundation: a Discord account has no Persona")
    mismatch = bind.execute(
        sa.text(
            "SELECT ga.id FROM game_accounts AS ga "
            "JOIN discord_accounts AS da ON da.id = ga.discord_account_id "
            "WHERE ga.persona_id <> da.persona_id LIMIT 1"
        )
    ).first()
    if mismatch is not None:
        raise RuntimeError("cannot upgrade Persona foundation: direct account pair has different Personas")
    missing_main = bind.execute(
        sa.text(
            "SELECT ga.id FROM game_accounts AS ga "
            "JOIN personas AS p ON p.id = ga.persona_id "
            "WHERE p.main_game_account_id IS NULL OR p.main_game_account_id <> ga.id LIMIT 1"
        )
    ).first()
    if missing_main is not None:
        raise RuntimeError("cannot upgrade Persona foundation: a game account is not its Persona main account")
    wrong_main = bind.execute(
        sa.text(
            "SELECT p.id FROM personas AS p "
            "JOIN game_accounts AS ga ON ga.id = p.main_game_account_id "
            "WHERE ga.persona_id <> p.id LIMIT 1"
        )
    ).first()
    if wrong_main is not None:
        raise RuntimeError("cannot upgrade Persona foundation: a main game account belongs to another Persona")


def _drop_main_persona_foreign_key() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _has_foreign_key(inspector, PERSONA_TABLE, "main_game_account_id", "game_accounts", "id"):
        return
    with op.batch_alter_table(PERSONA_TABLE) as batch:
        batch.drop_constraint(PERSONA_MAIN_FK, type_="foreignkey")


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {str(column["name"]) for column in inspector.get_columns(table)}


def _has_foreign_key(
    inspector: sa.Inspector,
    table: str,
    column: str,
    referred_table: str,
    referred_column: str,
) -> bool:
    return any(
        tuple(foreign_key.get("constrained_columns", ())) == (column,)
        and foreign_key.get("referred_table") == referred_table
        and tuple(foreign_key.get("referred_columns", ())) == (referred_column,)
        for foreign_key in inspector.get_foreign_keys(table)
    )


def _has_unique(inspector: sa.Inspector, table: str, columns: tuple[str, ...]) -> bool:
    if any(tuple(item.get("column_names", ())) == columns for item in inspector.get_unique_constraints(table)):
        return True
    return any(
        bool(item.get("unique")) and tuple(item.get("column_names", ())) == columns
        for item in inspector.get_indexes(table)
    )


def _has_index(inspector: sa.Inspector, table: str, name: str) -> bool:
    return any(item.get("name") == name for item in inspector.get_indexes(table))
