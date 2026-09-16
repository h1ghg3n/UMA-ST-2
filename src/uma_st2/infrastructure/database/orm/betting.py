"""Bet ORM mappings."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .schema_types import BET_STATUS, BET_TYPE, PERSONA_ID


class BetORM(Base):
    __tablename__ = "bets"
    __table_args__ = (
        UniqueConstraint("match_id", "persona_id", "type", "selection_fingerprint", "active_marker"),
        CheckConstraint("active_marker IS NULL OR active_marker = 1", name="active_marker_is_true_or_null"),
        Index(None, "match_id"),
        Index(None, "persona_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("matches.id"), nullable=False)
    persona_id: Mapped[str] = mapped_column(PERSONA_ID, ForeignKey("personas.id"), nullable=False)
    type: Mapped[str] = mapped_column(BET_TYPE, nullable=False)
    selections: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    selection_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(BET_STATUS, nullable=False)
    active_marker: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
