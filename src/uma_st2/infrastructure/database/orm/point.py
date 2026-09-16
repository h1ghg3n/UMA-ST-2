"""Circle Point ORM mappings."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .schema_types import PERSONA_ID


class CirclePointORM(Base):
    __tablename__ = "circle_points"

    persona_id: Mapped[str] = mapped_column(PERSONA_ID, ForeignKey("personas.id"), primary_key=True)
    balance: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PointTransactionORM(Base):
    __tablename__ = "point_transactions"
    __table_args__ = (Index(None, "persona_id"), Index(None, "operation_id"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    persona_id: Mapped[str] = mapped_column(PERSONA_ID, ForeignKey("personas.id"), nullable=False)
    operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("operations.id", ondelete="SET NULL"),
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
