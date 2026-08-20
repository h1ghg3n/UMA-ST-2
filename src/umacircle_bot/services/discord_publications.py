from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import DiscordPublication, DiscordPublicationAudit
from umacircle_bot.domain.discord_publications import (
    DiscordPublicationDestination,
    DiscordPublicationStatus,
    validate_safe_publication_payload,
)
from umacircle_bot.domain.errors import (
    DiscordPublicationConflictError,
    DiscordPublicationError,
)
from umacircle_bot.domain.time import database_datetime_as_utc


@dataclass(frozen=True, slots=True)
class EnqueueDiscordPublicationCommand:
    guild_id: str
    destination_kind: str
    event_type: str
    event_key: str
    source_kind: str
    source_id: int
    payload: dict[str, object]
    enabled: bool
    configured_channel_id: str | None
    actor_discord_user_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class BindDiscordPublicationChannelCommand:
    publication_id: int
    target_channel_id: str
    actor_discord_user_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class MarkDiscordPublicationChannelFailedCommand:
    publication_id: int
    error_code: str
    actor_discord_user_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ClaimDiscordPublicationCommand:
    publication_id: int
    actor_discord_user_id: str
    idempotency_key: str
    authorize_delivery_unknown_retry: bool = False


@dataclass(frozen=True, slots=True)
class MarkStalePendingUnknownCommand:
    publication_id: int
    stale_before: datetime
    actor_discord_user_id: str
    idempotency_key: str
    error_code: str = "STALE_PENDING"


@dataclass(frozen=True, slots=True)
class RecordDiscordPublicationOutcomeCommand:
    publication_id: int
    outcome: str
    actor_discord_user_id: str
    idempotency_key: str
    discord_message_id: str | None = None
    error_code: str | None = None
    reconcile_delivery_unknown: bool = False


@dataclass(frozen=True, slots=True)
class DiscordPublicationDTO:
    id: int
    guild_id: str
    destination_kind: str
    event_type: str
    event_key: str
    source_kind: str
    source_id: int
    target_channel_id: str | None
    payload_json: str
    payload_fingerprint: str
    status: str
    attempt_count: int
    discord_message_id: str | None
    last_error_code: str | None
    failure_stage: str | None
    attempt_started_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


def get_discord_publication(
    session: Session,
    *,
    publication_id: int,
) -> DiscordPublicationDTO:
    identifier = _positive_int(publication_id, field="publication ID")
    row = session.get(DiscordPublication, identifier)
    if row is None:
        raise DiscordPublicationError("Discord publication not found")
    return _dto(row)


def get_discord_publication_by_event(
    session: Session,
    *,
    guild_id: str,
    destination_kind: str,
    event_key: str,
) -> DiscordPublicationDTO | None:
    guild = _snowflake(guild_id, field="guild ID")
    destination = _enum(
        destination_kind,
        DiscordPublicationDestination,
        field="destination kind",
    )
    key = _text(event_key, field="event key", maximum=128)
    row = _publication_by_event(
        session,
        guild_id=guild,
        destination=destination.value,
        event_key=key,
        lock=False,
    )
    return _dto(row) if row is not None else None


def enqueue_discord_publication(
    session: Session,
    *,
    command: EnqueueDiscordPublicationCommand,
) -> DiscordPublicationDTO:
    guild_id = _snowflake(command.guild_id, field="guild ID")
    destination = _enum(command.destination_kind, DiscordPublicationDestination, field="destination kind")
    event_type = _text(command.event_type, field="event type", maximum=64)
    event_key = _text(command.event_key, field="event key", maximum=128)
    source_kind = _text(command.source_kind, field="source kind", maximum=32)
    source_id = _positive_int(command.source_id, field="source ID")
    payload = validate_safe_publication_payload(
        command.payload,
        destination_kind=destination,
    )
    if not isinstance(command.enabled, bool):
        raise DiscordPublicationError("publication enabled flag must be boolean")
    channel_id = (
        _snowflake(command.configured_channel_id, field="configured channel ID")
        if command.configured_channel_id is not None
        else None
    )
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    payload_fingerprint = _fingerprint(payload)
    request_fingerprint = _fingerprint(
        {
            "action": "enqueue",
            "guild_id": guild_id,
            "destination": destination.value,
            "event_type": event_type,
            "event_key": event_key,
            "source_kind": source_kind,
            "source_id": source_id,
            "payload_fingerprint": payload_fingerprint,
            "enabled": command.enabled,
            "channel_id": channel_id,
            "actor": actor,
        }
    )
    _ensure_transaction(session)
    try:
        with session.begin_nested():
            audit = _audit_by_key(session, key)
            if audit is not None:
                return _audit_retry(audit, "enqueue", request_fingerprint)
            existing = _publication_by_event(
                session,
                guild_id=guild_id,
                destination=destination.value,
                event_key=event_key,
                lock=False,
            )
            status = (
                DiscordPublicationStatus.SUPPRESSED
                if not command.enabled
                else DiscordPublicationStatus.READY
                if channel_id is not None
                else DiscordPublicationStatus.AWAITING_CHANNEL
            )
            created = False
            if existing is None:
                created = _insert_publication_if_absent(
                    session,
                    values={
                        "guild_id": guild_id,
                        "destination_kind": destination.value,
                        "event_type": event_type,
                        "event_key": event_key,
                        "source_kind": source_kind,
                        "source_id": source_id,
                        "target_channel_id": channel_id if command.enabled else None,
                        "payload_json": payload,
                        "payload_fingerprint": payload_fingerprint,
                        "status": status.value,
                        "attempt_count": 0,
                    },
                )
            existing = _publication_by_event(
                session,
                guild_id=guild_id,
                destination=destination.value,
                event_key=event_key,
                lock=True,
            )
            if existing is None:
                raise DiscordPublicationConflictError("Discord publication enqueue did not create a durable row")
            audit = _audit_by_key(session, key, lock=True)
            if audit is not None:
                return _audit_retry(audit, "enqueue", request_fingerprint)
            if not created:
                _verify_event_identity(
                    existing,
                    event_type=event_type,
                    source_kind=source_kind,
                    source_id=source_id,
                    payload=payload,
                    payload_fingerprint=payload_fingerprint,
                    enabled=command.enabled,
                    channel_id=channel_id,
                )
                _verify_original_enqueue(session, existing.id, request_fingerprint)
                _append_audit(
                    session,
                    publication=existing,
                    action="enqueue",
                    actor=actor,
                    key=key,
                    fingerprint=request_fingerprint,
                    before=_json(existing),
                )
                return _dto(existing)
            _append_audit(
                session,
                publication=existing,
                action="enqueue",
                actor=actor,
                key=key,
                fingerprint=request_fingerprint,
                before=None,
            )
            return _dto(existing)
    except IntegrityError as exc:
        with session.begin_nested():
            audit = _audit_by_key(session, key, lock=True)
            if audit is not None:
                return _audit_retry(audit, "enqueue", request_fingerprint)
            existing = _publication_by_event(
                session,
                guild_id=guild_id,
                destination=destination.value,
                event_key=event_key,
                lock=True,
            )
            if existing is None:
                raise DiscordPublicationConflictError("concurrent Discord publication enqueue conflicted") from exc
            _verify_event_identity(
                existing,
                event_type=event_type,
                source_kind=source_kind,
                source_id=source_id,
                payload=payload,
                payload_fingerprint=payload_fingerprint,
                enabled=command.enabled,
                channel_id=channel_id,
            )
            _verify_original_enqueue(session, existing.id, request_fingerprint)
            _append_audit(
                session,
                publication=existing,
                action="enqueue",
                actor=actor,
                key=key,
                fingerprint=request_fingerprint,
                before=_json(existing),
            )
            return _dto(existing)


def bind_discord_publication_channel(
    session: Session,
    *,
    command: BindDiscordPublicationChannelCommand,
) -> DiscordPublicationDTO:
    publication_id = _positive_int(command.publication_id, field="publication ID")
    channel_id = _snowflake(command.target_channel_id, field="target channel ID")
    return _mutate(
        session,
        publication_id=publication_id,
        action="bind_channel",
        actor=command.actor_discord_user_id,
        key=command.idempotency_key,
        request={"publication_id": publication_id, "channel_id": channel_id},
        mutate=lambda row: _bind(row, channel_id),
    )


def mark_discord_publication_channel_failed(
    session: Session,
    *,
    command: MarkDiscordPublicationChannelFailedCommand,
) -> DiscordPublicationDTO:
    publication_id = _positive_int(command.publication_id, field="publication ID")
    error = _error_code(command.error_code)
    return _mutate(
        session,
        publication_id=publication_id,
        action="channel_failed",
        actor=command.actor_discord_user_id,
        key=command.idempotency_key,
        request={"publication_id": publication_id, "error": error},
        mutate=lambda row: _channel_failed(row, error),
    )


def claim_discord_publication(
    session: Session,
    *,
    command: ClaimDiscordPublicationCommand,
) -> DiscordPublicationDTO:
    publication_id = _positive_int(command.publication_id, field="publication ID")
    if not isinstance(command.authorize_delivery_unknown_retry, bool):
        raise DiscordPublicationError("delivery-unknown authorization must be boolean")
    return _mutate(
        session,
        publication_id=publication_id,
        action="claim",
        actor=command.actor_discord_user_id,
        key=command.idempotency_key,
        request={
            "publication_id": publication_id,
            "authorize_unknown": command.authorize_delivery_unknown_retry,
        },
        mutate=lambda row: _claim(row, command.authorize_delivery_unknown_retry),
    )


def list_stale_pending_discord_publications(
    session: Session,
    *,
    stale_before: datetime,
    limit: int = 100,
) -> tuple[DiscordPublicationDTO, ...]:
    cutoff = _utc_datetime(stale_before, field="stale pending cutoff")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise DiscordPublicationError("stale pending query limit must be between 1 and 1000")
    rows = session.scalars(
        select(DiscordPublication)
        .where(
            DiscordPublication.status == DiscordPublicationStatus.PENDING.value,
            DiscordPublication.attempt_started_at <= cutoff,
        )
        .order_by(DiscordPublication.attempt_started_at, DiscordPublication.id)
        .limit(limit)
    )
    return tuple(_dto(row) for row in rows)


def mark_stale_pending_discord_publication_unknown(
    session: Session,
    *,
    command: MarkStalePendingUnknownCommand,
) -> DiscordPublicationDTO:
    publication_id = _positive_int(command.publication_id, field="publication ID")
    cutoff = _utc_datetime(command.stale_before, field="stale pending cutoff")
    error = _error_code(command.error_code)
    return _mutate(
        session,
        publication_id=publication_id,
        action="stale_pending_unknown",
        actor=command.actor_discord_user_id,
        key=command.idempotency_key,
        request={
            "publication_id": publication_id,
            "stale_before": _datetime_json(cutoff),
            "error": error,
        },
        mutate=lambda row: _mark_stale_pending_unknown(row, cutoff, error),
    )


def record_discord_publication_outcome(
    session: Session,
    *,
    command: RecordDiscordPublicationOutcomeCommand,
) -> DiscordPublicationDTO:
    publication_id = _positive_int(command.publication_id, field="publication ID")
    outcome = _enum(command.outcome, DiscordPublicationStatus, field="publication outcome")
    if outcome not in {
        DiscordPublicationStatus.SENT,
        DiscordPublicationStatus.FAILED,
        DiscordPublicationStatus.DELIVERY_UNKNOWN,
    }:
        raise DiscordPublicationError("publication outcome is invalid")
    message_id = (
        _snowflake(command.discord_message_id, field="Discord message ID")
        if command.discord_message_id is not None
        else None
    )
    error = _error_code(command.error_code) if command.error_code is not None else None
    if not isinstance(command.reconcile_delivery_unknown, bool):
        raise DiscordPublicationError("delivery-unknown reconciliation flag must be boolean")
    return _mutate(
        session,
        publication_id=publication_id,
        action=f"outcome_{outcome.value}",
        actor=command.actor_discord_user_id,
        key=command.idempotency_key,
        request={
            "publication_id": publication_id,
            "outcome": outcome.value,
            "message_id": message_id,
            "error": error,
            "reconcile_unknown": command.reconcile_delivery_unknown,
        },
        mutate=lambda row: _outcome(
            row,
            outcome=outcome,
            message_id=message_id,
            error=error,
            reconcile_unknown=command.reconcile_delivery_unknown,
        ),
    )


def _mutate(session, *, publication_id, action, actor, key, request, mutate):
    actor = _text(actor, field="actor ID", maximum=32)
    key = _text(key, field="idempotency key", maximum=128)
    fingerprint = _fingerprint({"action": action, "actor": actor, **request})
    _ensure_transaction(session)
    try:
        with session.begin_nested():
            audit = _audit_by_key(session, key)
            if audit is not None:
                return _audit_retry(audit, action, fingerprint)
            row = session.scalar(
                select(DiscordPublication).where(DiscordPublication.id == publication_id).with_for_update()
            )
            if row is None:
                raise DiscordPublicationError("Discord publication not found")
            before = _json(row)
            mutate(row)
            session.flush()
            _append_audit(
                session,
                publication=row,
                action=action,
                actor=actor,
                key=key,
                fingerprint=fingerprint,
                before=before,
            )
            return _dto(row)
    except IntegrityError as exc:
        return _recover(session, exc, key, action, fingerprint)


def _bind(row, channel_id):
    status = DiscordPublicationStatus(row.status)
    if status is DiscordPublicationStatus.SUPPRESSED:
        raise DiscordPublicationConflictError("suppressed publication cannot bind a channel")
    if row.attempt_count > 0 and row.target_channel_id != channel_id:
        raise DiscordPublicationConflictError("publication target is immutable after the first attempt")
    if row.target_channel_id not in {None, channel_id}:
        raise DiscordPublicationConflictError("publication is already bound to another channel")
    if status not in {
        DiscordPublicationStatus.AWAITING_CHANNEL,
        DiscordPublicationStatus.FAILED,
        DiscordPublicationStatus.READY,
    }:
        raise DiscordPublicationConflictError("publication cannot bind a channel from its current state")
    if status is DiscordPublicationStatus.FAILED and row.failure_stage != "channel":
        raise DiscordPublicationConflictError("send failure cannot silently retarget")
    row.target_channel_id = channel_id
    row.status = DiscordPublicationStatus.READY.value
    row.last_error_code = None
    row.failure_stage = None


def _channel_failed(row, error):
    if (
        DiscordPublicationStatus(row.status)
        not in {
            DiscordPublicationStatus.AWAITING_CHANNEL,
            DiscordPublicationStatus.FAILED,
        }
        or row.attempt_count != 0
    ):
        raise DiscordPublicationConflictError("channel resolution failure is invalid for current state")
    row.status = DiscordPublicationStatus.FAILED.value
    row.target_channel_id = None
    row.last_error_code = error
    row.failure_stage = "channel"


def _claim(row, authorize_unknown):
    status = DiscordPublicationStatus(row.status)
    if status is DiscordPublicationStatus.DELIVERY_UNKNOWN and not authorize_unknown:
        raise DiscordPublicationConflictError("delivery-unknown retry requires explicit authorization")
    if status not in {
        DiscordPublicationStatus.READY,
        DiscordPublicationStatus.FAILED,
        DiscordPublicationStatus.DELIVERY_UNKNOWN,
    }:
        raise DiscordPublicationConflictError("publication cannot be claimed from its current state")
    if status is DiscordPublicationStatus.FAILED and row.failure_stage != "send":
        raise DiscordPublicationConflictError("channel failure must be resolved before delivery")
    if row.target_channel_id is None:
        raise DiscordPublicationConflictError("publication target channel is missing")
    row.status = DiscordPublicationStatus.PENDING.value
    row.attempt_count += 1
    row.discord_message_id = None
    row.last_error_code = None
    row.failure_stage = None
    row.attempt_started_at = datetime.now(UTC)
    row.published_at = None


def _mark_stale_pending_unknown(row, cutoff, error):
    if DiscordPublicationStatus(row.status) is not DiscordPublicationStatus.PENDING:
        raise DiscordPublicationConflictError("only pending publication can be marked stale")
    started_at = _db_optional_datetime(row.attempt_started_at)
    if started_at is None or started_at > cutoff:
        raise DiscordPublicationConflictError("publication pending attempt is newer than the stale cutoff")
    row.status = DiscordPublicationStatus.DELIVERY_UNKNOWN.value
    row.discord_message_id = None
    row.last_error_code = error
    row.failure_stage = "send"
    row.published_at = None


def _outcome(row, *, outcome, message_id, error, reconcile_unknown):
    current = DiscordPublicationStatus(row.status)
    if current is DiscordPublicationStatus.DELIVERY_UNKNOWN:
        if outcome is not DiscordPublicationStatus.SENT or not reconcile_unknown:
            raise DiscordPublicationConflictError("delivery-unknown state requires explicit sent reconciliation")
    elif current is not DiscordPublicationStatus.PENDING:
        raise DiscordPublicationConflictError("only pending publication can record an outcome")
    if outcome is DiscordPublicationStatus.SENT:
        if message_id is None or error is not None:
            raise DiscordPublicationError("sent outcome requires only a Discord message ID")
        row.status = outcome.value
        row.discord_message_id = message_id
        row.last_error_code = None
        row.failure_stage = None
        row.published_at = datetime.now(UTC)
    else:
        if error is None or message_id is not None:
            raise DiscordPublicationError("failed outcome requires only an error code")
        row.status = outcome.value
        row.discord_message_id = None
        row.last_error_code = error
        row.failure_stage = "send"
        row.published_at = None


def _verify_event_identity(
    row, *, event_type, source_kind, source_id, payload, payload_fingerprint, enabled, channel_id
):
    expected_status = (
        DiscordPublicationStatus.SUPPRESSED.value
        if not enabled
        else DiscordPublicationStatus.READY.value
        if channel_id is not None
        else DiscordPublicationStatus.AWAITING_CHANNEL.value
    )
    if (
        row.event_type != event_type
        or row.source_kind != source_kind
        or row.source_id != source_id
        or row.payload_json != payload
        or row.payload_fingerprint != payload_fingerprint
        or (row.attempt_count == 0 and row.status != expected_status)
        or (channel_id is not None and row.target_channel_id not in {None, channel_id})
    ):
        raise DiscordPublicationConflictError("publication event payload conflicts with the durable intent")


def _append_audit(session, *, publication, action, actor, key, fingerprint, before):
    audit = DiscordPublicationAudit(
        publication_id=publication.id,
        action=action,
        actor_discord_user_id=actor,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        before_json=before,
        after_json=_json(publication),
    )
    session.add(audit)
    session.flush()


def _recover(session, error, key, action, fingerprint):
    with session.begin_nested():
        audit = _audit_by_key(session, key, lock=True)
        if audit is None:
            raise DiscordPublicationConflictError("concurrent Discord publication operation conflicted") from error
        return _audit_retry(audit, action, fingerprint)


def _audit_retry(audit, action, fingerprint):
    if audit.action != action or audit.request_fingerprint != fingerprint:
        raise DiscordPublicationConflictError("idempotency key payload differs from the original publication operation")
    return _dto_from_json(audit.after_json)


def _audit_by_key(session, key, *, lock=False):
    query = select(DiscordPublicationAudit).where(DiscordPublicationAudit.idempotency_key == key)
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


def _verify_original_enqueue(session: Session, publication_id: int, fingerprint: str) -> None:
    original = session.scalar(
        select(DiscordPublicationAudit)
        .where(
            DiscordPublicationAudit.publication_id == publication_id,
            DiscordPublicationAudit.action == "enqueue",
        )
        .order_by(DiscordPublicationAudit.id)
        .limit(1)
        .with_for_update()
    )
    if original is None or original.request_fingerprint != fingerprint:
        raise DiscordPublicationConflictError("publication event differs from the original enqueue request")


def _publication_by_event(session, *, guild_id, destination, event_key, lock):
    query = select(DiscordPublication).where(
        DiscordPublication.guild_id == guild_id,
        DiscordPublication.destination_kind == destination,
        DiscordPublication.event_key == event_key,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalar(query)


def _insert_publication_if_absent(
    session: Session,
    *,
    values: dict[str, object],
) -> bool:
    dialect_name = session.get_bind().dialect.name
    if dialect_name in {"mysql", "mariadb"}:
        session.scalar(select(func.last_insert_id(0)))
        statement = mysql_insert(DiscordPublication).values(**values)
        session.execute(
            statement.on_duplicate_key_update(
                id=DiscordPublication.id + func.last_insert_id(0),
            )
        )
        return bool(session.scalar(select(func.last_insert_id())))
    if dialect_name == "sqlite":
        result = session.execute(
            sqlite_insert(DiscordPublication)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    "guild_id",
                    "destination_kind",
                    "event_key",
                ]
            )
        )
        return result.rowcount == 1

    publication = DiscordPublication(**values)
    session.add(publication)
    session.flush()
    return True


def _json(row):
    return {
        "id": row.id,
        "guild_id": row.guild_id,
        "destination_kind": row.destination_kind,
        "event_type": row.event_type,
        "event_key": row.event_key,
        "source_kind": row.source_kind,
        "source_id": row.source_id,
        "target_channel_id": row.target_channel_id,
        "payload": row.payload_json,
        "payload_fingerprint": row.payload_fingerprint,
        "status": row.status,
        "attempt_count": row.attempt_count,
        "discord_message_id": row.discord_message_id,
        "last_error_code": row.last_error_code,
        "failure_stage": row.failure_stage,
        "attempt_started_at": _datetime_json(row.attempt_started_at),
        "published_at": _datetime_json(row.published_at),
        "created_at": _datetime_json(row.created_at),
        "updated_at": _datetime_json(row.updated_at),
    }


def _dto(row):
    return DiscordPublicationDTO(
        id=row.id,
        guild_id=row.guild_id,
        destination_kind=row.destination_kind,
        event_type=row.event_type,
        event_key=row.event_key,
        source_kind=row.source_kind,
        source_id=row.source_id,
        target_channel_id=row.target_channel_id,
        payload_json=_canonical_json(row.payload_json),
        payload_fingerprint=row.payload_fingerprint,
        status=row.status,
        attempt_count=row.attempt_count,
        discord_message_id=row.discord_message_id,
        last_error_code=row.last_error_code,
        failure_stage=row.failure_stage,
        attempt_started_at=_db_optional_datetime(row.attempt_started_at),
        published_at=_db_optional_datetime(row.published_at),
        created_at=database_datetime_as_utc(row.created_at),
        updated_at=database_datetime_as_utc(row.updated_at),
    )


def _dto_from_json(value):
    if not isinstance(value, dict):
        raise DiscordPublicationConflictError("publication audit snapshot is invalid")
    return DiscordPublicationDTO(
        id=_positive_int(value.get("id"), field="publication ID"),
        guild_id=_snowflake(value.get("guild_id"), field="guild ID"),
        destination_kind=_enum(
            value.get("destination_kind"), DiscordPublicationDestination, field="destination kind"
        ).value,
        event_type=_text(value.get("event_type"), field="event type", maximum=64),
        event_key=_text(value.get("event_key"), field="event key", maximum=128),
        source_kind=_text(value.get("source_kind"), field="source kind", maximum=32),
        source_id=_positive_int(value.get("source_id"), field="source ID"),
        target_channel_id=(
            _snowflake(value.get("target_channel_id"), field="target channel ID")
            if value.get("target_channel_id") is not None
            else None
        ),
        payload_json=_canonical_json(
            validate_safe_publication_payload(
                value.get("payload"),
                destination_kind=value.get("destination_kind"),
            )
        ),
        payload_fingerprint=_text(value.get("payload_fingerprint"), field="payload fingerprint", maximum=64),
        status=_enum(value.get("status"), DiscordPublicationStatus, field="publication status").value,
        attempt_count=_nonnegative_int(value.get("attempt_count"), field="attempt count"),
        discord_message_id=(
            _snowflake(value.get("discord_message_id"), field="message ID")
            if value.get("discord_message_id") is not None
            else None
        ),
        last_error_code=value.get("last_error_code"),
        failure_stage=value.get("failure_stage"),
        attempt_started_at=_datetime_from_json(value.get("attempt_started_at")),
        published_at=_datetime_from_json(value.get("published_at")),
        created_at=_datetime_from_json(value.get("created_at"), required=True),
        updated_at=_datetime_from_json(value.get("updated_at"), required=True),
    )


def _ensure_transaction(session):
    if not session.in_transaction():
        session.begin()
    connection = session.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")


def _fingerprint(value):
    encoded = _canonical_json(value)
    return sha256(encoded.encode()).hexdigest()


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _enum(value, enum_type, *, field):
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        raise DiscordPublicationError(f"{field} is invalid") from exc


def _text(value, *, field, maximum):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise DiscordPublicationError(f"{field} must contain 1 to {maximum} characters")
    return value.strip()


def _snowflake(value, *, field):
    value = _text(value, field=field, maximum=32)
    if not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise DiscordPublicationError(f"{field} must be a Discord snowflake")
    return value


def _positive_int(value, *, field):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DiscordPublicationError(f"{field} must be positive")
    return value


def _nonnegative_int(value, *, field):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DiscordPublicationError(f"{field} must be nonnegative")
    return value


def _error_code(value):
    value = _text(value, field="error code", maximum=64)
    if not value.replace("_", "").replace("-", "").isalnum():
        raise DiscordPublicationError("error code is invalid")
    return value


def _datetime_json(value):
    return database_datetime_as_utc(value).isoformat() if value is not None else None


def _datetime_from_json(value, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise DiscordPublicationConflictError("publication timestamp is invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise DiscordPublicationConflictError("publication timestamp is invalid")
    return parsed.astimezone(UTC)


def _utc_datetime(value, *, field):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DiscordPublicationError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _db_optional_datetime(value):
    return database_datetime_as_utc(value) if value is not None else None
