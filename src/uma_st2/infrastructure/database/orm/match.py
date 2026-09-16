"""Circle Match ORM mappings."""

from __future__ import annotations

from datetime import datetime
from typing import Any

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
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .schema_types import (
    MATCH_GRADE,
    MATCH_RATING_DISPOSITION,
    MATCH_RESULT_SOURCE_KIND,
    MATCH_RESULT_SUBMISSION_STATUS,
    MATCH_SEASON,
    MATCH_SOURCE_KIND,
    MATCH_STATUS,
    MATCH_TIME_OF_DAY,
    MATCH_TRACK_CONDITION,
    MATCH_WEATHER,
    PERSONA_ID,
)


class MatchORM(Base):
    __tablename__ = "matches"
    __table_args__ = (
        Index(None, "scheduled_at"),
        Index(None, "status"),
        Index(None, "source_kind", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(
        MATCH_SOURCE_KIND,
        server_default=text("'native_v2'"),
        nullable=False,
    )
    grade: Mapped[str] = mapped_column(MATCH_GRADE, nullable=False)
    stadium_course_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stadium_courses.id"), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(MATCH_STATUS, nullable=False)
    terminal_reason: Mapped[str | None] = mapped_column(String(255))
    finish_time_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MatchConditionORM(Base):
    __tablename__ = "match_conditions"

    match_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("matches.id"), primary_key=True)
    season: Mapped[str] = mapped_column(MATCH_SEASON, nullable=False)
    weather: Mapped[str] = mapped_column(MATCH_WEATHER, nullable=False)
    time_of_day: Mapped[str] = mapped_column(MATCH_TIME_OF_DAY, nullable=False)
    track_condition: Mapped[str] = mapped_column(MATCH_TRACK_CONDITION, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MatchEntryORM(Base):
    __tablename__ = "match_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ("umamusume_variant_id", "umamusume_id"),
            ("umamusume_variants.id", "umamusume_variants.umamusume_id"),
        ),
        UniqueConstraint("match_id", "entry_number"),
        Index(None, "match_id", "game_account_id"),
        UniqueConstraint("match_id", "rank"),
        UniqueConstraint("match_id", "popularity_rank"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("matches.id"), nullable=False)
    game_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("game_accounts.id"), nullable=False)
    owner_at_event_persona_id: Mapped[str] = mapped_column(
        PERSONA_ID,
        ForeignKey("personas.id"),
        nullable=False,
    )
    affiliation_at_event: Mapped[str | None] = mapped_column(String(100))
    umamusume_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("umamusumes.id"), nullable=False)
    umamusume_variant_id: Mapped[int | None] = mapped_column(BigInteger)
    entry_number: Mapped[int] = mapped_column(Integer, nullable=False)
    running_style: Mapped[str | None] = mapped_column(String(32))
    training_grade: Mapped[str | None] = mapped_column(String(16))
    rank: Mapped[int | None] = mapped_column(Integer)
    popularity_rank: Mapped[int | None] = mapped_column(Integer)
    margin: Mapped[str | None] = mapped_column(String(16))
    rating_disposition: Mapped[str | None] = mapped_column(MATCH_RATING_DISPOSITION)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MatchResultSubmissionORM(Base):
    __tablename__ = "match_result_submissions"
    __table_args__ = (
        UniqueConstraint("match_id", "revision_number"),
        UniqueConstraint("match_id", "pending_marker"),
        UniqueConstraint("match_id", "confirmed_marker"),
        CheckConstraint("pending_marker IS NULL OR pending_marker = 1", name="pending_marker_is_true_or_null"),
        CheckConstraint(
            "confirmed_marker IS NULL OR confirmed_marker = 1",
            name="confirmed_marker_is_true_or_null",
        ),
        Index(None, "match_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("matches.id"), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_kind: Mapped[str] = mapped_column(MATCH_RESULT_SOURCE_KIND, nullable=False)
    status: Mapped[str] = mapped_column(MATCH_RESULT_SUBMISSION_STATUS, nullable=False)
    pending_marker: Mapped[bool | None] = mapped_column(Boolean)
    confirmed_marker: Mapped[bool | None] = mapped_column(Boolean)
    candidate_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    submitted_operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("operations.id", ondelete="SET NULL"),
        unique=True,
    )
    rejected_operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("operations.id", ondelete="SET NULL"),
        unique=True,
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime)
    rejection_reason: Mapped[str | None] = mapped_column(String(255))
    confirmed_operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("operations.id", ondelete="SET NULL"),
        unique=True,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
