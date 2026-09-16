"""Umamusume and stadium master-data ORM mappings."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .schema_types import MATCH_DIRECTION, MATCH_SURFACE, STADIUM_COURSE_LAYOUT


class UmamusumeORM(Base):
    __tablename__ = "umamusumes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    external_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    name_jp: Mapped[str] = mapped_column(String(100), nullable=False)
    name_ko: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class UmamusumeVariantORM(Base):
    __tablename__ = "umamusume_variants"
    __table_args__ = (UniqueConstraint("id", "umamusume_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    umamusume_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("umamusumes.id"), nullable=False)
    external_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    name_jp: Mapped[str] = mapped_column(String(100), nullable=False)
    name_ko: Mapped[str | None] = mapped_column(String(100))
    release_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class StadiumORM(Base):
    __tablename__ = "stadiums"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    external_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    name_jp: Mapped[str] = mapped_column(String(100), nullable=False)
    name_ko: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class StadiumCourseORM(Base):
    __tablename__ = "stadium_courses"
    __table_args__ = (UniqueConstraint("stadium_id", "surface", "distance", "direction", "layout"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stadium_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stadiums.id"), nullable=False)
    external_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    surface: Mapped[str] = mapped_column(MATCH_SURFACE, nullable=False)
    distance: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[str] = mapped_column(MATCH_DIRECTION, nullable=False)
    layout: Mapped[str] = mapped_column(STADIUM_COURSE_LAYOUT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
