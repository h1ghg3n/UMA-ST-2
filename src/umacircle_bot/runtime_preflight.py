from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine, text

from umacircle_bot.config import Settings

EXPECTED_ALEMBIC_HEAD = "20260815_0030"
EXPECTED_ROOM_POINT_SCALE = 10


@dataclass(frozen=True, slots=True)
class RuntimeDatabaseDiagnostics:
    dialect: str
    alembic_revision: str
    room_point_scale: int


def verify_runtime_database(settings: Settings) -> RuntimeDatabaseDiagnostics:
    """Prove connectivity and the exact deployed schema without logging credentials."""
    engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
            if revision != EXPECTED_ALEMBIC_HEAD:
                raise RuntimeError(
                    f"database Alembic revision must be {EXPECTED_ALEMBIC_HEAD}; found {revision or 'none'}"
                )
            scale = connection.execute(
                text("SELECT scale_version FROM room_point_scale_state WHERE id = 1")
            ).scalar_one_or_none()
            if scale != EXPECTED_ROOM_POINT_SCALE:
                raise RuntimeError(
                    f"database Circle Point scale must be {EXPECTED_ROOM_POINT_SCALE}; found {scale or 'none'}"
                )
            return RuntimeDatabaseDiagnostics(
                dialect=connection.dialect.name,
                alembic_revision=str(revision),
                room_point_scale=int(scale),
            )
    finally:
        engine.dispose()
