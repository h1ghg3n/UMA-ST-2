"""allow audited Persona links from the Discord staff adapter

Revision ID: 20260814_0028
Revises: 20260813_0027
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0028"
down_revision: str | Sequence[str] | None = "20260813_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "persona_link_operation_audits"
CONSTRAINT_NAME = "source"
PREVIOUS_SOURCE_CHECK = "source = 'server_console'"
CURRENT_SOURCE_CHECK = "source IN ('server_console', 'discord_staff')"


def upgrade() -> None:
    _replace_source_constraint(CURRENT_SOURCE_CHECK)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT 1 FROM {TABLE_NAME} WHERE source = 'discord_staff' LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade Persona link audit source after Discord staff audit data exists")
    _replace_source_constraint(PREVIOUS_SOURCE_CHECK)


def _replace_source_constraint(expression: str) -> None:
    bind = op.get_bind()
    if TABLE_NAME not in set(sa.inspect(bind).get_table_names()):
        raise RuntimeError(f"cannot migrate Persona link audit source: missing {TABLE_NAME}")
    source_constraints = [
        item
        for item in sa.inspect(bind).get_check_constraints(TABLE_NAME)
        if "source" in str(item.get("sqltext") or "").lower()
        and "server_console" in str(item.get("sqltext") or "").lower()
    ]
    if len(source_constraints) != 1 or not source_constraints[0].get("name"):
        raise RuntimeError("cannot migrate Persona link audit source: source constraint is missing")
    existing_name = str(source_constraints[0]["name"])
    with op.batch_alter_table(TABLE_NAME) as batch:
        batch.drop_constraint(op.f(existing_name), type_="check")
        batch.create_check_constraint(CONSTRAINT_NAME, expression)
