"""Identity ORM mappings."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .schema_types import GAME_REGION, PERSONA_ID, PERSONA_STATUS, REGISTRATION_REQUEST_STATUS


class PersonaORM(Base):
    __tablename__ = "personas"

    id: Mapped[str] = mapped_column(PERSONA_ID, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(PERSONA_STATUS, nullable=False, server_default=text("'normal'"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DiscordAccountORM(Base):
    __tablename__ = "discord_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    persona_id: Mapped[str | None] = mapped_column(PERSONA_ID, ForeignKey("personas.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class GameAccountORM(Base):
    __tablename__ = "game_accounts"
    __table_args__ = (UniqueConstraint("game_region", "uma_pid"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    persona_id: Mapped[str] = mapped_column(PERSONA_ID, ForeignKey("personas.id"), nullable=False)
    game_region: Mapped[str] = mapped_column(GAME_REGION, nullable=False)
    uma_pid: Mapped[str | None] = mapped_column(String(32))
    nickname: Mapped[str] = mapped_column(String(100), nullable=False)
    affiliation: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class GameAccountRegistrationRequestORM(Base):
    __tablename__ = "game_account_registration_requests"
    __table_args__ = (
        UniqueConstraint("guild_id", "requester_discord_user_id", "active_marker"),
        CheckConstraint("active_marker IS NULL OR active_marker = 1", name="active_marker_valid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    requester_discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    discord_display_name_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    game_region: Mapped[str] = mapped_column(GAME_REGION, nullable=False)
    uma_pid: Mapped[str] = mapped_column(String(32), nullable=False)
    nickname: Mapped[str] = mapped_column(String(100), nullable=False)
    affiliation: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(REGISTRATION_REQUEST_STATUS, nullable=False)
    active_marker: Mapped[bool | None] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
