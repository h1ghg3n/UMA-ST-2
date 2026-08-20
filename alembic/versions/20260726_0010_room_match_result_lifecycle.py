"""add native Room Match result lifecycle and publication outbox

Revision ID: 20260726_0010
Revises: 20260725_0009
Create Date: 2026-07-26 00:10:00.000000

The direct previous-shape input is the exact 0009 submission table with no
submission rows. Existing RaceResult history is preserved byte-for-byte. Native
result provenance is stored in a separate association table so the historical
RaceResult physical contract owned by revision 0006 remains unchanged on a
fresh migration chain.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260726_0010"
down_revision: str | Sequence[str] | None = "20260725_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREVIOUS_REVISION = "20260725_0009"
BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
SUBMISSION_TABLE = "match_result_submissions"
PROVENANCE_TABLE = "room_match_result_provenance"
PUBLICATION_TABLE = "room_match_result_publications"

PREVIOUS_SUBMISSION_COLUMNS = {
    "id": ("integer", False, None),
    "event_id": ("integer", True, None),
    "race_id": ("integer", True, None),
    "match_type": ("string", False, 32),
    "submitted_by_discord_user_id": ("string", False, 32),
    "submission_status": ("string", False, 32),
    "raw_input_json": ("json", False, None),
    "created_at": ("datetime", False, None),
    "confirmed_at": ("datetime", True, None),
}
TARGET_SUBMISSION_COLUMNS = {
    **PREVIOUS_SUBMISSION_COLUMNS,
    "revision_number": ("integer", False, None),
    "supersedes_submission_id": ("integer", True, None),
    "current_marker": ("string", True, 16),
    "reviewed_by_discord_user_id": ("string", True, 32),
    "reviewed_at": ("datetime", True, None),
    "rejected_by_discord_user_id": ("string", True, 32),
    "rejected_at": ("datetime", True, None),
    "rejection_reason": ("string", True, 255),
    "confirmed_by_discord_user_id": ("string", True, 32),
}
TARGET_MARKER_COLUMNS = set(TARGET_SUBMISSION_COLUMNS) - set(PREVIOUS_SUBMISSION_COLUMNS)

PREVIOUS_SUBMISSION_FOREIGN_KEYS = {
    "fk_match_result_submissions_event_id_game_events": (
        ("event_id",),
        "game_events",
        ("id",),
    ),
    "fk_match_result_submissions_race_id_races": (
        ("race_id",),
        "races",
        ("id",),
    ),
}
TARGET_SUBMISSION_FOREIGN_KEYS = {
    **PREVIOUS_SUBMISSION_FOREIGN_KEYS,
    "fk_match_result_submissions_supersedes_submission_id": (
        ("supersedes_submission_id",),
        SUBMISSION_TABLE,
        ("id",),
    ),
}
TARGET_SUBMISSION_UNIQUES = {
    "uq_match_result_submissions_race_revision": ("race_id", "revision_number"),
    "uq_match_result_submissions_race_current": ("race_id", "current_marker"),
    "uq_match_result_submissions_supersedes": ("supersedes_submission_id",),
}
TARGET_SUBMISSION_CHECKS = {
    "ck_match_result_submissions_wu10_positive_revision",
    "ck_match_result_submissions_wu10_submission_status",
    "ck_match_result_submissions_wu10_review_metadata",
    "ck_match_result_submissions_wu10_rejection_metadata",
    "ck_match_result_submissions_wu10_confirmation_metadata",
    "ck_match_result_submissions_wu10_submission_state",
}

PROVENANCE_COLUMNS = {
    "id": ("integer", False, None),
    "race_result_id": ("integer", False, None),
    "match_result_submission_id": ("integer", False, None),
    "created_at": ("datetime", False, None),
}
PROVENANCE_FOREIGN_KEYS = {
    "fk_rm_result_provenance_result": (
        ("race_result_id",),
        "race_results",
        ("id",),
    ),
    "fk_rm_result_provenance_submission": (
        ("match_result_submission_id",),
        SUBMISSION_TABLE,
        ("id",),
    ),
}
PROVENANCE_UNIQUES = {
    "uq_room_match_result_provenance_race_result": ("race_result_id",),
}

PUBLICATION_COLUMNS = {
    "id": ("integer", False, None),
    "race_id": ("integer", False, None),
    "match_result_submission_id": ("integer", False, None),
    "target_channel_id": ("string", False, 32),
    "payload_json": ("json", False, None),
    "request_fingerprint": ("string", False, 64),
    "status": ("string", False, 32),
    "attempt_count": ("integer", False, None),
    "discord_message_id": ("string", True, 32),
    "last_error_code": ("string", True, 64),
    "published_at": ("datetime", True, None),
    "created_at": ("datetime", False, None),
    "updated_at": ("datetime", False, None),
}
PUBLICATION_FOREIGN_KEYS = {
    "fk_rm_result_publication_race": (
        ("race_id",),
        "races",
        ("id",),
    ),
    "fk_rm_result_publication_submission": (
        ("match_result_submission_id",),
        SUBMISSION_TABLE,
        ("id",),
    ),
}
PUBLICATION_UNIQUES = {
    "uq_room_match_result_publications_race": ("race_id",),
    "uq_room_match_result_publications_submission": ("match_result_submission_id",),
}
PUBLICATION_CHECKS = {
    "ck_room_match_result_publications_wu10_publication_status",
    "ck_room_match_result_publications_wu10_publication_attempt_count",
    "ck_room_match_result_publications_wu10_publication_state",
}


def upgrade() -> None:
    connection = op.get_bind()
    mode = _preflight(connection)
    if mode == "previous":
        _upgrade_previous_shape()
    _verify_target(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _require_revision(connection, revision)
    _verify_target(connection)
    _refuse_operational_downgrade(connection)

    op.drop_table(PUBLICATION_TABLE)
    op.drop_table(PROVENANCE_TABLE)

    if connection.dialect.name in {"mariadb", "mysql"}:
        op.create_index(
            "fk_match_result_submissions_race_id_races",
            SUBMISSION_TABLE,
            ["race_id"],
        )

    with op.batch_alter_table(SUBMISSION_TABLE) as batch:
        batch.drop_constraint(
            op.f("fk_match_result_submissions_supersedes_submission_id"),
            type_="foreignkey",
        )
        for name in sorted(TARGET_SUBMISSION_CHECKS):
            batch.drop_constraint(op.f(name), type_="check")
        for name in sorted(TARGET_SUBMISSION_UNIQUES):
            batch.drop_constraint(op.f(name), type_="unique")
        for column_name in (
            "confirmed_by_discord_user_id",
            "rejection_reason",
            "rejected_at",
            "rejected_by_discord_user_id",
            "reviewed_at",
            "reviewed_by_discord_user_id",
            "current_marker",
            "supersedes_submission_id",
            "revision_number",
        ):
            batch.drop_column(column_name)
        batch.alter_column(
            "submission_status",
            existing_type=sa.String(32),
            existing_nullable=False,
            server_default=None,
        )

    _verify_previous(sa.inspect(connection))


def _upgrade_previous_shape() -> None:
    with op.batch_alter_table(SUBMISSION_TABLE) as batch:
        batch.add_column(
            sa.Column(
                "revision_number",
                sa.Integer(),
                server_default="1",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("supersedes_submission_id", BIGINT, nullable=True))
        batch.add_column(sa.Column("current_marker", sa.String(16), nullable=True))
        batch.add_column(sa.Column("reviewed_by_discord_user_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("rejected_by_discord_user_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("rejection_reason", sa.String(255), nullable=True))
        batch.add_column(sa.Column("confirmed_by_discord_user_id", sa.String(32), nullable=True))
        batch.alter_column(
            "submission_status",
            existing_type=sa.String(32),
            existing_nullable=False,
            server_default="pending_review",
        )
        batch.create_unique_constraint(
            "uq_match_result_submissions_race_revision",
            ["race_id", "revision_number"],
        )
        batch.create_unique_constraint(
            "uq_match_result_submissions_race_current",
            ["race_id", "current_marker"],
        )
        batch.create_unique_constraint(
            "uq_match_result_submissions_supersedes",
            ["supersedes_submission_id"],
        )
        batch.create_foreign_key(
            "fk_match_result_submissions_supersedes_submission_id",
            SUBMISSION_TABLE,
            ["supersedes_submission_id"],
            ["id"],
        )
        batch.create_check_constraint(
            "wu10_positive_revision",
            "revision_number > 0",
        )
        batch.create_check_constraint(
            "wu10_submission_status",
            "submission_status IN ('pending_review', 'reviewed', 'superseded', 'rejected', 'confirmed')",
        )
        batch.create_check_constraint(
            "wu10_review_metadata",
            "(reviewed_at IS NULL AND reviewed_by_discord_user_id IS NULL) OR "
            "(reviewed_at IS NOT NULL AND reviewed_by_discord_user_id IS NOT NULL)",
        )
        batch.create_check_constraint(
            "wu10_rejection_metadata",
            "(rejected_at IS NULL AND rejected_by_discord_user_id IS NULL AND rejection_reason IS NULL) OR "
            "(rejected_at IS NOT NULL AND rejected_by_discord_user_id IS NOT NULL AND rejection_reason IS NOT NULL)",
        )
        batch.create_check_constraint(
            "wu10_confirmation_metadata",
            "(confirmed_at IS NULL AND confirmed_by_discord_user_id IS NULL) OR "
            "(confirmed_at IS NOT NULL AND confirmed_by_discord_user_id IS NOT NULL)",
        )
        batch.create_check_constraint(
            "wu10_submission_state",
            "(submission_status = 'pending_review' "
            "AND current_marker = 'current' "
            "AND reviewed_at IS NULL "
            "AND rejected_at IS NULL "
            "AND confirmed_at IS NULL) OR "
            "(submission_status = 'reviewed' "
            "AND current_marker = 'current' "
            "AND reviewed_at IS NOT NULL "
            "AND rejected_at IS NULL "
            "AND confirmed_at IS NULL) OR "
            "(submission_status = 'superseded' "
            "AND current_marker IS NULL "
            "AND rejected_at IS NULL "
            "AND confirmed_at IS NULL) OR "
            "(submission_status = 'rejected' "
            "AND current_marker IS NULL "
            "AND rejected_at IS NOT NULL "
            "AND confirmed_at IS NULL) OR "
            "(submission_status = 'confirmed' "
            "AND current_marker = 'current' "
            "AND reviewed_at IS NOT NULL "
            "AND rejected_at IS NULL "
            "AND confirmed_at IS NOT NULL)",
        )

    with op.batch_alter_table(SUBMISSION_TABLE) as batch:
        batch.alter_column(
            "revision_number",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=None,
        )

    connection = op.get_bind()
    if connection.dialect.name in {"mariadb", "mysql"}:
        index_names = {str(item.get("name")) for item in sa.inspect(connection).get_indexes(SUBMISSION_TABLE)}
        if "fk_match_result_submissions_race_id_races" in index_names:
            op.drop_index(
                "fk_match_result_submissions_race_id_races",
                table_name=SUBMISSION_TABLE,
            )

    op.create_table(
        PROVENANCE_TABLE,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("race_result_id", BIGINT, nullable=False),
        sa.Column("match_result_submission_id", BIGINT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["race_result_id"],
            ["race_results.id"],
            name="fk_rm_result_provenance_result",
        ),
        sa.ForeignKeyConstraint(
            ["match_result_submission_id"],
            [f"{SUBMISSION_TABLE}.id"],
            name="fk_rm_result_provenance_submission",
        ),
        sa.UniqueConstraint(
            "race_result_id",
            name="uq_room_match_result_provenance_race_result",
        ),
    )

    op.create_table(
        PUBLICATION_TABLE,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("race_id", BIGINT, nullable=False),
        sa.Column("match_result_submission_id", BIGINT, nullable=False),
        sa.Column("target_channel_id", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("discord_message_id", sa.String(32), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["race_id"],
            ["races.id"],
            name="fk_rm_result_publication_race",
        ),
        sa.ForeignKeyConstraint(
            ["match_result_submission_id"],
            [f"{SUBMISSION_TABLE}.id"],
            name="fk_rm_result_publication_submission",
        ),
        sa.UniqueConstraint(
            "race_id",
            name="uq_room_match_result_publications_race",
        ),
        sa.UniqueConstraint(
            "match_result_submission_id",
            name="uq_room_match_result_publications_submission",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'delivery_unknown')",
            name="wu10_publication_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="wu10_publication_attempt_count",
        ),
        sa.CheckConstraint(
            "(status = 'pending' "
            "AND discord_message_id IS NULL "
            "AND published_at IS NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'sent' "
            "AND discord_message_id IS NOT NULL "
            "AND published_at IS NOT NULL "
            "AND last_error_code IS NULL) OR "
            "(status IN ('failed', 'delivery_unknown') "
            "AND discord_message_id IS NULL "
            "AND published_at IS NULL "
            "AND last_error_code IS NOT NULL)",
            name="wu10_publication_state",
        ),
    )


def _preflight(connection: sa.Connection) -> str:
    _require_revision(connection, PREVIOUS_REVISION)
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    required = {
        "alembic_version",
        "races",
        "race_entries",
        "race_results",
        "race_operation_audits",
        SUBMISSION_TABLE,
    }
    if not required.issubset(tables):
        _unsupported("required canonical 0009 tables are missing")

    submission_columns = {column["name"] for column in inspector.get_columns(SUBMISSION_TABLE)}
    target_tables = {PROVENANCE_TABLE, PUBLICATION_TABLE}
    marker_columns = submission_columns & TARGET_MARKER_COLUMNS
    present_target_tables = tables & target_tables

    if not marker_columns and not present_target_tables:
        _verify_previous(inspector)
        if _has_row(connection, SUBMISSION_TABLE):
            _unsupported("legacy match_result_submissions must be empty; revision lineage cannot be inferred")
        _validate_race_results(connection, target=False)
        return "previous"

    if marker_columns == TARGET_MARKER_COLUMNS and present_target_tables == target_tables:
        _verify_target(connection)
        return "target"

    _unsupported("partial or hybrid WU10 schema markers are present")


def _verify_previous(inspector: sa.Inspector) -> None:
    _validate_exact_columns(
        inspector,
        SUBMISSION_TABLE,
        PREVIOUS_SUBMISSION_COLUMNS,
    )
    _validate_primary_key(inspector, SUBMISSION_TABLE, ("id",))
    _validate_foreign_keys(
        inspector,
        SUBMISSION_TABLE,
        PREVIOUS_SUBMISSION_FOREIGN_KEYS,
    )
    _validate_named_columns(
        inspector.get_unique_constraints(SUBMISSION_TABLE),
        {},
        kind="unique constraints",
        table_name=SUBMISSION_TABLE,
    )
    _validate_named_set(
        inspector.get_check_constraints(SUBMISSION_TABLE),
        set(),
        kind="check constraints",
        table_name=SUBMISSION_TABLE,
    )
    _validate_indexes(
        inspector,
        SUBMISSION_TABLE,
        mariadb_expected={
            "fk_match_result_submissions_event_id_game_events",
            "fk_match_result_submissions_race_id_races",
        },
    )


def _verify_target(connection: sa.Connection) -> None:
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if not {PROVENANCE_TABLE, PUBLICATION_TABLE}.issubset(tables):
        raise RuntimeError("WU10 target verification failed: result lifecycle tables are missing")

    _validate_exact_columns(
        inspector,
        SUBMISSION_TABLE,
        TARGET_SUBMISSION_COLUMNS,
        target=True,
    )
    _validate_primary_key(inspector, SUBMISSION_TABLE, ("id",))
    _validate_foreign_keys(
        inspector,
        SUBMISSION_TABLE,
        TARGET_SUBMISSION_FOREIGN_KEYS,
        target=True,
    )
    _validate_named_columns(
        inspector.get_unique_constraints(SUBMISSION_TABLE),
        TARGET_SUBMISSION_UNIQUES,
        kind="unique constraints",
        table_name=SUBMISSION_TABLE,
        target=True,
    )
    _validate_named_set(
        inspector.get_check_constraints(SUBMISSION_TABLE),
        TARGET_SUBMISSION_CHECKS,
        kind="check constraints",
        table_name=SUBMISSION_TABLE,
        target=True,
    )
    _validate_indexes(
        inspector,
        SUBMISSION_TABLE,
        mariadb_expected={"fk_match_result_submissions_event_id_game_events"},
        target=True,
    )

    _verify_new_table(
        inspector,
        PROVENANCE_TABLE,
        columns=PROVENANCE_COLUMNS,
        foreign_keys=PROVENANCE_FOREIGN_KEYS,
        uniques=PROVENANCE_UNIQUES,
        checks=set(),
        mariadb_indexes={"fk_rm_result_provenance_submission"},
    )
    _verify_new_table(
        inspector,
        PUBLICATION_TABLE,
        columns=PUBLICATION_COLUMNS,
        foreign_keys=PUBLICATION_FOREIGN_KEYS,
        uniques=PUBLICATION_UNIQUES,
        checks=PUBLICATION_CHECKS,
        mariadb_indexes=set(),
    )
    _validate_race_results(connection, target=True)
    _validate_target_semantics(connection)


def _verify_new_table(
    inspector: sa.Inspector,
    table_name: str,
    *,
    columns: dict[str, tuple[str, bool, int | None]],
    foreign_keys: dict[str, tuple[tuple[str, ...], str, tuple[str, ...]]],
    uniques: dict[str, tuple[str, ...]],
    checks: set[str],
    mariadb_indexes: set[str],
) -> None:
    _validate_exact_columns(inspector, table_name, columns, target=True)
    _validate_primary_key(inspector, table_name, ("id",), target=True)
    _validate_foreign_keys(inspector, table_name, foreign_keys, target=True)
    _validate_named_columns(
        inspector.get_unique_constraints(table_name),
        uniques,
        kind="unique constraints",
        table_name=table_name,
        target=True,
    )
    _validate_named_set(
        inspector.get_check_constraints(table_name),
        checks,
        kind="check constraints",
        table_name=table_name,
        target=True,
    )
    _validate_indexes(
        inspector,
        table_name,
        mariadb_expected=mariadb_indexes,
        target=True,
    )


def _validate_race_results(connection: sa.Connection, *, target: bool) -> None:
    invalid = connection.execute(
        sa.text(
            "SELECT rr.id FROM race_results rr "
            "JOIN races r ON r.id = rr.race_id "
            "WHERE rr.entry_number <= 0 OR rr.rank <= 0 "
            "OR (r.race_kind = 'room_match' AND NOT EXISTS ("
            "SELECT 1 FROM race_entries re "
            "WHERE re.race_id = rr.race_id "
            "AND re.entry_number = rr.entry_number "
            "AND re.entry_kind = 'room_match')) "
            "ORDER BY rr.id LIMIT 1"
        )
    ).first()
    if invalid is not None:
        _unsupported("existing RaceResult history has invalid rank or final-entry membership")

    review_predicate = (
        "AND NOT EXISTS ("
        "SELECT 1 FROM match_result_submissions s "
        "WHERE s.race_id = r.id "
        "AND s.current_marker = 'current' "
        "AND s.submission_status IN ('pending_review', 'reviewed'))"
        if target
        else ""
    )
    unsupported_review = connection.execute(
        sa.text(
            "SELECT r.id FROM races r "
            "WHERE r.race_kind = 'room_match' "
            "AND r.status = 'result_review' "
            f"{review_predicate} "
            "ORDER BY r.id LIMIT 1"
        )
    ).first()
    if unsupported_review is not None:
        _unsupported("result_review state without WU10 submission provenance is unsupported")


def _validate_target_semantics(connection: sa.Connection) -> None:
    invalid_submission = connection.execute(
        sa.text(
            "SELECT s.id FROM match_result_submissions s "
            "LEFT JOIN match_result_submissions previous "
            "ON previous.id = s.supersedes_submission_id "
            "WHERE s.race_id IS NULL "
            "OR s.match_type NOT IN ('regular_room_match', 'irregular_room_match') "
            "OR (s.revision_number = 1 AND s.supersedes_submission_id IS NOT NULL) "
            "OR (s.revision_number > 1 AND ("
            "s.supersedes_submission_id IS NULL "
            "OR previous.race_id <> s.race_id "
            "OR previous.revision_number <> s.revision_number - 1)) "
            "ORDER BY s.id LIMIT 1"
        )
    ).first()
    if invalid_submission is not None:
        _unsupported("target submission revision lineage is invalid")

    invalid_current_state = connection.execute(
        sa.text(
            "SELECT s.id FROM match_result_submissions s "
            "JOIN races r ON r.id = s.race_id "
            "WHERE (s.submission_status IN ('pending_review', 'reviewed') "
            "AND r.status <> 'result_review') "
            "OR (s.submission_status = 'confirmed' "
            "AND r.status <> 'result_confirmed') "
            "ORDER BY s.id LIMIT 1"
        )
    ).first()
    if invalid_current_state is not None:
        _unsupported("target submission status does not match the Room Match Race lifecycle")

    invalid_provenance = connection.execute(
        sa.text(
            "SELECT p.id FROM room_match_result_provenance p "
            "JOIN race_results rr ON rr.id = p.race_result_id "
            "JOIN match_result_submissions s ON s.id = p.match_result_submission_id "
            "WHERE s.submission_status <> 'confirmed' OR s.race_id <> rr.race_id "
            "ORDER BY p.id LIMIT 1"
        )
    ).first()
    if invalid_provenance is not None:
        _unsupported("target native RaceResult provenance is invalid")

    invalid_snapshot = connection.execute(
        sa.text(
            "SELECT p.id FROM room_match_result_provenance p "
            "JOIN race_results rr ON rr.id = p.race_result_id "
            "JOIN race_entries re "
            "ON re.race_id = rr.race_id "
            "AND re.entry_number = rr.entry_number "
            "WHERE re.entry_kind <> 'room_match' "
            "OR NOT (rr.game_account_id = re.game_account_id "
            "OR (rr.game_account_id IS NULL AND re.game_account_id IS NULL)) "
            "OR NOT (rr.character_name = re.horse_name_or_label "
            "OR (rr.character_name IS NULL AND re.horse_name_or_label IS NULL)) "
            "ORDER BY p.id LIMIT 1"
        )
    ).first()
    if invalid_snapshot is not None:
        _unsupported("native RaceResult identity or character snapshot differs from the final RaceEntry")

    incomplete_confirmation = connection.execute(
        sa.text(
            "SELECT s.id FROM match_result_submissions s "
            "WHERE s.submission_status = 'confirmed' "
            "AND ("
            "(SELECT COUNT(*) FROM race_results rr WHERE rr.race_id = s.race_id) = 0 "
            "OR (SELECT COUNT(*) FROM race_results rr WHERE rr.race_id = s.race_id) "
            "<> (SELECT COUNT(*) FROM room_match_result_provenance p "
            "WHERE p.match_result_submission_id = s.id)"
            ") ORDER BY s.id LIMIT 1"
        )
    ).first()
    if incomplete_confirmation is not None:
        _unsupported("confirmed submission does not own one complete native result set")

    invalid_publication = connection.execute(
        sa.text(
            "SELECT p.id FROM room_match_result_publications p "
            "JOIN match_result_submissions s ON s.id = p.match_result_submission_id "
            "WHERE s.submission_status <> 'confirmed' OR s.race_id <> p.race_id "
            "ORDER BY p.id LIMIT 1"
        )
    ).first()
    if invalid_publication is not None:
        _unsupported("target publication does not match a confirmed submission")


def _refuse_operational_downgrade(connection: sa.Connection) -> None:
    if _has_row(connection, SUBMISSION_TABLE):
        raise RuntimeError("WU10 downgrade refused after result submission history; restore the verified backup")
    if _has_row(connection, PROVENANCE_TABLE):
        raise RuntimeError("WU10 downgrade refused after native result provenance; restore the verified backup")
    if _has_row(connection, PUBLICATION_TABLE):
        raise RuntimeError("WU10 downgrade refused after publication history; restore the verified backup")
    if (
        connection.execute(
            sa.text("SELECT 1 FROM race_operation_audits WHERE action LIKE 'room_result_%' LIMIT 1")
        ).first()
        is not None
    ):
        raise RuntimeError("WU10 downgrade refused after result audit history; restore the verified backup")
    if (
        connection.execute(
            sa.text("SELECT 1 FROM races WHERE race_kind = 'room_match' AND status = 'result_review' LIMIT 1")
        ).first()
        is not None
    ):
        raise RuntimeError("WU10 downgrade refused after result_review state; restore the verified backup")


def _validate_exact_columns(
    inspector: sa.Inspector,
    table_name: str,
    expected: dict[str, tuple[str, bool, int | None]],
    *,
    target: bool = False,
) -> None:
    actual_rows = inspector.get_columns(table_name)
    actual_names = {row["name"] for row in actual_rows}
    if actual_names != set(expected):
        _shape_error(
            target,
            f"{table_name} columns differ from the exact contract",
        )
    for row in actual_rows:
        name = row["name"]
        family, nullable, length = expected[name]
        if _type_family(row["type"]) != family or bool(row["nullable"]) is not nullable:
            _shape_error(target, f"{table_name}.{name} type or nullability is invalid")
        if family == "string" and getattr(row["type"], "length", None) != length:
            _shape_error(target, f"{table_name}.{name} length is invalid")
        if row.get("computed") is not None or row.get("identity") is not None:
            _shape_error(target, f"{table_name}.{name} cannot be generated or identity")


def _validate_primary_key(
    inspector: sa.Inspector,
    table_name: str,
    expected_columns: tuple[str, ...],
    *,
    target: bool = False,
) -> None:
    primary = inspector.get_pk_constraint(table_name)
    if tuple(primary.get("constrained_columns") or ()) != expected_columns:
        _shape_error(target, f"{table_name} primary key differs from the exact contract")
    expected_name = f"pk_{table_name}"
    allowed_names = (
        {None, "PRIMARY", expected_name} if inspector.bind.dialect.name in {"mariadb", "mysql"} else {expected_name}
    )
    if primary.get("name") not in allowed_names:
        _shape_error(target, f"{table_name} primary key name differs from the exact contract")


def _validate_foreign_keys(
    inspector: sa.Inspector,
    table_name: str,
    expected: dict[str, tuple[tuple[str, ...], str, tuple[str, ...]]],
    *,
    target: bool = False,
) -> None:
    actual = {
        row.get("name"): (
            tuple(row.get("constrained_columns") or ()),
            row.get("referred_table"),
            tuple(row.get("referred_columns") or ()),
        )
        for row in inspector.get_foreign_keys(table_name)
    }
    if actual != expected:
        _shape_error(target, f"{table_name} foreign keys differ from the exact contract")


def _validate_named_columns(
    rows: list[dict[str, object]],
    expected: dict[str, tuple[str, ...]],
    *,
    kind: str,
    table_name: str,
    target: bool = False,
) -> None:
    actual = {str(row.get("name")): tuple(row.get("column_names") or ()) for row in rows}
    if actual != expected:
        _shape_error(target, f"{table_name} {kind} differ from the exact contract")


def _validate_named_set(
    rows: list[dict[str, object]],
    expected: set[str],
    *,
    kind: str,
    table_name: str,
    target: bool = False,
) -> None:
    actual = {str(row.get("name")) for row in rows if kind != "indexes" or row.get("duplicates_constraint") is None}
    if actual != expected:
        _shape_error(target, f"{table_name} {kind} differ from the exact contract")


def _validate_indexes(
    inspector: sa.Inspector,
    table_name: str,
    *,
    mariadb_expected: set[str],
    target: bool = False,
) -> None:
    unique_names = {str(row.get("name")) for row in inspector.get_unique_constraints(table_name)}
    actual = {
        str(row.get("name"))
        for row in inspector.get_indexes(table_name)
        if row.get("duplicates_constraint") is None and str(row.get("name")) not in unique_names
    }
    expected = mariadb_expected if inspector.bind.dialect.name in {"mariadb", "mysql"} else set()
    if actual != expected:
        _shape_error(target, f"{table_name} indexes differ from the exact contract")


def _type_family(value: sa.types.TypeEngine) -> str:
    if isinstance(value, (sa.BigInteger, sa.Integer)):
        return "integer"
    if isinstance(value, sa.JSON) or type(value).__name__.upper() in {
        "JSON",
        "LONGTEXT",
    }:
        return "json"
    if isinstance(value, sa.String):
        return "string"
    if isinstance(value, sa.DateTime):
        return "datetime"
    return value.__class__.__name__.lower()


def _has_row(connection: sa.Connection, table_name: str) -> bool:
    return connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first() is not None


def _require_revision(connection: sa.Connection, expected: str) -> None:
    tables = set(sa.inspect(connection).get_table_names())
    if "alembic_version" not in tables:
        _unsupported("alembic_version table is missing")
    current = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    if current != expected:
        _unsupported(f"database is not at exact revision {expected}")


def _shape_error(target: bool, detail: str) -> None:
    if target:
        raise RuntimeError(f"WU10 target verification failed: {detail}")
    _unsupported(detail)


def _unsupported(detail: str) -> None:
    raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: {detail}")
