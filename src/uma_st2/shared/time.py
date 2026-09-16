"""Generic timezone-aware UTC normalization primitives."""

from __future__ import annotations

from datetime import UTC, datetime


def normalize_utc_datetime(value: datetime, *, field_name: str = "datetime") -> datetime:
    """Require an aware datetime and return the same instant in UTC."""

    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)


def utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""

    return datetime.now(UTC)
