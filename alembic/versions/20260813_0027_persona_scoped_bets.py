"""make Persona the immutable economic owner of Bets

Revision ID: 20260813_0027
Revises: 20260813_0026
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0027"
down_revision: str | Sequence[str] | None = "20260813_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNER_COLUMN = "persona_id"
OWNER_FOREIGN_KEY = "fk_bets_persona_id_personas"
OWNER_INDEX = "ix_bets_persona_id"
EXPECTED_SELECTION_COUNTS = {"win": 1, "quinella": 2, "trio": 3}


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    column = _owner_column(bind)
    if column is not None:
        _validate_owner_column_shape(column)
        _validate_owner_schema_drift(bind)

    owners = _preflight_rows(bind, has_owner_column=column is not None)
    if column is None:
        with op.batch_alter_table("bets") as batch:
            batch.add_column(sa.Column(OWNER_COLUMN, sa.String(length=36), nullable=True))

    _backfill_missing_owners(bind, owners)
    _assert_all_owners_present(bind)

    column = _owner_column(bind)
    if column is None:
        raise RuntimeError("cannot upgrade Persona-scoped Bets: bets.persona_id was not created")
    if column["nullable"]:
        with op.batch_alter_table("bets") as batch:
            batch.alter_column(
                OWNER_COLUMN,
                existing_type=sa.String(length=36),
                nullable=False,
            )

    _ensure_canonical_index_and_foreign_key(bind)
    _validate_completed_schema(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    if bind.execute(sa.text("SELECT 1 FROM bets LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade Persona-scoped Bets while Bet data exists")

    column = _owner_column(bind)
    if column is None:
        return
    _validate_completed_schema(bind)
    with op.batch_alter_table("bets") as batch:
        batch.drop_constraint(OWNER_FOREIGN_KEY, type_="foreignkey")
        batch.drop_index(OWNER_INDEX)
        batch.drop_column(OWNER_COLUMN)


def _require_tables(bind: sa.Connection) -> None:
    required = {"bets", "game_accounts", "personas"}
    missing = required - set(sa.inspect(bind).get_table_names())
    if missing:
        raise RuntimeError("cannot upgrade Persona-scoped Bets: missing table(s): " + ", ".join(sorted(missing)))


def _owner_column(bind: sa.Connection) -> dict[str, object] | None:
    return next(
        (column for column in sa.inspect(bind).get_columns("bets") if column["name"] == OWNER_COLUMN),
        None,
    )


def _validate_owner_column_shape(column: dict[str, object]) -> None:
    column_type = column["type"]
    if not isinstance(column_type, sa.String) or column_type.length != 36:
        raise RuntimeError("cannot upgrade Persona-scoped Bets: conflicting bets.persona_id")


def _preflight_rows(bind: sa.Connection, *, has_owner_column: bool) -> dict[int, str]:
    owner_expression = "bet.persona_id AS stored_persona_id," if has_owner_column else "NULL AS stored_persona_id,"
    rows = bind.execute(
        sa.text(
            "SELECT bet.id, bet.race_id, bet.game_account_id, bet.betting_mode, bet.bet_type, "
            f"bet.numbers, bet.status, {owner_expression} game.persona_id AS linked_persona_id, "
            "stored.id AS stored_persona_exists, linked.id AS linked_persona_exists "
            "FROM bets AS bet "
            "LEFT JOIN game_accounts AS game ON game.id = bet.game_account_id "
            + (
                "LEFT JOIN personas AS stored ON stored.id = bet.persona_id "
                if has_owner_column
                else "LEFT JOIN personas AS stored ON 1 = 0 "
            )
            + "LEFT JOIN personas AS linked ON linked.id = game.persona_id ORDER BY bet.id"
        )
    ).mappings()

    owners: dict[int, str] = {}
    active_keys: dict[tuple[object, ...], int] = {}
    for row in rows:
        row_id = _required_int(row["id"], field="id", row_id=None)
        _required_int(row["race_id"], field="race_id", row_id=row_id)
        _required_int(row["game_account_id"], field="game_account_id", row_id=row_id)
        mode = _required_text(row["betting_mode"], field="betting_mode", row_id=row_id)
        bet_type = _required_text(row["bet_type"], field="bet_type", row_id=row_id)
        status = _required_text(row["status"], field="status", row_id=row_id)
        numbers = _canonical_numbers(row["numbers"], bet_type=bet_type, row_id=row_id)

        stored_owner = row["stored_persona_id"]
        if stored_owner is not None:
            if row["stored_persona_exists"] is None:
                raise RuntimeError(f"cannot upgrade Persona-scoped Bets: Bet id={row_id} has an unknown Persona owner")
            owner = _required_text(stored_owner, field="persona_id", row_id=row_id)
        else:
            if row["linked_persona_id"] is None or row["linked_persona_exists"] is None:
                raise RuntimeError(f"cannot upgrade Persona-scoped Bets: Bet id={row_id} has no Persona owner")
            owner = _required_text(row["linked_persona_id"], field="persona_id", row_id=row_id)
        owners[row_id] = owner

        if status == "active":
            key = (owner, int(row["race_id"]), mode, bet_type, numbers)
            duplicate_id = active_keys.setdefault(key, row_id)
            if duplicate_id != row_id:
                raise RuntimeError(
                    f"cannot upgrade Persona-scoped Bets: active duplicate Bets id={duplicate_id} and id={row_id}"
                )
    return owners


def _required_int(value: object, *, field: str, row_id: int | None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        location = "" if row_id is None else f" at id={row_id}"
        raise RuntimeError(f"cannot upgrade Persona-scoped Bets: malformed Bet {field}{location}")
    return value


def _required_text(value: object, *, field: str, row_id: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"cannot upgrade Persona-scoped Bets: malformed Bet {field} at id={row_id}")
    return value


def _canonical_numbers(value: object, *, bet_type: str, row_id: int) -> tuple[int, ...]:
    decoded = value
    if isinstance(decoded, bytes):
        try:
            decoded = decoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"cannot upgrade Persona-scoped Bets: malformed Bet numbers at id={row_id}") from exc
    if isinstance(decoded, str):
        try:
            decoded = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"cannot upgrade Persona-scoped Bets: malformed Bet numbers at id={row_id}") from exc
    expected_count = EXPECTED_SELECTION_COUNTS.get(bet_type)
    if (
        expected_count is None
        or not isinstance(decoded, list)
        or len(decoded) != expected_count
        or any(not isinstance(number, int) or isinstance(number, bool) or number <= 0 for number in decoded)
        or len(set(decoded)) != len(decoded)
    ):
        raise RuntimeError(f"cannot upgrade Persona-scoped Bets: malformed Bet numbers at id={row_id}")
    if bet_type in {"quinella", "trio"}:
        decoded = sorted(decoded)
    return tuple(decoded)


def _backfill_missing_owners(bind: sa.Connection, owners: dict[int, str]) -> None:
    for row_id, persona_id in owners.items():
        bind.execute(
            sa.text("UPDATE bets SET persona_id = :persona_id WHERE id = :id AND persona_id IS NULL"),
            {"id": row_id, "persona_id": persona_id},
        )


def _assert_all_owners_present(bind: sa.Connection) -> None:
    missing = bind.execute(sa.text("SELECT id FROM bets WHERE persona_id IS NULL LIMIT 1")).scalar()
    if missing is not None:
        raise RuntimeError(f"cannot upgrade Persona-scoped Bets: Bet id={missing} has no Persona owner")


def _owner_foreign_keys(bind: sa.Connection) -> list[dict[str, object]]:
    return [
        foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys("bets")
        if OWNER_COLUMN in tuple(foreign_key.get("constrained_columns") or ())
    ]


def _owner_indexes(bind: sa.Connection) -> list[dict[str, object]]:
    return [
        index
        for index in sa.inspect(bind).get_indexes("bets")
        if OWNER_COLUMN in tuple(index.get("column_names") or ())
    ]


def _is_canonical_foreign_key(foreign_key: dict[str, object]) -> bool:
    options = foreign_key.get("options")
    return (
        foreign_key.get("name") == OWNER_FOREIGN_KEY
        and tuple(foreign_key.get("constrained_columns") or ()) == (OWNER_COLUMN,)
        and foreign_key.get("referred_schema") is None
        and foreign_key.get("referred_table") == "personas"
        and tuple(foreign_key.get("referred_columns") or ()) == ("id",)
        and (not isinstance(options, dict) or not any(value is not None for value in options.values()))
    )


def _is_canonical_index(index: dict[str, object]) -> bool:
    dialect_options = index.get("dialect_options")
    return (
        index.get("name") == OWNER_INDEX
        and tuple(index.get("column_names") or ()) == (OWNER_COLUMN,)
        and not bool(index.get("unique"))
        and (not isinstance(dialect_options, dict) or not any(value is not None for value in dialect_options.values()))
    )


def _validate_owner_schema_drift(bind: sa.Connection) -> None:
    foreign_keys = _owner_foreign_keys(bind)
    if foreign_keys and not (len(foreign_keys) == 1 and _is_canonical_foreign_key(foreign_keys[0])):
        raise RuntimeError("cannot upgrade Persona-scoped Bets: conflicting foreign key on bets.persona_id")

    indexes = _owner_indexes(bind)
    canonical = [index for index in indexes if _is_canonical_index(index)]
    if indexes and not (len(indexes) == 1 and len(canonical) == 1):
        raise RuntimeError("cannot upgrade Persona-scoped Bets: conflicting index on bets.persona_id")


def _ensure_canonical_index_and_foreign_key(bind: sa.Connection) -> None:
    _validate_owner_schema_drift(bind)
    indexes = _owner_indexes(bind)
    if not any(_is_canonical_index(index) for index in indexes):
        with op.batch_alter_table("bets") as batch:
            batch.create_index(OWNER_INDEX, [OWNER_COLUMN], unique=False)

    foreign_keys = _owner_foreign_keys(bind)
    if not foreign_keys:
        with op.batch_alter_table("bets") as batch:
            batch.create_foreign_key(
                OWNER_FOREIGN_KEY,
                "personas",
                [OWNER_COLUMN],
                ["id"],
            )


def _validate_completed_schema(bind: sa.Connection) -> None:
    column = _owner_column(bind)
    if column is None:
        raise RuntimeError("cannot upgrade Persona-scoped Bets: missing bets.persona_id")
    _validate_owner_column_shape(column)
    if column["nullable"]:
        raise RuntimeError("cannot upgrade Persona-scoped Bets: bets.persona_id must be NOT NULL")
    foreign_keys = _owner_foreign_keys(bind)
    if not (len(foreign_keys) == 1 and _is_canonical_foreign_key(foreign_keys[0])):
        raise RuntimeError("cannot upgrade Persona-scoped Bets: noncanonical foreign key on bets.persona_id")
    indexes = _owner_indexes(bind)
    if not (len(indexes) == 1 and _is_canonical_index(indexes[0])):
        raise RuntimeError("cannot upgrade Persona-scoped Bets: noncanonical index on bets.persona_id")
