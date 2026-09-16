"""UTC-aware application to MariaDB DATETIME representation translation."""

from __future__ import annotations

from datetime import UTC, datetime

from uma_st2.shared import normalize_utc_datetime


def to_database_utc(value: datetime, *, field_name: str = "datetime") -> datetime:
    """Encode an aware instant as the UTC-naive value stored in DATETIME."""

    return normalize_utc_datetime(value, field_name=field_name).replace(tzinfo=None)


def from_database_utc(value: datetime, *, field_name: str = "database datetime") -> datetime:
    """Decode a DATETIME value whose stored calendar fields are defined as UTC."""

    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime.")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return normalize_utc_datetime(value, field_name=field_name)
