from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import GameEvent
from umacircle_bot.domain.errors import BettingRuleError
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.services.dtos import GameEventDTO

EVENT_SCOPES = frozenset({"official", "circle_internal"})


def create_game_event(
    session: Session,
    *,
    name: str,
    event_type: str,
    event_scope: str = "circle_internal",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    created_by_discord_user_id: str | None = None,
) -> GameEventDTO:
    normalized_name = _normalize_text(name, field_name="event name", maximum=200)
    normalized_type = _normalize_text(event_type, field_name="event type", maximum=32)
    normalized_scope = _normalize_event_scope(event_scope)
    normalized_starts_at = _normalize_datetime(starts_at, field_name="event start time")
    normalized_ends_at = _normalize_datetime(ends_at, field_name="event end time")
    normalized_actor = (
        _normalize_text(created_by_discord_user_id, field_name="Discord user ID", maximum=32)
        if created_by_discord_user_id is not None
        else None
    )
    if (
        normalized_starts_at is not None
        and normalized_ends_at is not None
        and normalized_ends_at <= normalized_starts_at
    ):
        raise BettingRuleError("event end time must be after its start time")
    event = GameEvent(
        name=normalized_name,
        event_type=normalized_type,
        event_scope=normalized_scope,
        starts_at=normalized_starts_at,
        ends_at=normalized_ends_at,
        status="scheduled",
        created_by_discord_user_id=normalized_actor,
    )
    session.add(event)
    session.flush()
    return _event_dto(event)


def list_scheduled_game_events(
    session: Session,
    *,
    limit: int = 20,
    now: datetime | None = None,
) -> tuple[GameEventDTO, ...]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("event list limit must be between 1 and 100")
    current_time = _normalize_datetime(now, field_name="current time") if now is not None else datetime.now(UTC)
    events = session.scalars(
        select(GameEvent)
        .where(
            GameEvent.status == "scheduled",
            or_(GameEvent.ends_at.is_(None), GameEvent.ends_at > current_time),
        )
        .order_by(GameEvent.starts_at.is_(None), GameEvent.starts_at, GameEvent.id)
        .limit(limit)
    ).all()
    return tuple(_event_dto(event) for event in events)


def _event_dto(event: GameEvent) -> GameEventDTO:
    return GameEventDTO(
        event.id,
        event.name,
        event.event_type,
        event.event_scope,
        database_datetime_as_utc(event.starts_at) if event.starts_at is not None else None,
        database_datetime_as_utc(event.ends_at) if event.ends_at is not None else None,
        event.status,
        event.created_by_discord_user_id,
        database_datetime_as_utc(event.created_at),
        database_datetime_as_utc(event.updated_at),
    )


def _normalize_text(value: str, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise BettingRuleError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise BettingRuleError(f"{field_name} must contain 1 to {maximum} printable characters")
    return normalized


def _normalize_event_scope(value: str) -> str:
    normalized = _normalize_text(value, field_name="event scope", maximum=32).lower()
    if normalized not in EVENT_SCOPES:
        raise BettingRuleError("event scope must be official or circle_internal")
    return normalized


def _normalize_datetime(value: datetime | None, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BettingRuleError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)
