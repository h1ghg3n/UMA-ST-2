"""move Room Point wallets to Persona and scale money by ten

Revision ID: 20260806_0025
Revises: 20260805_0024
"""

import json
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_0025"
down_revision: str | Sequence[str] | None = "20260805_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
SCALE = 10
MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "room_point_scale_state" in tables:
        _assert_completed_state(bind)
        return
    _preflight(bind, inspector)
    _widen_monetary_columns()
    _add_persona_columns()
    _backfill_persona_owners(bind)
    _scale_monetary_data(bind)
    _scale_win5_monetary_audits(bind)
    _consolidate_wallets(bind)
    _finalize_wallet_schema()
    _reconcile_wallets(bind)
    op.create_table(
        "room_point_scale_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("scale_version", sa.Integer(), nullable=False),
        sa.Column("migrated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_room_point_scale_state_singleton"),
        sa.CheckConstraint("scale_version = 10", name="ck_room_point_scale_state_version"),
    )
    bind.execute(sa.text("INSERT INTO room_point_scale_state (id, scale_version) VALUES (1, :scale)"), {"scale": SCALE})


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM room_point_accounts LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade Persona wallet scale after monetary data exists")
    if bind.execute(sa.text("SELECT 1 FROM room_point_transactions LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade Persona wallet scale after monetary data exists")
    if bind.execute(sa.text("SELECT 1 FROM bets LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade Persona wallet scale after monetary data exists")
    if bind.execute(sa.text("SELECT 1 FROM bet_judgements LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade Persona wallet scale after monetary data exists")
    with op.batch_alter_table("room_point_transactions") as batch:
        batch.drop_constraint("fk_room_point_transactions_persona_id_personas", type_="foreignkey")
        batch.drop_index("ix_room_point_transactions_persona_id")
        batch.drop_column("persona_id")
        batch.alter_column("amount", existing_type=BIGINT, type_=sa.Integer())
    with op.batch_alter_table("room_point_accounts") as batch:
        batch.drop_constraint("fk_room_point_accounts_persona_id_personas", type_="foreignkey")
        batch.drop_constraint("uq_room_point_accounts_persona_id", type_="unique")
        batch.drop_column("persona_id")
        batch.add_column(sa.Column("game_account_id", BIGINT, nullable=False))
        batch.create_foreign_key(
            "fk_room_point_accounts_game_account_id_game_accounts", "game_accounts", ["game_account_id"], ["id"]
        )
        batch.create_unique_constraint("uq_room_point_accounts_game_account_id", ["game_account_id"])
        batch.alter_column("balance", existing_type=BIGINT, type_=sa.Integer())
    for table, column in (
        ("bets", "amount"),
        ("bet_judgements", "stake_amount"),
        ("bet_judgements", "payout_amount"),
        ("bet_judgements", "point_delta"),
    ):
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=BIGINT, type_=sa.Integer(), nullable=False)
    op.drop_table("room_point_scale_state")


def _preflight(bind: sa.Connection, inspector: sa.Inspector) -> None:
    required = {"personas", "game_accounts", "room_point_accounts", "room_point_transactions", "bets", "bet_judgements"}
    missing = required - set(inspector.get_table_names())
    if missing:
        raise RuntimeError(f"cannot upgrade Persona wallet scale: missing table(s): {', '.join(sorted(missing))}")
    for table, column in (("room_point_accounts", "game_account_id"), ("room_point_transactions", "game_account_id")):
        missing_owner = bind.execute(
            sa.text(
                f"SELECT item.id FROM {table} AS item LEFT JOIN game_accounts AS game ON game.id = item.{column} "
                "WHERE game.id IS NULL OR game.persona_id IS NULL LIMIT 1"
            )
        ).first()
        if missing_owner is not None:
            raise RuntimeError(f"cannot upgrade Persona wallet scale: {table} row has no Persona owner")
    for table, column in (
        ("room_point_accounts", "balance"),
        ("room_point_transactions", "amount"),
        ("bets", "amount"),
        ("bet_judgements", "stake_amount"),
        ("bet_judgements", "payout_amount"),
        ("bet_judgements", "point_delta"),
    ):
        value = bind.execute(sa.text(f"SELECT MAX(ABS({column})) FROM {table}")).scalar()
        if value is not None and int(value) > MAX_SIGNED_BIGINT // SCALE:
            raise RuntimeError(f"cannot upgrade Persona wallet scale: {table}.{column} would overflow BIGINT")


def _widen_monetary_columns() -> None:
    for table, column in (
        ("room_point_accounts", "balance"),
        ("room_point_transactions", "amount"),
        ("bets", "amount"),
        ("bet_judgements", "stake_amount"),
        ("bet_judgements", "payout_amount"),
        ("bet_judgements", "point_delta"),
    ):
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=sa.Integer(), type_=BIGINT, nullable=False)


def _add_persona_columns() -> None:
    with op.batch_alter_table("room_point_accounts") as batch:
        batch.add_column(sa.Column("persona_id", sa.String(length=36), nullable=True))
    with op.batch_alter_table("room_point_transactions") as batch:
        batch.add_column(sa.Column("persona_id", sa.String(length=36), nullable=True))


def _backfill_persona_owners(bind: sa.Connection) -> None:
    for table in ("room_point_accounts", "room_point_transactions"):
        rows = bind.execute(
            sa.text(
                f"SELECT item.id, game.persona_id FROM {table} AS item "
                "JOIN game_accounts AS game ON game.id = item.game_account_id ORDER BY item.id"
            )
        ).mappings()
        for row in rows:
            bind.execute(sa.text(f"UPDATE {table} SET persona_id = :persona_id WHERE id = :id"), dict(row))


def _scale_monetary_data(bind: sa.Connection) -> None:
    for table, column in (
        ("room_point_accounts", "balance"),
        ("room_point_transactions", "amount"),
        ("bets", "amount"),
        ("bet_judgements", "stake_amount"),
        ("bet_judgements", "payout_amount"),
        ("bet_judgements", "point_delta"),
    ):
        bind.execute(sa.text(f"UPDATE {table} SET {column} = {column} * :scale"), {"scale": SCALE})


def _scale_win5_monetary_audits(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    targets = (
        ("win5_judgements", "audit_json", _scale_room_point_delta),
        ("win5_operation_audits", "after_json", _scale_room_point_rewards),
    )
    for table, column, scaler in targets:
        if table not in set(inspector.get_table_names()):
            continue
        if column not in {item["name"] for item in inspector.get_columns(table)}:
            continue
        rows = bind.execute(sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")).mappings()
        for row in rows:
            decoded = _decode_json_payload(row[column], table=table, column=column, row_id=int(row["id"]))
            changed = scaler(decoded, table=table, column=column, row_id=int(row["id"]))
            if changed:
                bind.execute(
                    sa.text(f"UPDATE {table} SET {column} = :payload WHERE id = :id"),
                    {"id": row["id"], "payload": json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))},
                )


def _decode_json_payload(value: object, *, table: str, column: str, row_id: int) -> dict[str, object]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"cannot upgrade Persona wallet scale: invalid {table}.{column} JSON at id={row_id}"
        ) from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"cannot upgrade Persona wallet scale: invalid {table}.{column} JSON at id={row_id}")
    return decoded


def _scale_room_point_delta(payload: dict[str, object], *, table: str, column: str, row_id: int) -> bool:
    value = payload.get("room_point_delta")
    if value is None:
        return False
    payload["room_point_delta"] = _scaled_json_amount(value, table=table, column=column, row_id=row_id)
    return True


def _scale_room_point_rewards(payload: dict[str, object], *, table: str, column: str, row_id: int) -> bool:
    rewards = payload.get("room_point_rewards")
    if rewards is None:
        return False
    if not isinstance(rewards, list):
        raise RuntimeError(f"cannot upgrade Persona wallet scale: invalid {table}.{column} rewards at id={row_id}")
    for reward in rewards:
        if not isinstance(reward, dict) or "amount" not in reward:
            raise RuntimeError(f"cannot upgrade Persona wallet scale: invalid {table}.{column} rewards at id={row_id}")
        reward["amount"] = _scaled_json_amount(reward["amount"], table=table, column=column, row_id=row_id)
    return True


def _scaled_json_amount(value: object, *, table: str, column: str, row_id: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or abs(value) > MAX_SIGNED_BIGINT // SCALE:
        raise RuntimeError(
            f"cannot upgrade Persona wallet scale: {table}.{column} would overflow BIGINT at id={row_id}"
        )
    return value * SCALE


def _consolidate_wallets(bind: sa.Connection) -> None:
    grouped: dict[str, list[tuple[int, int]]] = defaultdict(list)
    rows = bind.execute(sa.text("SELECT id, persona_id, balance FROM room_point_accounts ORDER BY id")).mappings()
    for row in rows:
        grouped[str(row["persona_id"])].append((int(row["id"]), int(row["balance"])))
    for _persona_id, wallets in grouped.items():
        retained_id, _ = wallets[0]
        bind.execute(
            sa.text("UPDATE room_point_accounts SET balance = :balance WHERE id = :id"),
            {"id": retained_id, "balance": sum(balance for _, balance in wallets)},
        )
        for wallet_id, _ in wallets[1:]:
            bind.execute(sa.text("DELETE FROM room_point_accounts WHERE id = :id"), {"id": wallet_id})


def _finalize_wallet_schema() -> None:
    inspector = sa.inspect(op.get_bind())
    with op.batch_alter_table("room_point_accounts") as batch:
        batch.alter_column("persona_id", existing_type=sa.String(length=36), nullable=False)
        batch.create_foreign_key("fk_room_point_accounts_persona_id_personas", "personas", ["persona_id"], ["id"])
        batch.create_unique_constraint("uq_room_point_accounts_persona_id", ["persona_id"])
        for foreign_key in inspector.get_foreign_keys("room_point_accounts"):
            if tuple(foreign_key.get("constrained_columns") or ()) == ("game_account_id",) and foreign_key.get("name"):
                batch.drop_constraint(str(foreign_key["name"]), type_="foreignkey")
        for constraint in inspector.get_unique_constraints("room_point_accounts"):
            if tuple(constraint.get("column_names") or ()) == ("game_account_id",) and constraint.get("name"):
                batch.drop_constraint(str(constraint["name"]), type_="unique")
        batch.drop_column("game_account_id")
    with op.batch_alter_table("room_point_transactions") as batch:
        batch.alter_column("persona_id", existing_type=sa.String(length=36), nullable=False)
        batch.create_foreign_key("fk_room_point_transactions_persona_id_personas", "personas", ["persona_id"], ["id"])
        batch.create_index("ix_room_point_transactions_persona_id", ["persona_id"], unique=False)


def _reconcile_wallets(bind: sa.Connection) -> None:
    mismatch = bind.execute(
        sa.text(
            "SELECT wallet.persona_id FROM room_point_accounts AS wallet "
            "LEFT JOIN room_point_transactions AS ledger ON ledger.persona_id = wallet.persona_id "
            "GROUP BY wallet.id, wallet.persona_id, wallet.balance "
            "HAVING wallet.balance <> COALESCE(SUM(ledger.amount), 0) LIMIT 1"
        )
    ).first()
    if mismatch is not None:
        raise RuntimeError("cannot upgrade Persona wallet scale: wallet ledger reconciliation failed")


def _assert_completed_state(bind: sa.Connection) -> None:
    version = bind.execute(sa.text("SELECT scale_version FROM room_point_scale_state WHERE id = 1")).scalar()
    if version != SCALE:
        raise RuntimeError("cannot upgrade Persona wallet scale: existing scale state is invalid")
    inspector = sa.inspect(bind)
    account_columns = {column["name"] for column in inspector.get_columns("room_point_accounts")}
    transaction_columns = {column["name"] for column in inspector.get_columns("room_point_transactions")}
    if (
        account_columns != {"id", "persona_id", "balance", "created_at", "updated_at"}
        or "persona_id" not in transaction_columns
    ):
        raise RuntimeError("cannot upgrade Persona wallet scale: existing scale schema is invalid")
