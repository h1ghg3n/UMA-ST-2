"""GameAccount Rating ORM mappings."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RatingORM(Base):
    __tablename__ = "ratings"

    game_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("game_accounts.id"), primary_key=True)
    rating: Mapped[Decimal] = mapped_column(Numeric(30, 18), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class RatingRuleVersionORM(Base):
    __tablename__ = "rating_rule_versions"
    __table_args__ = (
        UniqueConstraint("version_number"),
        UniqueConstraint("source_identifier", "source_checksum", "source_sheet_name", "source_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_identifier: Mapped[str] = mapped_column(String(200), nullable=False)
    source_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sheet_name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_range: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_set_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class RatingRuleORM(Base):
    __tablename__ = "rating_rules"
    __table_args__ = (
        UniqueConstraint("rating_rule_version_id", "grade", "participant_count", "converted_rank"),
        Index(None, "rating_rule_version_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rating_rule_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("rating_rule_versions.id"),
        nullable=False,
    )
    grade: Mapped[str] = mapped_column(String(8), nullable=False)
    participant_count: Mapped[int] = mapped_column(Integer, nullable=False)
    converted_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    base_delta: Mapped[Decimal] = mapped_column(Numeric(30, 18), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class RatingTransactionORM(Base):
    __tablename__ = "rating_transactions"
    __table_args__ = (
        CheckConstraint("rating_after = rating_before + amount", name="balanced_rating_delta"),
        Index(None, "match_entry_id"),
        Index(None, "operation_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("operations.id", ondelete="SET NULL"),
    )
    rating_rule_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("rating_rule_versions.id"),
        nullable=False,
        index=True,
    )
    match_entry_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("match_entries.id"), nullable=False)
    rating_before: Mapped[Decimal] = mapped_column(Numeric(30, 18), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(30, 18), nullable=False)
    rating_after: Mapped[Decimal] = mapped_column(Numeric(30, 18), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
