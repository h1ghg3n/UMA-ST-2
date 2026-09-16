"""Discord settings and publication ORM mappings."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .schema_types import MATCH_ODDS_REFRESH_MODE, PUBLICATION_STATUS


class BotGuildSettingORM(Base):
    __tablename__ = "bot_guild_settings"

    guild_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    win5_announcement_channel_id: Mapped[str | None] = mapped_column(String(32))
    match_announcement_channel_id: Mapped[str | None] = mapped_column(String(32))
    log_channel_id: Mapped[str | None] = mapped_column(String(32))
    operator_role_id: Mapped[str | None] = mapped_column(String(32))
    bot_manager_role_id: Mapped[str | None] = mapped_column(String(32))
    default_timezone: Mapped[str] = mapped_column(String(32), nullable=False)
    win5_announcements_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    match_announcements_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    match_odds_refresh_mode: Mapped[str] = mapped_column(
        MATCH_ODDS_REFRESH_MODE,
        nullable=False,
        server_default=text("'normal'"),
    )
    match_odds_refresh_next_at: Mapped[datetime | None] = mapped_column(DateTime)
    match_odds_refresh_sequence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    match_odds_last_projection_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DiscordPublicationORM(Base):
    __tablename__ = "discord_publications"
    __table_args__ = (
        UniqueConstraint("guild_id", "destination_kind", "event_key"),
        Index(None, "status", "attempt_started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    destination_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_channel_id: Mapped[str | None] = mapped_column(String(32))
    payload_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(PUBLICATION_STATUS, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    discord_message_id: Mapped[str | None] = mapped_column(String(32))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    failure_stage: Mapped[str | None] = mapped_column(String(16))
    attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
