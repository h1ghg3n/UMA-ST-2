"""Common operation and audit-extension ORM mappings."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .schema_types import PERSONA_ID


class OperationORM(Base):
    __tablename__ = "operations"
    __table_args__ = (
        Index(None, "guild_id"),
        Index(None, "correlation_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    guild_id: Mapped[str | None] = mapped_column(String(32))
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    actor_discord_user_id: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class IdentityOperationORM(Base):
    __tablename__ = "identity_operations"

    operation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("operations.id"), primary_key=True)
    persona_id: Mapped[str | None] = mapped_column(PERSONA_ID)
    game_account_id: Mapped[int | None] = mapped_column(BigInteger)
    discord_user_id: Mapped[str | None] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    before_data: Mapped[Any | None] = mapped_column(JSON)
    after_data: Mapped[Any | None] = mapped_column(JSON)


class MatchOperationORM(Base):
    __tablename__ = "match_operations"
    __table_args__ = (Index(None, "match_id"),)

    operation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("operations.id"), primary_key=True)
    match_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    before_data: Mapped[Any | None] = mapped_column(JSON)
    after_data: Mapped[Any | None] = mapped_column(JSON)


class BetOperationORM(Base):
    __tablename__ = "bet_operations"
    __table_args__ = (Index(None, "match_id"), Index(None, "bet_id"))

    operation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("operations.id"), primary_key=True)
    match_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bet_id: Mapped[int | None] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    before_data: Mapped[Any | None] = mapped_column(JSON)
    after_data: Mapped[Any | None] = mapped_column(JSON)


class Win5OperationORM(Base):
    __tablename__ = "win5_operations"
    __table_args__ = (
        Index(None, "season_id"),
        Index(None, "round_id"),
        Index(None, "submission_id"),
    )

    operation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("operations.id"), primary_key=True)
    season_id: Mapped[int | None] = mapped_column(BigInteger)
    round_id: Mapped[int | None] = mapped_column(BigInteger)
    submission_id: Mapped[int | None] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    before_data: Mapped[Any | None] = mapped_column(JSON)
    after_data: Mapped[Any | None] = mapped_column(JSON)


class SettingsOperationORM(Base):
    __tablename__ = "settings_operations"
    __table_args__ = (Index(None, "guild_id"),)

    operation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("operations.id"), primary_key=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    before_data: Mapped[Any | None] = mapped_column(JSON)
    after_data: Mapped[Any | None] = mapped_column(JSON)
