from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from umacircle_bot.db.base import Base

BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")
RATING_NUMERIC_PRECISION = 30
RATING_NUMERIC_SCALE = 18
RATING_NUMERIC = Numeric(RATING_NUMERIC_PRECISION, RATING_NUMERIC_SCALE)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Persona(TimestampMixin, Base):
    __tablename__ = "personas"
    __table_args__ = (
        CheckConstraint("display_name <> ''", name="nonempty_display_name"),
        CheckConstraint(
            "display_name_source IN ('discord', 'main_game_account', 'manual')",
            name="display_name_source",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'suspended', 'archived')",
            name="status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name_source: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    main_game_account_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "game_accounts.id",
            name="fk_personas_main_game_account_id_game_accounts",
            use_alter=True,
        ),
        unique=True,
        nullable=True,
    )

    discord_accounts: Mapped[list["DiscordAccount"]] = relationship(back_populates="persona")
    game_accounts: Mapped[list["GameAccount"]] = relationship(
        back_populates="persona",
        foreign_keys=lambda: [GameAccount.persona_id],
    )
    main_game_account: Mapped["GameAccount | None"] = relationship(
        "GameAccount",
        foreign_keys=[main_game_account_id],
        primaryjoin=lambda: Persona.main_game_account_id == GameAccount.id,
        viewonly=True,
    )
    circle_point_account: Mapped["CirclePointAccount | None"] = relationship(back_populates="persona")


class DiscordAccount(TimestampMixin, Base):
    __tablename__ = "discord_accounts"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    discord_nickname: Mapped[str] = mapped_column(String(100), nullable=False)
    persona_id: Mapped[str | None] = mapped_column(ForeignKey("personas.id"), nullable=True, index=True)

    # A Discord account remains a compatibility pointer for historical records,
    # but a Persona can own more than one GameAccount.
    game_accounts: Mapped[list["GameAccount"]] = relationship(back_populates="discord_account")
    persona: Mapped["Persona | None"] = relationship(back_populates="discord_accounts")


class GuildDiscordSettings(TimestampMixin, Base):
    __tablename__ = "guild_discord_settings"
    __table_args__ = (
        CheckConstraint("revision_number > 0", name="positive_revision"),
        CheckConstraint("default_timezone IN ('KST', 'UTC')", name="default_timezone"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    win5_announcement_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    room_match_announcement_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    log_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    operator_role_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bot_manager_role_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    default_timezone: Mapped[str] = mapped_column(String(3), default="KST", server_default="KST", nullable=False)
    win5_announcements_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    room_match_announcements_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    revision_number: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)


class GuildDiscordSettingsAudit(Base):
    __tablename__ = "guild_discord_settings_audits"
    __table_args__ = (
        CheckConstraint(
            "action = 'guild_discord_settings_update'",
            name="action",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    guild_discord_settings_id: Mapped[int] = mapped_column(
        ForeignKey(
            "guild_discord_settings.id",
            name="fk_guild_settings_audits_settings",
        ),
        nullable=False,
    )
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    before_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DiscordPublication(TimestampMixin, Base):
    __tablename__ = "discord_publications"
    __table_args__ = (
        Index(
            "ix_discord_publications_status_attempt_started_at",
            "status",
            "attempt_started_at",
            "id",
        ),
        UniqueConstraint(
            "guild_id",
            "destination_kind",
            "event_key",
            name="uq_discord_publications_guild_destination_event",
        ),
        CheckConstraint(
            "destination_kind IN ('win5_announcement', 'room_match_announcement', 'log_mirror')",
            name="destination_kind",
        ),
        CheckConstraint(
            "status IN ('suppressed', 'awaiting_channel', 'ready', 'pending', 'sent', 'failed', 'delivery_unknown')",
            name="status",
        ),
        CheckConstraint("attempt_count >= 0", name="nonnegative_attempt_count"),
        CheckConstraint(
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
            "AND attempt_started_at IS NOT NULL AND published_at IS NULL)",
            name="state",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    destination_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int] = mapped_column(BIGINT_PK, nullable=False)
    target_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    discord_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DiscordPublicationAudit(Base):
    __tablename__ = "discord_publication_audits"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    publication_id: Mapped[int] = mapped_column(
        ForeignKey("discord_publications.id", name="fk_discord_publication_audits_publication"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GameAccount(TimestampMixin, Base):
    __tablename__ = "game_accounts"
    __table_args__ = (
        CheckConstraint(
            "(identity_status = 'confirmed_identity' AND uma_pid IS NOT NULL) OR "
            "(identity_status IN ('pending_identity', 'identity_conflict', 'registration_cancelled') "
            "AND uma_pid IS NULL)",
            name="identity_state",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    discord_account_id: Mapped[int | None] = mapped_column(ForeignKey("discord_accounts.id"), nullable=True)
    persona_id: Mapped[str | None] = mapped_column(ForeignKey("personas.id"), nullable=True, index=True)
    uma_pid: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    nickname: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ingame_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    identity_status: Mapped[str] = mapped_column(String(32), default="confirmed_identity", nullable=False)

    discord_account: Mapped["DiscordAccount | None"] = relationship(back_populates="game_accounts")
    persona: Mapped["Persona | None"] = relationship(
        back_populates="game_accounts",
        foreign_keys=[persona_id],
    )
    identity_backfill_task: Mapped["IdentityBackfillTask | None"] = relationship(back_populates="game_account")


class IdentityBackfillTask(TimestampMixin, Base):
    __tablename__ = "identity_backfill_tasks"
    __table_args__ = (CheckConstraint("status IN ('pending', 'conflict', 'resolved')", name="status"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    game_account_id: Mapped[int] = mapped_column(ForeignKey("game_accounts.id"), unique=True, nullable=False)
    source_import_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("sheet_import_records.id"), unique=True, nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    conflict_detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    resolved_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    game_account: Mapped["GameAccount"] = relationship(back_populates="identity_backfill_task")


class PlayerLinkRequest(TimestampMixin, Base):
    __tablename__ = "player_link_requests"
    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "requester_discord_user_id",
            "active_request_marker",
            name="uq_player_link_requests_active_requester",
        ),
        UniqueConstraint("selected_game_account_id", name="uq_player_link_requests_selected_game_account"),
        CheckConstraint(
            "status IN ('pending', 'review_required', 'approved', 'rejected', 'cancelled')",
            name="status",
        ),
        CheckConstraint(
            "active_request_marker IS NULL OR active_request_marker = 1",
            name="active_request_marker",
        ),
        CheckConstraint(
            "(status IN ('pending', 'review_required') AND active_request_marker = 1 "
            "AND selected_game_account_id IS NULL AND reviewed_by_discord_user_id IS NULL "
            "AND resolved_at IS NULL) OR "
            "(status = 'approved' AND active_request_marker IS NULL "
            "AND selected_game_account_id IS NOT NULL AND reviewed_by_discord_user_id IS NOT NULL "
            "AND resolved_at IS NOT NULL) OR "
            "(status = 'rejected' AND active_request_marker IS NULL "
            "AND selected_game_account_id IS NULL AND reviewed_by_discord_user_id IS NOT NULL "
            "AND resolved_at IS NOT NULL) OR "
            "(status = 'cancelled' AND active_request_marker IS NULL "
            "AND selected_game_account_id IS NULL AND reviewed_by_discord_user_id IS NULL "
            "AND resolved_at IS NULL)",
            name="state",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    requester_discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    discord_nickname_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    submitted_ingame_name: Mapped[str] = mapped_column(String(100), nullable=False)
    submitted_uma_pid: Mapped[str] = mapped_column(String(32), nullable=False)
    submitted_nickname_chunk: Mapped[str | None] = mapped_column(String(100), nullable=True)
    submitted_participation_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requester_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending", nullable=False)
    active_request_marker: Mapped[int | None] = mapped_column(Integer, default=1, server_default="1", nullable=True)
    selected_game_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("game_accounts.id", name="fk_player_link_requests_selected_game_account"),
        nullable=True,
    )
    reviewed_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlayerLinkOperationAudit(Base):
    __tablename__ = "player_link_operation_audits"
    __table_args__ = (
        CheckConstraint(
            "action IN ('submit', 'revise', 'cancel', 'review', 'approve', 'reject')",
            name="action",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    player_link_request_id: Mapped[int] = mapped_column(
        ForeignKey("player_link_requests.id", name="fk_player_link_operation_audits_request"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AccountRegistrationRequest(TimestampMixin, Base):
    __tablename__ = "account_registration_requests"
    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "requester_discord_user_id",
            "active_request_marker",
            name="active_request",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')",
            name="status",
        ),
        CheckConstraint(
            "active_request_marker IS NULL OR active_request_marker = 1",
            name="active_request_marker",
        ),
        CheckConstraint(
            "(status = 'pending' AND active_request_marker = 1 AND reviewed_by_discord_user_id IS NULL "
            "AND accepted_persona_id IS NULL AND accepted_game_account_id IS NULL "
            "AND resolved_at IS NULL) OR "
            "(status = 'approved' AND active_request_marker IS NULL AND reviewed_by_discord_user_id IS NOT NULL "
            "AND accepted_persona_id IS NOT NULL AND accepted_game_account_id IS NOT NULL "
            "AND resolved_at IS NOT NULL) OR "
            "(status = 'rejected' AND active_request_marker IS NULL AND reviewed_by_discord_user_id IS NOT NULL "
            "AND accepted_persona_id IS NULL AND accepted_game_account_id IS NULL "
            "AND resolved_at IS NOT NULL) OR "
            "(status = 'cancelled' AND active_request_marker IS NULL AND reviewed_by_discord_user_id IS NULL "
            "AND accepted_persona_id IS NULL AND accepted_game_account_id IS NULL "
            "AND resolved_at IS NOT NULL)",
            name="lifecycle",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    requester_discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    discord_nickname_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    submitted_uma_pid: Mapped[str] = mapped_column(String(32), nullable=False)
    submitted_nickname: Mapped[str | None] = mapped_column(String(100), nullable=True)
    submitted_ingame_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    active_request_marker: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    accepted_persona_id: Mapped[str | None] = mapped_column(
        ForeignKey("personas.id", name="fk_account_registration_requests_accepted_persona"), nullable=True
    )
    accepted_game_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("game_accounts.id", name="fk_account_registration_requests_accepted_game_account"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccountRegistrationOperationAudit(Base):
    __tablename__ = "account_registration_operation_audits"
    __table_args__ = (
        CheckConstraint(
            "action IN ('submit', 'cancel', 'approve', 'reject')",
            name="action",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_registration_request_id: Mapped[int] = mapped_column(
        ForeignKey(
            "account_registration_requests.id",
            name="fk_account_registration_operation_audits_request",
        ),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PersonaLinkOperationAudit(Base):
    __tablename__ = "persona_link_operation_audits"
    __table_args__ = (
        CheckConstraint(
            "action IN ('discord_attach', 'discord_detach', 'discord_transfer', "
            "'game_attach', 'game_detach', 'game_transfer', 'set_main_game_account', "
            "'set_display_name', 'restore_display_name_source')",
            name="action",
        ),
        CheckConstraint("source IN ('server_console', 'discord_staff')", name="source"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    target_persona_id: Mapped[str] = mapped_column(
        ForeignKey("personas.id", name="fk_persona_link_audits_target_persona"), nullable=False
    )
    from_persona_id: Mapped[str | None] = mapped_column(
        ForeignKey("personas.id", name="fk_persona_link_audits_from_persona"), nullable=True
    )
    discord_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("discord_accounts.id", name="fk_persona_link_audits_discord_account"), nullable=True
    )
    game_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("game_accounts.id", name="fk_persona_link_audits_game_account"), nullable=True
    )
    before_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CirclePointAccount(TimestampMixin, Base):
    __tablename__ = "room_point_accounts"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    persona_id: Mapped[str] = mapped_column(ForeignKey("personas.id"), unique=True, nullable=False)
    balance: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    persona: Mapped["Persona"] = relationship(back_populates="circle_point_account")


class CirclePointTransaction(Base):
    __tablename__ = "room_point_transactions"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    persona_id: Mapped[str] = mapped_column(ForeignKey("personas.id"), nullable=False, index=True)
    game_account_id: Mapped[int] = mapped_column(ForeignKey("game_accounts.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_bet_id: Mapped[int | None] = mapped_column(ForeignKey("bets.id"), nullable=True)
    related_race_result_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "race_results.id",
            name="fk_room_point_transactions_related_race_result_id_race_results",
        ),
        nullable=True,
        index=True,
    )
    created_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    related_race_result: Mapped["RaceResult | None"] = relationship()


class GameEvent(TimestampMixin, Base):
    __tablename__ = "game_events"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_scope: Mapped[str] = mapped_column(String(32), default="circle_internal", nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    created_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    deleted_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delete_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Race(TimestampMixin, Base):
    __tablename__ = "races"
    __table_args__ = (
        UniqueConstraint(
            "external_source",
            "external_race_id",
            name="uq_races_external_source_external_race_id",
        ),
        CheckConstraint(
            "race_kind <> 'room_match' OR status IN "
            "('setup', 'betting_open', 'betting_closed', 'result_review', "
            "'result_confirmed', 'settled', 'voided')",
            name="room_match_status",
        ),
        CheckConstraint("betting_window_version > 0", name="positive_betting_window_version"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("game_events.id"), nullable=True)
    external_source: Mapped[str | None] = mapped_column(String(200), nullable=True)
    external_race_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    race_kind: Mapped[str] = mapped_column(String(32), default="room_match", nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    betting_window_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    betting_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    betting_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    condition: Mapped["RaceCondition | None"] = relationship(back_populates="race")


class RaceCondition(TimestampMixin, Base):
    __tablename__ = "race_conditions"
    __table_args__ = (CheckConstraint("participant_count >= 0", name="nonnegative_participant_count"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id"), unique=True, nullable=False)
    grade: Mapped[str] = mapped_column(String(16), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    track_surface: Mapped[str] = mapped_column(String(32), nullable=False)
    distance: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    season: Mapped[str] = mapped_column(String(16), nullable=False)
    weather: Mapped[str] = mapped_column(String(32), nullable=False)
    track_condition: Mapped[str] = mapped_column(String(32), nullable=False)
    condition_label: Mapped[str] = mapped_column(String(32), nullable=False)
    participant_count: Mapped[int] = mapped_column(Integer, nullable=False)

    race: Mapped["Race"] = relationship(back_populates="condition")


class RaceEntry(TimestampMixin, Base):
    __tablename__ = "race_entries"
    __table_args__ = (
        UniqueConstraint("race_id", "entry_number"),
        UniqueConstraint("source_import_record_id"),
        Index(
            "ix_race_entries_owner_at_event_persona_id_race_id",
            "owner_at_event_persona_id",
            "race_id",
        ),
        CheckConstraint("entry_number > 0", name="positive_entry_number"),
        CheckConstraint(
            "entry_number_source IN ('declared', 'payout_result', 'synthetic')",
            name="entry_number_source",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id"), nullable=False)
    entry_number: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_kind: Mapped[str] = mapped_column(String(32), default="room_match", nullable=False)
    entry_number_source: Mapped[str] = mapped_column(
        String(32), default="declared", server_default="declared", nullable=False
    )
    source_import_record_id: Mapped[int | None] = mapped_column(ForeignKey("sheet_import_records.id"), nullable=True)
    game_account_id: Mapped[int | None] = mapped_column(ForeignKey("game_accounts.id"), nullable=True)
    owner_at_event_persona_id: Mapped[str | None] = mapped_column(ForeignKey("personas.id"), nullable=True)
    player_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    horse_name_or_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    running_style: Mapped[str | None] = mapped_column(String(32), nullable=True)
    horse_age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    total_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    second_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    third_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RaceOperationAudit(Base):
    __tablename__ = "race_operation_audits"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RaceResult(TimestampMixin, Base):
    __tablename__ = "race_results"
    __table_args__ = (
        UniqueConstraint("race_id", "entry_number"),
        UniqueConstraint("source_import_record_id"),
        CheckConstraint(
            "popularity_rank IS NULL OR popularity_rank > 0",
            name="positive_popularity_rank",
        ),
        CheckConstraint(
            "finish_time_ms IS NULL OR finish_time_ms > 0",
            name="positive_finish_time_ms",
        ),
        Index("ix_race_results_race_id_betting_excluded", "race_id", "is_betting_excluded"),
        Index(
            "ix_race_results_owner_at_event_persona_id_race_id",
            "owner_at_event_persona_id",
            "race_id",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id"), nullable=False)
    entry_number: Mapped[int] = mapped_column(Integer, nullable=False)
    game_account_id: Mapped[int | None] = mapped_column(ForeignKey("game_accounts.id"), nullable=True)
    owner_at_event_persona_id: Mapped[str | None] = mapped_column(ForeignKey("personas.id"), nullable=True)
    character_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    character_evaluation_rank: Mapped[str | None] = mapped_column(String(32), nullable=True)
    popularity_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_margin_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    converted_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_betting_excluded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    is_rating_excluded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    is_result_void: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    source_import_record_id: Mapped[int | None] = mapped_column(ForeignKey("sheet_import_records.id"), nullable=True)
    raw_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class RatingRuleVersion(Base):
    __tablename__ = "rating_rule_versions"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="positive_version_number"),
        CheckConstraint("rule_count > 0", name="positive_rule_count"),
        UniqueConstraint("version_number", name="uq_rating_rule_versions_version_number"),
        UniqueConstraint(
            "source_identifier",
            "source_checksum",
            "source_sheet_name",
            "source_range",
            name="uq_rating_rule_versions_source",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_identifier: Mapped[str] = mapped_column(String(200), nullable=False)
    source_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sheet_name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_range: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_set_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RatingRule(TimestampMixin, Base):
    __tablename__ = "rating_rules"
    __table_args__ = (
        UniqueConstraint(
            "rating_rule_version_id",
            "grade",
            "participant_count",
            "converted_rank",
            name="uq_rating_rules_version_grade_participants_rank",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    rating_rule_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("rating_rule_versions.id"), nullable=True, index=True
    )
    grade: Mapped[str] = mapped_column(String(8), nullable=False)
    participant_count: Mapped[int] = mapped_column(Integer, nullable=False)
    converted_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    base_delta: Mapped[Decimal] = mapped_column(RATING_NUMERIC, nullable=False)


class RaceRatingContext(Base):
    __tablename__ = "race_rating_contexts"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id"), unique=True, nullable=False)
    average_rating_before: Mapped[Decimal | None] = mapped_column(RATING_NUMERIC, nullable=True)
    participant_count: Mapped[int] = mapped_column(Integer, nullable=False)
    grade: Mapped[str] = mapped_column(String(8), nullable=False)
    raw_context_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RatingEvent(Base):
    __tablename__ = "rating_events"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    rating_rule_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("rating_rule_versions.id"), nullable=True, index=True
    )
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id"), nullable=False)
    race_result_id: Mapped[int] = mapped_column(ForeignKey("race_results.id"), nullable=False)
    game_account_id: Mapped[int] = mapped_column(ForeignKey("game_accounts.id"), nullable=False)
    grade: Mapped[str] = mapped_column(String(8), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    converted_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    rating_before: Mapped[Decimal] = mapped_column(RATING_NUMERIC, nullable=False)
    base_delta: Mapped[Decimal] = mapped_column(RATING_NUMERIC, nullable=False)
    adjustment_delta: Mapped[Decimal] = mapped_column(RATING_NUMERIC, default=Decimal(), nullable=False)
    total_delta: Mapped[Decimal] = mapped_column(RATING_NUMERIC, nullable=False)
    rating_after: Mapped[Decimal] = mapped_column(RATING_NUMERIC, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MatchOddsSnapshot(Base):
    __tablename__ = "room_match_odds_snapshots"
    __table_args__ = (
        CheckConstraint("settlement_participant_count > 0", name="positive_settlement_participant_count"),
        CheckConstraint("payout_multiplier IN (0.50, 1.00)", name="supported_payout_multiplier"),
        UniqueConstraint("race_id", name="uq_room_match_odds_snapshots_race"),
        UniqueConstraint("match_result_submission_id", name="uq_room_match_odds_snapshots_submission"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id"), nullable=False)
    match_result_submission_id: Mapped[int] = mapped_column(ForeignKey("match_result_submissions.id"), nullable=False)
    settlement_participant_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payout_multiplier: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    confirmed_by_discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MatchOddsSnapshotEntry(Base):
    __tablename__ = "room_match_odds_snapshot_entries"
    __table_args__ = (
        CheckConstraint("declared_payout_rate >= 0", name="nonnegative_declared_payout_rate"),
        CheckConstraint("effective_payout_rate >= 0", name="nonnegative_effective_payout_rate"),
        CheckConstraint("bet_type IN ('win', 'quinella', 'trio')", name="supported_bet_type"),
        UniqueConstraint("odds_snapshot_id", "bet_type", name="uq_room_match_odds_snapshot_entries_bet_type"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    odds_snapshot_id: Mapped[int] = mapped_column(ForeignKey("room_match_odds_snapshots.id"), nullable=False)
    bet_type: Mapped[str] = mapped_column(String(32), nullable=False)
    winning_numbers: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    declared_payout_rate: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    effective_payout_rate: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)


class Bet(TimestampMixin, Base):
    __tablename__ = "bets"
    __table_args__ = (CheckConstraint("betting_window_version > 0", name="positive_betting_window_version"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("game_events.id"), nullable=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id"), nullable=False)
    persona_id: Mapped[str] = mapped_column(ForeignKey("personas.id"), nullable=False, index=True)
    game_account_id: Mapped[int] = mapped_column(ForeignKey("game_accounts.id"), nullable=False)
    betting_mode: Mapped[str] = mapped_column(String(32), default="room_match", nullable=False)
    bet_type: Mapped[str] = mapped_column(String(32), nullable=False)
    numbers: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    betting_window_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BetJudgement(Base):
    __tablename__ = "bet_judgements"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    bet_id: Mapped[int] = mapped_column(ForeignKey("bets.id"), nullable=False)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id"), nullable=False)
    judgement_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    is_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payout_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    stake_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payout_amount: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    point_delta: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    judged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    judged_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Win5Season(TimestampMixin, Base):
    __tablename__ = "win5_seasons"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'closed', 'cancelled')",
            name="win5_season_status",
        ),
        CheckConstraint(
            "(status = 'active' AND active_marker = 'active') OR (status <> 'active' AND active_marker IS NULL)",
            name="win5_season_active_marker",
        ),
        CheckConstraint(
            "season_number > 0 AND "
            "((status = 'cancelled' AND season_number_marker IS NULL) OR "
            "(status <> 'cancelled' AND season_number_marker = season_number))",
            name="win5_season_number_claim",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    season_number: Mapped[int] = mapped_column(Integer, nullable=False)
    season_number_marker: Mapped[int | None] = mapped_column(
        Integer,
        unique=True,
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", server_default="draft", nullable=False)
    active_marker: Mapped[str | None] = mapped_column(String(16), unique=True, nullable=True)


class Win5Round(TimestampMixin, Base):
    __tablename__ = "win5_rounds"
    __table_args__ = (
        UniqueConstraint("season_id", "round_number", name="uq_win5_rounds_season_round_number"),
        UniqueConstraint("race_id", name="uq_win5_rounds_race_id"),
        CheckConstraint("round_number > 0", name="positive_win5_round_number"),
        CheckConstraint(
            "status IN ('setup', 'open', 'closed', 'result_entered', 'scored')",
            name="win5_round_status",
        ),
        CheckConstraint(
            "(round_type = 'normal' AND race_id IS NOT NULL) OR (round_type = 'special' AND race_id IS NULL)",
            name="win5_round_type_race",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("win5_seasons.id"), nullable=False)
    race_id: Mapped[int | None] = mapped_column(ForeignKey("races.id"), nullable=True)
    round_type: Mapped[str] = mapped_column(String(32), default="normal", server_default="normal", nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    round_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="setup", server_default="setup", nullable=False)
    opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Win5RoundRace(Base):
    __tablename__ = "win5_round_races"
    __table_args__ = (
        UniqueConstraint("id", "round_id", name="uq_win5_round_races_id_round"),
        UniqueConstraint("round_id", "race_id", name="uq_win5_round_races_round_race"),
        UniqueConstraint("round_id", "display_order", name="uq_win5_round_races_round_order"),
        UniqueConstraint("race_id", name="uq_win5_round_races_race_id"),
        CheckConstraint("display_order > 0", name="positive_win5_round_race_order"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("win5_rounds.id"), nullable=False)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id"), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Win5Entry(TimestampMixin, Base):
    __tablename__ = "win5_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["special_round_race_id", "round_id"],
            ["win5_round_races.id", "win5_round_races.round_id"],
            name="fk_win5_entries_special_round_race",
        ),
        UniqueConstraint("idempotency_key", name="uq_win5_entries_idempotency_key"),
        UniqueConstraint(
            "round_id",
            "game_account_id",
            "normal_accepted_marker",
            name="uq_win5_entries_normal_accepted_account_round",
        ),
        UniqueConstraint(
            "special_round_race_id",
            "game_account_id",
            "accepted_marker",
            name="uq_win5_entries_special_accepted_account_race",
        ),
        CheckConstraint(
            "prediction_tier IN ('top1', 'top3', 'top5', 'special_winner')",
            name="win5_prediction_tier",
        ),
        CheckConstraint("status IN ('accepted', 'cancelled')", name="win5_entry_status"),
        CheckConstraint(
            "(status = 'accepted' AND accepted_marker = 'accepted' "
            "AND cancelled_at IS NULL AND cancelled_by_discord_user_id IS NULL) OR "
            "(status = 'cancelled' AND accepted_marker IS NULL "
            "AND cancelled_at IS NOT NULL AND cancelled_by_discord_user_id IS NOT NULL)",
            name="win5_entry_state",
        ),
        CheckConstraint(
            "(prediction_tier IN ('top1', 'top3', 'top5') "
            "AND special_round_race_id IS NULL "
            "AND ((status = 'accepted' AND normal_accepted_marker = 'accepted') "
            "OR (status = 'cancelled' AND normal_accepted_marker IS NULL))) OR "
            "(prediction_tier = 'special_winner' "
            "AND special_round_race_id IS NOT NULL "
            "AND normal_accepted_marker IS NULL)",
            name="win5_entry_prediction_scope",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("win5_seasons.id"), nullable=False)
    round_id: Mapped[int] = mapped_column(ForeignKey("win5_rounds.id"), nullable=False)
    game_account_id: Mapped[int] = mapped_column(ForeignKey("game_accounts.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ordered_picks_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    prediction_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    special_round_race_id: Mapped[int | None] = mapped_column(BIGINT_PK, nullable=True)
    accepted_marker: Mapped[str | None] = mapped_column(String(16), nullable=True)
    normal_accepted_marker: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="accepted", server_default="accepted", nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Win5Pick(Base):
    __tablename__ = "win5_picks"
    __table_args__ = (
        UniqueConstraint("win5_entry_id", "pick_order", name="uq_win5_picks_entry_order"),
        UniqueConstraint("win5_entry_id", "entry_number", name="uq_win5_picks_entry_number"),
        CheckConstraint("pick_order > 0", name="positive_win5_pick_order"),
        CheckConstraint("entry_number > 0", name="positive_win5_entry_number"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    win5_entry_id: Mapped[int] = mapped_column(ForeignKey("win5_entries.id"), nullable=False)
    pick_order: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Win5OperationAudit(Base):
    __tablename__ = "win5_operation_audits"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("win5_seasons.id"), nullable=True)
    round_id: Mapped[int | None] = mapped_column(ForeignKey("win5_rounds.id"), nullable=True)
    win5_entry_id: Mapped[int | None] = mapped_column(ForeignKey("win5_entries.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Win5Result(TimestampMixin, Base):
    __tablename__ = "win5_results"
    __table_args__ = (
        UniqueConstraint("race_id", name="uq_win5_results_race_id"),
        Index("ix_win5_results_round_id", "round_id"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("win5_rounds.id"), nullable=False)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("game_events.id"), nullable=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("win5_seasons.id"), nullable=False)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id"), nullable=False)
    result_order: Mapped[list[int]] = mapped_column(JSON, nullable=False)


class Win5Judgement(Base):
    __tablename__ = "win5_judgements"
    __table_args__ = (
        UniqueConstraint("win5_entry_id", name="uq_win5_judgements_entry_id"),
        CheckConstraint(
            "exact_position_count >= 0 AND exact_position_count <= 5",
            name="win5_judgement_exact_count",
        ),
        CheckConstraint(
            "on_board_wrong_position_count >= 0 AND on_board_wrong_position_count <= 5",
            name="win5_judgement_board_count",
        ),
        CheckConstraint(
            "off_board_count >= 0 AND off_board_count <= 5",
            name="win5_judgement_off_board_count",
        ),
        CheckConstraint(
            "exact_position_count + on_board_wrong_position_count + off_board_count BETWEEN 1 AND 5",
            name="win5_judgement_pick_count",
        ),
        CheckConstraint(
            "prediction_tier IN ('top1', 'top3', 'top5', 'special_winner')",
            name="win5_judgement_prediction_tier",
        ),
        CheckConstraint(
            "(prediction_tier IN ('top1', 'top3', 'top5') "
            "AND season_score_delta = exact_position_count * 3 + on_board_wrong_position_count) "
            "OR (prediction_tier = 'special_winner' "
            "AND exact_position_count + off_board_count = 1 "
            "AND on_board_wrong_position_count = 0 "
            "AND season_score_delta = exact_position_count)",
            name="win5_judgement_season_score",
        ),
        CheckConstraint(
            "(prediction_tier = 'top1' "
            "AND top1_score_delta = CASE WHEN exact_position_count = 1 THEN 3 ELSE 0 END) OR "
            "(prediction_tier IN ('top3', 'top5') AND top1_score_delta = 0) OR "
            "(prediction_tier = 'special_winner' AND top1_score_delta = exact_position_count)",
            name="win5_judgement_top1_score",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    win5_entry_id: Mapped[int] = mapped_column(ForeignKey("win5_entries.id"), nullable=False)
    season_id: Mapped[int] = mapped_column(ForeignKey("win5_seasons.id"), nullable=False)
    prediction_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    exact_position_count: Mapped[int] = mapped_column(Integer, nullable=False)
    on_board_wrong_position_count: Mapped[int] = mapped_column(Integer, nullable=False)
    off_board_count: Mapped[int] = mapped_column(Integer, nullable=False)
    season_score_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    top1_score_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    judgement_detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    judged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    judged_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Win5Score(TimestampMixin, Base):
    __tablename__ = "win5_scores"
    __table_args__ = (
        UniqueConstraint("season_id", "game_account_id", name="uq_win5_scores_season_account"),
        CheckConstraint("season_score >= 0", name="nonnegative_win5_season_score"),
        CheckConstraint("top1_score >= 0", name="nonnegative_win5_top1_score"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("win5_seasons.id"), nullable=False)
    game_account_id: Mapped[int] = mapped_column(ForeignKey("game_accounts.id"), nullable=False)
    season_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    top1_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class Win5ScoreEvent(Base):
    __tablename__ = "win5_score_events"
    __table_args__ = (
        UniqueConstraint("win5_entry_id", name="uq_win5_score_events_entry_id"),
        CheckConstraint(
            "season_score_delta >= 0 AND season_score_delta <= 15",
            name="win5_score_event_season_delta",
        ),
        CheckConstraint(
            "top1_score_delta >= 0 AND top1_score_delta <= 3",
            name="win5_score_event_top1_delta",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("win5_seasons.id"), nullable=False)
    game_account_id: Mapped[int] = mapped_column(ForeignKey("game_accounts.id"), nullable=False)
    win5_entry_id: Mapped[int] = mapped_column(ForeignKey("win5_entries.id"), nullable=False)
    season_score_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    top1_score_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MatchResultSubmission(Base):
    __tablename__ = "match_result_submissions"
    __table_args__ = (
        UniqueConstraint(
            "race_id",
            "revision_number",
            name="uq_match_result_submissions_race_revision",
        ),
        UniqueConstraint(
            "race_id",
            "current_marker",
            name="uq_match_result_submissions_race_current",
        ),
        UniqueConstraint(
            "supersedes_submission_id",
            name="uq_match_result_submissions_supersedes",
        ),
        CheckConstraint(
            "revision_number > 0",
            name="wu10_positive_revision",
        ),
        CheckConstraint(
            "submission_status IN ('pending_review', 'reviewed', 'superseded', 'rejected', 'confirmed')",
            name="wu10_submission_status",
        ),
        CheckConstraint(
            "(reviewed_at IS NULL AND reviewed_by_discord_user_id IS NULL) OR "
            "(reviewed_at IS NOT NULL AND reviewed_by_discord_user_id IS NOT NULL)",
            name="wu10_review_metadata",
        ),
        CheckConstraint(
            "(rejected_at IS NULL AND rejected_by_discord_user_id IS NULL AND rejection_reason IS NULL) OR "
            "(rejected_at IS NOT NULL AND rejected_by_discord_user_id IS NOT NULL AND rejection_reason IS NOT NULL)",
            name="wu10_rejection_metadata",
        ),
        CheckConstraint(
            "(confirmed_at IS NULL AND confirmed_by_discord_user_id IS NULL) OR "
            "(confirmed_at IS NOT NULL AND confirmed_by_discord_user_id IS NOT NULL)",
            name="wu10_confirmation_metadata",
        ),
        CheckConstraint(
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
            name="wu10_submission_state",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("game_events.id"), nullable=True)
    race_id: Mapped[int | None] = mapped_column(ForeignKey("races.id"), nullable=True)
    match_type: Mapped[str] = mapped_column(String(32), nullable=False)
    submitted_by_discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    submission_status: Mapped[str] = mapped_column(
        String(32),
        default="pending_review",
        server_default="pending_review",
        nullable=False,
    )
    raw_input_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_submission_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "match_result_submissions.id",
            name="fk_match_result_submissions_supersedes_submission_id",
        ),
        nullable=True,
    )
    current_marker: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reviewed_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MatchResultProvenance(Base):
    __tablename__ = "room_match_result_provenance"
    __table_args__ = (
        UniqueConstraint(
            "race_result_id",
            name="uq_room_match_result_provenance_race_result",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    race_result_id: Mapped[int] = mapped_column(
        ForeignKey(
            "race_results.id",
            name="fk_rm_result_provenance_result",
        ),
        nullable=False,
    )
    match_result_submission_id: Mapped[int] = mapped_column(
        ForeignKey(
            "match_result_submissions.id",
            name="fk_rm_result_provenance_submission",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MatchResultPublication(TimestampMixin, Base):
    __tablename__ = "room_match_result_publications"
    __table_args__ = (
        UniqueConstraint(
            "race_id",
            name="uq_room_match_result_publications_race",
        ),
        UniqueConstraint(
            "match_result_submission_id",
            name="uq_room_match_result_publications_submission",
        ),
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'delivery_unknown')",
            name="wu10_publication_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="wu10_publication_attempt_count",
        ),
        CheckConstraint(
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

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    race_id: Mapped[int] = mapped_column(
        ForeignKey(
            "races.id",
            name="fk_rm_result_publication_race",
        ),
        nullable=False,
    )
    match_result_submission_id: Mapped[int] = mapped_column(
        ForeignKey(
            "match_result_submissions.id",
            name="fk_rm_result_publication_submission",
        ),
        nullable=False,
    )
    target_channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    discord_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SheetImportRun(Base):
    __tablename__ = "sheet_import_runs"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    import_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_identifier: Mapped[str] = mapped_column(String(200), nullable=False)
    source_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class SheetImportRecord(Base):
    __tablename__ = "sheet_import_records"
    __table_args__ = (
        UniqueConstraint("import_run_id", "source_sheet_name", "source_row_number"),
        CheckConstraint("source_row_number > 0", name="positive_source_row"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    import_run_id: Mapped[int] = mapped_column(ForeignKey("sheet_import_runs.id"), nullable=False)
    source_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    row_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sheet_name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    record_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    target_entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_entity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SheetExportRun(Base):
    __tablename__ = "sheet_export_runs"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    target_spreadsheet_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class ReportSnapshot(Base):
    __tablename__ = "report_snapshots"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    report_type: Mapped[str] = mapped_column(String(64), nullable=False)
    period_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    generated_by_discord_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ExportRun(Base):
    __tablename__ = "export_runs"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    report_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("report_snapshots.id"), nullable=True)
    export_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
