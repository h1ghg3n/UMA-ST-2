from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import RaceEntry, RaceOperationAudit
from umacircle_bot.domain.discord_publications import (
    DiscordPublicationDestination,
    validate_safe_publication_payload,
)
from umacircle_bot.domain.errors import (
    MatchResultConflictError,
    MatchResultError,
)
from umacircle_bot.services.discord_publications import (
    DiscordPublicationDTO,
    EnqueueDiscordPublicationCommand,
    enqueue_discord_publication,
    get_discord_publication_by_event,
)
from umacircle_bot.services.guild_discord_settings import (
    get_guild_discord_settings,
)
from umacircle_bot.services.match_results import MatchResultOperationDTO

ROOM_RESULT_COMMAND_NAMES = {
    "room_result_submit": "match.staff.result-submit",
    "room_result_review": "match.staff.result-review",
    "room_result_correct": "match.staff.result-correct",
    "room_result_reject": "match.staff.result-reject",
    "room_result_confirm": "match.staff.result-confirm",
    "room_result_publish": "match.staff.publish",
}

AuditSnapshot = tuple[
    Mapping[str, Any],
    Mapping[str, Any],
    tuple[Mapping[str, Any], ...],
]


@dataclass(frozen=True, slots=True)
class MatchResultNotificationDTO:
    operation: MatchResultOperationDTO
    publications: tuple[DiscordPublicationDTO, ...]


def enqueue_match_result_notifications(
    session: Session,
    *,
    operation: MatchResultOperationDTO,
    guild_id: str,
    actor_discord_user_id: str,
    correlation_id: str,
) -> MatchResultNotificationDTO:
    """Attach durable Discord intents before the caller commits the mutation."""

    command_name = ROOM_RESULT_COMMAND_NAMES.get(operation.action)
    if command_name is None:
        raise MatchResultError("room-match result operation is not publishable")
    actor = _bounded_text(actor_discord_user_id, field="actor ID", maximum=32)
    correlation = _bounded_text(
        correlation_id,
        field="correlation ID",
        maximum=64,
    )
    audit = session.scalar(
        select(RaceOperationAudit).where(RaceOperationAudit.id == operation.audit_id).with_for_update()
    )
    if (
        audit is None
        or audit.race_id != operation.race_id
        or audit.action != operation.action
        or audit.actor_discord_user_id != actor
    ):
        raise MatchResultConflictError("room-match result operation audit does not match notification context")
    snapshot = _verify_operation_snapshot(operation, audit.after_json)
    settings = get_guild_discord_settings(session, guild_id=guild_id)

    publications: list[DiscordPublicationDTO] = []
    if operation.action == "room_result_publish":
        if operation.publication is None:
            raise MatchResultConflictError("room-match publication operation is missing its publication state")
        publications.append(
            _existing_or_enqueue(
                session,
                command=_confirmed_result_publication(
                    session,
                    snapshot=snapshot,
                    guild_id=guild_id,
                    actor=actor,
                    channel_id=operation.publication.target_channel_id,
                    enabled=settings.room_match_announcements_enabled,
                ),
            )
        )

    publications.append(
        _existing_or_enqueue(
            session,
            command=EnqueueDiscordPublicationCommand(
                guild_id=guild_id,
                destination_kind=DiscordPublicationDestination.LOG_MIRROR.value,
                event_type="staff_operation_succeeded",
                event_key=f"race-operation-audit:{operation.audit_id}",
                source_kind="race_operation_audit",
                source_id=operation.audit_id,
                payload={
                    "command_name": command_name,
                    "resource_kind": "room_match_race",
                    "resource_id": operation.race_id,
                    "actor_discord_user_id": actor,
                    "audit_id": operation.audit_id,
                    "correlation_id": correlation,
                },
                enabled=True,
                configured_channel_id=settings.log_channel_id,
                actor_discord_user_id=actor,
                idempotency_key=(f"discord-log:race-operation-audit:{operation.audit_id}"),
            ),
        )
    )
    return MatchResultNotificationDTO(
        operation=operation,
        publications=tuple(publications),
    )


def _confirmed_result_publication(
    session: Session,
    *,
    snapshot: AuditSnapshot,
    guild_id: str,
    actor: str,
    channel_id: str | None,
    enabled: bool,
) -> EnqueueDiscordPublicationCommand:
    race, submission, results = snapshot
    if submission.get("status") != "confirmed":
        raise MatchResultConflictError("confirmed room-match result snapshot is incomplete")
    race_id = _positive_int(race.get("id"), field="race ID")
    submission_id = _positive_int(
        submission.get("id"),
        field="submission ID",
    )
    revision_number = _positive_int(
        submission.get("revision_number"),
        field="revision number",
    )
    entries = list(
        session.scalars(
            select(RaceEntry).where(RaceEntry.race_id == race_id).order_by(RaceEntry.entry_number, RaceEntry.id)
        )
    )
    entry_by_number = {entry.entry_number: entry for entry in entries}
    if len(entry_by_number) != len(entries):
        raise MatchResultConflictError("room-match result entries are ambiguous")
    result_entry_numbers = {_positive_int(item.get("entry_number"), field="result entry number") for item in results}
    if set(entry_by_number) != result_entry_numbers:
        raise MatchResultConflictError("confirmed room-match result entry set changed")

    ranked_entries = []
    for result in sorted(
        results,
        key=lambda item: (
            _positive_int(item.get("rank"), field="result rank"),
            _positive_int(
                item.get("entry_number"),
                field="result entry number",
            ),
        ),
    ):
        entry_number = _positive_int(
            result.get("entry_number"),
            field="result entry number",
        )
        character_name = result.get("character_name")
        if character_name is not None and not isinstance(character_name, str):
            raise MatchResultConflictError("confirmed room-match result character snapshot is invalid")
        entry = entry_by_number[entry_number]
        ranked_entry = {
            "rank": _positive_int(result.get("rank"), field="result rank"),
            "entry_number": entry_number,
            "player_display_name": entry.player_name,
            "character_name": character_name or entry.horse_name_or_label,
        }
        optional_details = {
            "character_evaluation_rank": _optional_text(
                result.get("character_evaluation_rank"),
                field="character evaluation rank",
            ),
            "popularity_rank": _optional_positive_int(
                result.get("popularity_rank"),
                field="popularity rank",
            ),
            "finish_time_ms": _optional_positive_int(
                result.get("finish_time_ms"),
                field="finish time milliseconds",
            ),
            "finish_margin_text": _optional_text(
                result.get("finish_margin_text"),
                field="finish margin",
            ),
        }
        ranked_entry.update({key: value for key, value in optional_details.items() if value is not None})
        ranked_entries.append(ranked_entry)
    return EnqueueDiscordPublicationCommand(
        guild_id=guild_id,
        destination_kind=(DiscordPublicationDestination.ROOM_MATCH_ANNOUNCEMENT.value),
        event_type="room_match_result_confirmed",
        event_key=(f"room-match-result-confirmed:{race_id}:revision:{revision_number}"),
        source_kind="room_match_result_submission",
        source_id=submission_id,
        payload={
            "race_id": race_id,
            "race_name": _text(race.get("name"), field="race name"),
            "match_type": _text(
                submission.get("match_type"),
                field="match type",
            ),
            "ranked_entries": ranked_entries,
        },
        enabled=enabled,
        configured_channel_id=channel_id,
        actor_discord_user_id=actor,
        idempotency_key=(f"discord-public:room-result:{race_id}:revision:{revision_number}"),
    )


def _existing_or_enqueue(
    session: Session,
    *,
    command: EnqueueDiscordPublicationCommand,
) -> DiscordPublicationDTO:
    existing = get_discord_publication_by_event(
        session,
        guild_id=command.guild_id,
        destination_kind=command.destination_kind,
        event_key=command.event_key,
    )
    if existing is None:
        return enqueue_discord_publication(session, command=command)
    payload = validate_safe_publication_payload(
        command.payload,
        destination_kind=command.destination_kind,
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if (
        existing.event_type != command.event_type
        or existing.source_kind != command.source_kind
        or existing.source_id != command.source_id
        or existing.payload_json != encoded
        or existing.payload_fingerprint != sha256(encoded.encode()).hexdigest()
    ):
        raise MatchResultConflictError("stored Discord publication does not match the room-match result audit")
    return existing


def _verify_operation_snapshot(
    operation: MatchResultOperationDTO,
    value: object,
) -> AuditSnapshot:
    state = _mapping(value, field="audit state")
    race = _mapping(state.get("race"), field="audit race")
    submission = _mapping(
        state.get("submission"),
        field="audit submission",
    )
    raw_results = state.get("results")
    if not isinstance(raw_results, list):
        raise MatchResultConflictError("room-match result operation audit snapshot is invalid")
    results = tuple(_mapping(item, field="audit result") for item in raw_results)
    operation_submission = operation.submission
    if operation_submission is None:
        raise MatchResultConflictError("room-match result operation submission snapshot is missing")
    expected_results = tuple(
        (
            item.entry_number,
            item.rank,
            item.game_account_id,
            item.character_name,
            item.character_evaluation_rank,
            item.popularity_rank,
            item.finish_time_ms,
            item.finish_margin_text,
        )
        for item in operation.results
    )
    actual_results = tuple(
        (
            _positive_int(
                item.get("entry_number"),
                field="result entry number",
            ),
            _positive_int(item.get("rank"), field="result rank"),
            item.get("game_account_id"),
            item.get("character_name"),
            item.get("character_evaluation_rank"),
            item.get("popularity_rank"),
            item.get("finish_time_ms"),
            item.get("finish_margin_text"),
        )
        for item in results
    )
    if (
        _positive_int(race.get("id"), field="race ID") != operation.race_id
        or race.get("event_id") != operation.event_id
        or _text(race.get("name"), field="race name") != operation.race_name
        or _text(race.get("status"), field="race status") != operation.race_status
        or _positive_int(submission.get("id"), field="submission ID") != operation_submission.id
        or _positive_int(
            submission.get("revision_number"),
            field="revision number",
        )
        != operation_submission.revision_number
        or _text(submission.get("match_type"), field="match type") != operation_submission.match_type
        or _text(submission.get("status"), field="submission status") != operation_submission.status
        or submission.get("is_current") is not operation_submission.is_current
        or actual_results != expected_results
    ):
        raise MatchResultConflictError("room-match result operation does not match its authoritative audit snapshot")
    return race, submission, results


def _bounded_text(
    value: object,
    *,
    field: str,
    maximum: int,
) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise MatchResultError(f"{field} must contain 1 to {maximum} characters")
    return value.strip()


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MatchResultConflictError(f"{field} is invalid")
    return value


def _positive_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MatchResultConflictError(f"{field} is invalid")
    return value


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise MatchResultConflictError(f"{field} is invalid")
    return value


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field=field)


def _optional_positive_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field=field)
