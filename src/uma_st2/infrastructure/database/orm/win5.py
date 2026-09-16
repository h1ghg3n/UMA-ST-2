"""WIN5 ORM mappings."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .schema_types import (
    PERSONA_ID,
    WIN5_JUDGEMENT_OUTCOME,
    WIN5_ROUND_SOURCE_KIND,
    WIN5_ROUND_STATUS,
    WIN5_ROUND_TYPE,
    WIN5_SEASON_STATUS,
    WIN5_SUBMISSION_STATUS,
    WIN5_SUBMISSION_TIER,
)


class Win5SeasonORM(Base):
    __tablename__ = "win5_seasons"
    __table_args__ = (
        UniqueConstraint("active_marker"),
        CheckConstraint("active_marker IS NULL OR active_marker = 1", name="active_marker_is_true_or_null"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(WIN5_SEASON_STATUS, nullable=False)
    active_marker: Mapped[bool | None] = mapped_column(Boolean)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Win5RoundORM(Base):
    __tablename__ = "win5_rounds"
    __table_args__ = (Index("ix_win5_rounds_source_kind_status", "source_kind", "status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("win5_seasons.id"), nullable=False)
    source_kind: Mapped[str] = mapped_column(
        WIN5_ROUND_SOURCE_KIND,
        default="native_v2",
        server_default=text("'native_v2'"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(WIN5_ROUND_TYPE, nullable=False)
    status: Mapped[str] = mapped_column(WIN5_ROUND_STATUS, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    opens_at: Mapped[datetime | None] = mapped_column(DateTime)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Win5RaceORM(Base):
    __tablename__ = "win5_races"
    __table_args__ = (
        CheckConstraint(
            "(void_reason IS NULL AND voided_at IS NULL) OR (void_reason IS NOT NULL AND voided_at IS NOT NULL)",
            name="void_fact_complete",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("win5_rounds.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    void_reason: Mapped[str | None] = mapped_column(String(255))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Win5RaceEntryORM(Base):
    __tablename__ = "win5_race_entries"
    __table_args__ = (
        UniqueConstraint("race_id", "gate_number"),
        UniqueConstraint("id", "race_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    race_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("win5_races.id"), nullable=False)
    gate_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Win5ResultORM(Base):
    __tablename__ = "win5_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ("race_entry_id", "race_id"),
            ("win5_race_entries.id", "win5_race_entries.race_id"),
        ),
        UniqueConstraint("race_id", "position"),
        UniqueConstraint("race_id", "race_entry_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    race_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("win5_races.id"), nullable=False)
    race_entry_id: Mapped[int | None] = mapped_column(BigInteger)
    gate_number: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Win5SubmissionORM(Base):
    __tablename__ = "win5_submissions"
    __table_args__ = (
        UniqueConstraint("round_id", "persona_id", "active_marker"),
        CheckConstraint("active_marker IS NULL OR active_marker = 1", name="active_marker_is_true_or_null"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("win5_rounds.id"), nullable=False)
    persona_id: Mapped[str] = mapped_column(PERSONA_ID, ForeignKey("personas.id"), nullable=False)
    tier: Mapped[str] = mapped_column(WIN5_SUBMISSION_TIER, nullable=False)
    status: Mapped[str] = mapped_column(WIN5_SUBMISSION_STATUS, nullable=False)
    active_marker: Mapped[bool | None] = mapped_column(Boolean)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Win5SubmissionPickORM(Base):
    __tablename__ = "win5_submission_picks"
    __table_args__ = (
        ForeignKeyConstraint(
            ("race_entry_id", "race_id"),
            ("win5_race_entries.id", "win5_race_entries.race_id"),
        ),
        UniqueConstraint("submission_id", "race_id", "position"),
        UniqueConstraint("submission_id", "race_id", "race_entry_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("win5_submissions.id"), nullable=False)
    race_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("win5_races.id"), nullable=False)
    race_entry_id: Mapped[int | None] = mapped_column(BigInteger)
    gate_number: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class Win5ScoreEventORM(Base):
    __tablename__ = "win5_score_events"
    __table_args__ = (
        UniqueConstraint("submission_id"),
        UniqueConstraint("round_id", "persona_id"),
        Index("ix_win5_score_events_operation_id", "operation_id"),
        Index("ix_win5_score_events_season_id", "season_id"),
        Index("ix_win5_score_events_round_id", "round_id"),
        Index("ix_win5_score_events_persona_id", "persona_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("operations.id", ondelete="SET NULL"),
    )
    season_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("win5_seasons.id"), nullable=False)
    round_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("win5_rounds.id"), nullable=False)
    race_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("win5_races.id"))
    submission_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("win5_submissions.id"),
        nullable=False,
    )
    submission_version: Mapped[int] = mapped_column(Integer, nullable=False)
    persona_id: Mapped[str] = mapped_column(PERSONA_ID, ForeignKey("personas.id"), nullable=False)
    tier: Mapped[str] = mapped_column(WIN5_SUBMISSION_TIER, nullable=False)
    result_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    scoring_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    reward_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    exact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    wrong_position_count: Mapped[int] = mapped_column(Integer, nullable=False)
    off_board_count: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False)
    season_score_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    top1_score_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    circle_point_reward: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Win5ScoreEventItemORM(Base):
    __tablename__ = "win5_score_event_items"
    __table_args__ = (
        UniqueConstraint("score_event_id", "race_id", "position"),
        UniqueConstraint("submission_pick_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    score_event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("win5_score_events.id"),
        nullable=False,
    )
    race_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("win5_races.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    submission_pick_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("win5_submission_picks.id"),
    )
    matched_result_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("win5_results.id"),
    )
    outcome: Mapped[str] = mapped_column(WIN5_JUDGEMENT_OUTCOME, nullable=False)
    season_score_delta: Mapped[int] = mapped_column(Integer, nullable=False)


class Win5ScoreORM(Base):
    __tablename__ = "win5_scores"

    season_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("win5_seasons.id"), primary_key=True)
    persona_id: Mapped[str] = mapped_column(PERSONA_ID, ForeignKey("personas.id"), primary_key=True)
    season_score: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    top1_score: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
