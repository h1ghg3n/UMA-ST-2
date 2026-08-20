from datetime import UTC, datetime, timedelta, timezone


def source_datetime_to_utc(value: datetime, *, utc_offset_minutes: int) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("source datetime must be a datetime")
    offset = normalize_utc_offset_minutes(utc_offset_minutes)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone(timedelta(minutes=offset)))
    return value.astimezone(UTC)


def database_datetime_as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("database datetime must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def normalize_utc_offset_minutes(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not -840 <= value <= 840:
        raise ValueError("UTC offset minutes must be an integer between -840 and 840")
    return value
