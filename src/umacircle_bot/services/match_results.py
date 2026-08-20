from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    MatchResultProvenance,
    MatchResultPublication,
    MatchResultSubmission,
    Race,
    RaceEntry,
    RaceOperationAudit,
    RaceResult,
)
from umacircle_bot.domain.errors import MatchResultConflictError, MatchResultError
from umacircle_bot.domain.match_results import (
    MatchResultInput,
    MatchResultPublicationStatus,
    MatchResultSubmissionStatus,
    normalize_match_type,
    normalize_result_input,
    validate_complete_result,
    validate_confirmation,
    validate_correction,
    validate_initial_submission,
    validate_publication_intent,
    validate_publication_outcome,
    validate_rejection,
    validate_review,
    validate_submission_state,
)
from umacircle_bot.domain.races import MatchRaceStatus
from umacircle_bot.domain.time import database_datetime_as_utc

RESULT_REVIEW_CAPABILITY = "result.review"
RESULT_CONFIRM_CAPABILITY = "result.confirm"
RESULT_PUBLISH_CAPABILITY = "result.publish"


@dataclass(frozen=True, slots=True)
class SubmitMatchResultCommand:
    race_id: int
    match_type: str
    results: tuple[MatchResultInput, ...]
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewMatchResultCommand:
    race_id: int
    revision_number: int
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CorrectMatchResultCommand:
    race_id: int
    revision_number: int
    match_type: str
    results: tuple[MatchResultInput, ...]
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RejectMatchResultCommand:
    race_id: int
    revision_number: int
    actor_discord_user_id: str
    idempotency_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class ConfirmMatchResultCommand:
    race_id: int
    revision_number: int
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PublishMatchResultCommand:
    race_id: int
    revision_number: int
    target_channel_id: str
    actor_discord_user_id: str
    idempotency_key: str
    authorize_delivery_unknown_retry: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MarkMatchResultPublicationSentCommand:
    race_id: int
    publication_id: int
    discord_message_id: str
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MarkMatchResultPublicationFailedCommand:
    race_id: int
    publication_id: int
    error_code: str
    actor_discord_user_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class MarkMatchResultPublicationUnknownCommand:
    race_id: int
    publication_id: int
    error_code: str
    actor_discord_user_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class MatchResultLineDTO:
    entry_number: int
    rank: int
    game_account_id: int | None = None
    character_name: str | None = None
    character_evaluation_rank: str | None = None
    popularity_rank: int | None = None
    finish_time_ms: int | None = None
    finish_margin_text: str | None = None


@dataclass(frozen=True, slots=True)
class MatchResultSubmissionDTO:
    id: int
    revision_number: int
    supersedes_submission_id: int | None
    match_type: str
    status: str
    is_current: bool
    submitted_by_discord_user_id: str
    reviewed_by_discord_user_id: str | None
    rejected_by_discord_user_id: str | None
    rejection_reason: str | None
    confirmed_by_discord_user_id: str | None
    created_at: datetime
    reviewed_at: datetime | None
    rejected_at: datetime | None
    confirmed_at: datetime | None


@dataclass(frozen=True, slots=True)
class MatchResultPublicationLineDTO:
    entry_number: int
    rank: int
    display_name: str | None
    game_account_id: int | None
    character_name: str | None
    character_evaluation_rank: str | None
    popularity_rank: int | None
    finish_time_ms: int | None
    finish_margin_text: str | None


@dataclass(frozen=True, slots=True)
class MatchResultPublicationPayloadDTO:
    race_id: int
    race_name: str
    submission_id: int
    revision_number: int
    match_type: str
    results: tuple[MatchResultPublicationLineDTO, ...]


@dataclass(frozen=True, slots=True)
class MatchResultPublicationDTO:
    id: int
    race_id: int
    submission_id: int
    target_channel_id: str
    status: str
    attempt_count: int
    discord_message_id: str | None
    last_error_code: str | None
    payload: MatchResultPublicationPayloadDTO
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class MatchResultOperationDTO:
    action: str
    audit_id: int
    race_id: int
    event_id: int | None
    race_name: str
    race_status: str
    submission: MatchResultSubmissionDTO | None
    results: tuple[MatchResultLineDTO, ...]
    publication: MatchResultPublicationDTO | None


Mutation = Callable[[Race], None]


def submit_match_result(
    session: Session,
    *,
    command: SubmitMatchResultCommand,
) -> MatchResultOperationDTO:
    action = "room_result_submit"
    race_id = _positive_int(command.race_id, field="race ID")
    match_type = normalize_match_type(command.match_type)
    results = normalize_result_input(command.results)
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    fingerprint = _fingerprint(
        action,
        {
            "race_id": race_id,
            "actor": actor,
            "match_type": match_type,
            "results": _result_input_json(results),
            "reason": reason,
        },
    )

    def mutate(race: Race) -> None:
        entries = _load_entries(session, race.id, lock=True)
        history = _load_latest_submission(session, race.id, lock=True)
        existing_results = _load_race_results(session, race.id, lock=True)
        validate_initial_submission(
            race_status=race.status,
            has_results=bool(existing_results),
            has_submission_history=history is not None,
        )
        validate_complete_result(results, final_entry_numbers=[entry.entry_number for entry in entries])
        submission = MatchResultSubmission(
            event_id=race.event_id,
            race_id=race.id,
            match_type=match_type,
            submitted_by_discord_user_id=actor,
            submission_status=MatchResultSubmissionStatus.PENDING_REVIEW.value,
            raw_input_json=_raw_input_json(match_type, results),
            revision_number=1,
            current_marker="current",
        )
        session.add(submission)
        race.status = MatchRaceStatus.RESULT_REVIEW.value
        session.flush()

    return _execute_mutation(
        session,
        race_id=race_id,
        action=action,
        capability=RESULT_REVIEW_CAPABILITY,
        actor=actor,
        request_key=request_key,
        fingerprint=fingerprint,
        reason=reason,
        mutate=mutate,
    )


def review_match_result(
    session: Session,
    *,
    command: ReviewMatchResultCommand,
) -> MatchResultOperationDTO:
    action = "room_result_review"
    race_id, revision, actor, request_key, reason = _normalize_revision_command(command)
    fingerprint = _fingerprint(
        action,
        {"race_id": race_id, "revision_number": revision, "actor": actor, "reason": reason},
    )

    def mutate(race: Race) -> None:
        submission = _load_submission(session, race.id, revision, lock=True)
        validate_review(
            race_status=race.status,
            submission_status=submission.submission_status,
            is_current=submission.current_marker == "current",
        )
        entries = _load_entries(session, race.id, lock=True)
        results = _submission_results(submission)
        validate_complete_result(results, final_entry_numbers=[entry.entry_number for entry in entries])
        now = _now()
        submission.submission_status = MatchResultSubmissionStatus.REVIEWED.value
        submission.reviewed_by_discord_user_id = actor
        submission.reviewed_at = now
        session.flush()

    return _execute_mutation(
        session,
        race_id=race_id,
        action=action,
        capability=RESULT_REVIEW_CAPABILITY,
        actor=actor,
        request_key=request_key,
        fingerprint=fingerprint,
        reason=reason,
        mutate=mutate,
    )


def correct_match_result(
    session: Session,
    *,
    command: CorrectMatchResultCommand,
) -> MatchResultOperationDTO:
    action = "room_result_correct"
    race_id, revision, actor, request_key, reason = _normalize_revision_command(command)
    match_type = normalize_match_type(command.match_type)
    results = normalize_result_input(command.results)
    fingerprint = _fingerprint(
        action,
        {
            "race_id": race_id,
            "revision_number": revision,
            "actor": actor,
            "match_type": match_type,
            "results": _result_input_json(results),
            "reason": reason,
        },
    )

    def mutate(race: Race) -> None:
        predecessor = _load_submission(session, race.id, revision, lock=True)
        latest = _load_latest_submission(session, race.id, lock=True)
        existing_results = _load_race_results(session, race.id, lock=True)
        validate_correction(
            race_status=race.status,
            submission_status=predecessor.submission_status,
            is_latest_revision=latest is not None and latest.id == predecessor.id,
            has_results=bool(existing_results),
        )
        entries = _load_entries(session, race.id, lock=True)
        validate_complete_result(results, final_entry_numbers=[entry.entry_number for entry in entries])
        if predecessor.submission_status != MatchResultSubmissionStatus.REJECTED.value:
            predecessor.submission_status = MatchResultSubmissionStatus.SUPERSEDED.value
            predecessor.current_marker = None
        successor = MatchResultSubmission(
            event_id=race.event_id,
            race_id=race.id,
            match_type=match_type,
            submitted_by_discord_user_id=actor,
            submission_status=MatchResultSubmissionStatus.PENDING_REVIEW.value,
            raw_input_json=_raw_input_json(match_type, results),
            revision_number=predecessor.revision_number + 1,
            supersedes_submission_id=predecessor.id,
            current_marker="current",
        )
        session.add(successor)
        session.flush()

    return _execute_mutation(
        session,
        race_id=race_id,
        action=action,
        capability=RESULT_REVIEW_CAPABILITY,
        actor=actor,
        request_key=request_key,
        fingerprint=fingerprint,
        reason=reason,
        mutate=mutate,
    )


def reject_match_result(
    session: Session,
    *,
    command: RejectMatchResultCommand,
) -> MatchResultOperationDTO:
    action = "room_result_reject"
    race_id, revision, actor, request_key, reason = _normalize_revision_command(command, require_reason=True)
    assert reason is not None
    fingerprint = _fingerprint(
        action,
        {"race_id": race_id, "revision_number": revision, "actor": actor, "reason": reason},
    )

    def mutate(race: Race) -> None:
        submission = _load_submission(session, race.id, revision, lock=True)
        validate_rejection(
            race_status=race.status,
            submission_status=submission.submission_status,
            is_current=submission.current_marker == "current",
        )
        now = _now()
        submission.submission_status = MatchResultSubmissionStatus.REJECTED.value
        submission.current_marker = None
        submission.rejected_by_discord_user_id = actor
        submission.rejected_at = now
        submission.rejection_reason = reason
        session.flush()

    return _execute_mutation(
        session,
        race_id=race_id,
        action=action,
        capability=RESULT_REVIEW_CAPABILITY,
        actor=actor,
        request_key=request_key,
        fingerprint=fingerprint,
        reason=reason,
        mutate=mutate,
    )


def confirm_match_result(
    session: Session,
    *,
    command: ConfirmMatchResultCommand,
) -> MatchResultOperationDTO:
    action = "room_result_confirm"
    race_id, revision, actor, request_key, reason = _normalize_revision_command(command)
    fingerprint = _fingerprint(
        action,
        {"race_id": race_id, "revision_number": revision, "actor": actor, "reason": reason},
    )

    def mutate(race: Race) -> None:
        submission = _load_submission(session, race.id, revision, lock=True)
        existing_results = _load_race_results(session, race.id, lock=True)
        validate_confirmation(
            race_status=race.status,
            submission_status=submission.submission_status,
            is_current=submission.current_marker == "current",
            has_results=bool(existing_results),
        )
        entries = _load_entries(session, race.id, lock=True)
        submitted_results = _submission_results(submission)
        validate_complete_result(
            submitted_results,
            final_entry_numbers=[entry.entry_number for entry in entries],
        )
        entry_by_number = {entry.entry_number: entry for entry in entries}
        result_rows: list[RaceResult] = []
        for item in submitted_results:
            entry = entry_by_number[item.entry_number]
            result = RaceResult(
                race_id=race.id,
                entry_number=item.entry_number,
                game_account_id=entry.game_account_id,
                owner_at_event_persona_id=entry.owner_at_event_persona_id,
                character_name=entry.horse_name_or_label,
                character_evaluation_rank=item.character_evaluation_rank,
                popularity_rank=item.popularity_rank,
                finish_time_ms=item.finish_time_ms,
                finish_margin_text=item.finish_margin_text,
                rank=item.rank,
                converted_rank=None,
                is_betting_excluded=False,
                is_rating_excluded=False,
                is_result_void=False,
                raw_result_json={
                    "source": "native_room_match",
                    "entry_number": item.entry_number,
                    "rank": item.rank,
                    "character_evaluation_rank": item.character_evaluation_rank,
                    "popularity_rank": item.popularity_rank,
                    "finish_time_ms": item.finish_time_ms,
                    "finish_margin_text": item.finish_margin_text,
                },
            )
            session.add(result)
            result_rows.append(result)
        session.flush()
        session.add_all(
            [
                MatchResultProvenance(
                    race_result_id=result.id,
                    match_result_submission_id=submission.id,
                )
                for result in result_rows
            ]
        )
        now = _now()
        submission.submission_status = MatchResultSubmissionStatus.CONFIRMED.value
        submission.confirmed_by_discord_user_id = actor
        submission.confirmed_at = now
        race.status = MatchRaceStatus.RESULT_CONFIRMED.value
        session.flush()
        _validate_confirmed_result_set(
            session,
            race=race,
            submission=submission,
            entries=entries,
            lock=True,
        )

    return _execute_mutation(
        session,
        race_id=race_id,
        action=action,
        capability=RESULT_CONFIRM_CAPABILITY,
        actor=actor,
        request_key=request_key,
        fingerprint=fingerprint,
        reason=reason,
        mutate=mutate,
    )


def publish_match_result(
    session: Session,
    *,
    command: PublishMatchResultCommand,
) -> MatchResultOperationDTO:
    action = "room_result_publish"
    race_id = _positive_int(command.race_id, field="race ID")
    revision = _positive_int(command.revision_number, field="revision number")
    target_channel_id = _discord_snowflake(command.target_channel_id, field="target channel ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    if not isinstance(command.authorize_delivery_unknown_retry, bool):
        raise MatchResultError("delivery-unknown retry authorization must be boolean")
    fingerprint = _fingerprint(
        action,
        {
            "race_id": race_id,
            "revision_number": revision,
            "target_channel_id": target_channel_id,
            "actor": actor,
            "authorize_delivery_unknown_retry": command.authorize_delivery_unknown_retry,
            "reason": reason,
        },
    )

    def mutate(race: Race) -> None:
        submission = _load_submission(session, race.id, revision, lock=True)
        publication = _load_publication(session, race.id, lock=True)
        validate_publication_intent(
            race_status=race.status,
            submission_status=submission.submission_status,
            publication_status=publication.status if publication is not None else None,
            authorize_delivery_unknown_retry=command.authorize_delivery_unknown_retry,
        )
        entries = _load_entries(session, race.id, lock=True)
        confirmed_results = _validate_confirmed_result_set(
            session,
            race=race,
            submission=submission,
            entries=entries,
            lock=True,
        )
        payload = _publication_payload(race, submission, entries, confirmed_results)
        payload_fingerprint = _fingerprint(
            "room_result_publication_payload",
            {"target_channel_id": target_channel_id, "payload": payload},
        )
        if publication is None:
            publication = MatchResultPublication(
                race_id=race.id,
                match_result_submission_id=submission.id,
                target_channel_id=target_channel_id,
                payload_json=payload,
                request_fingerprint=payload_fingerprint,
                status=MatchResultPublicationStatus.PENDING.value,
                attempt_count=1,
            )
            session.add(publication)
        else:
            if publication.match_result_submission_id != submission.id:
                raise MatchResultConflictError("publication belongs to a different result revision")
            if publication.target_channel_id != target_channel_id:
                raise MatchResultConflictError("publication target channel cannot be changed")
            if publication.payload_json != payload or publication.request_fingerprint != payload_fingerprint:
                raise MatchResultConflictError("stored publication payload no longer matches confirmed state")
            publication.status = MatchResultPublicationStatus.PENDING.value
            publication.attempt_count += 1
            publication.discord_message_id = None
            publication.last_error_code = None
            publication.published_at = None
        session.flush()

    return _execute_mutation(
        session,
        race_id=race_id,
        action=action,
        capability=RESULT_PUBLISH_CAPABILITY,
        actor=actor,
        request_key=request_key,
        fingerprint=fingerprint,
        reason=reason,
        mutate=mutate,
    )


def mark_match_result_publication_sent(
    session: Session,
    *,
    command: MarkMatchResultPublicationSentCommand,
) -> MatchResultOperationDTO:
    message_id = _discord_snowflake(command.discord_message_id, field="Discord message ID")
    return _record_publication_outcome(
        session,
        command=command,
        action="room_result_publish_sent",
        outcome=MatchResultPublicationStatus.SENT,
        message_id=message_id,
        error_code=None,
    )


def mark_match_result_publication_failed(
    session: Session,
    *,
    command: MarkMatchResultPublicationFailedCommand,
) -> MatchResultOperationDTO:
    error_code = _error_code(command.error_code)
    return _record_publication_outcome(
        session,
        command=command,
        action="room_result_publish_failed",
        outcome=MatchResultPublicationStatus.FAILED,
        message_id=None,
        error_code=error_code,
    )


def mark_match_result_publication_unknown(
    session: Session,
    *,
    command: MarkMatchResultPublicationUnknownCommand,
) -> MatchResultOperationDTO:
    error_code = _error_code(command.error_code)
    return _record_publication_outcome(
        session,
        command=command,
        action="room_result_publish_delivery_unknown",
        outcome=MatchResultPublicationStatus.DELIVERY_UNKNOWN,
        message_id=None,
        error_code=error_code,
    )


def get_room_match_result(
    session: Session,
    *,
    race_id: int,
    revision_number: int | None = None,
) -> MatchResultOperationDTO:
    normalized_race_id = _positive_int(race_id, field="race ID")
    normalized_revision = (
        _positive_int(revision_number, field="revision number") if revision_number is not None else None
    )
    race = _load_race(session, normalized_race_id, lock=False)
    _ensure_match_entry_kind_exclusive(session, race.id)
    if normalized_revision is not None:
        _load_submission(session, race.id, normalized_revision, lock=False)
    return _result_from_state(
        session,
        action="room_result_show",
        audit_id=0,
        race=race,
        revision_number=normalized_revision,
    )


def _record_publication_outcome(
    session: Session,
    *,
    command: MarkMatchResultPublicationSentCommand
    | MarkMatchResultPublicationFailedCommand
    | MarkMatchResultPublicationUnknownCommand,
    action: str,
    outcome: MatchResultPublicationStatus,
    message_id: str | None,
    error_code: str | None,
) -> MatchResultOperationDTO:
    race_id = _positive_int(command.race_id, field="race ID")
    publication_id = _positive_int(command.publication_id, field="publication ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = (
        _optional_text(command.reason, field="reason", maximum=255)
        if isinstance(command, MarkMatchResultPublicationSentCommand)
        else None
    )
    fingerprint = _fingerprint(
        action,
        {
            "race_id": race_id,
            "publication_id": publication_id,
            "actor": actor,
            "outcome": outcome.value,
            "discord_message_id": message_id,
            "error_code": error_code,
            "reason": reason,
        },
    )

    def mutate(race: Race) -> None:
        publication = _load_publication(session, race.id, lock=True)
        if publication is None or publication.id != publication_id:
            raise MatchResultError("room-match result publication not found")
        validate_publication_outcome(current_status=publication.status, outcome=outcome)
        if outcome is MatchResultPublicationStatus.SENT:
            publication.status = outcome.value
            publication.discord_message_id = message_id
            publication.last_error_code = None
            publication.published_at = _now()
        else:
            publication.status = outcome.value
            publication.discord_message_id = None
            publication.last_error_code = error_code
            publication.published_at = None
        session.flush()

    return _execute_mutation(
        session,
        race_id=race_id,
        action=action,
        capability=RESULT_PUBLISH_CAPABILITY,
        actor=actor,
        request_key=request_key,
        fingerprint=fingerprint,
        reason=reason,
        mutate=mutate,
    )


def _execute_mutation(
    session: Session,
    *,
    race_id: int,
    action: str,
    capability: str,
    actor: str,
    request_key: str,
    fingerprint: str,
    reason: str | None,
    mutate: Mutation,
) -> MatchResultOperationDTO:
    _ensure_application_transaction(session)
    try:
        with session.begin_nested():
            race = _load_race(session, race_id, lock=True)
            audit = _load_audit(session, request_key)
            if audit is not None:
                return _idempotent_result(audit, action=action, fingerprint=fingerprint)
            if race.external_source is not None:
                raise MatchResultConflictError("imported historical races are read-only")
            _ensure_match_entry_kind_exclusive(session, race.id)
            before = _state_json(session, race)
            mutate(race)
            audit = _append_audit(
                session,
                race=race,
                action=action,
                capability=capability,
                actor=actor,
                request_key=request_key,
                fingerprint=fingerprint,
                before=before,
                reason=reason,
            )
            return _result_from_json(action=action, audit_id=audit.id, value=audit.after_json)
    except IntegrityError as exc:
        return _recover_concurrent_retry(
            session,
            error=exc,
            request_key=request_key,
            action=action,
            fingerprint=fingerprint,
        )


def _ensure_application_transaction(session: Session) -> None:
    if not session.in_transaction():
        session.begin()
    connection = session.connection()
    if connection.dialect.name != "sqlite":
        return
    driver_connection = connection.connection.driver_connection
    if not driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")


def _append_audit(
    session: Session,
    *,
    race: Race,
    action: str,
    capability: str,
    actor: str,
    request_key: str,
    fingerprint: str,
    before: dict[str, object],
    reason: str | None,
) -> RaceOperationAudit:
    audit = RaceOperationAudit(
        race_id=race.id,
        action=action,
        capability=capability,
        actor_discord_user_id=actor,
        idempotency_key=request_key,
        request_fingerprint=fingerprint,
        before_json=before,
        after_json=_state_json(session, race),
        reason=reason,
    )
    session.add(audit)
    session.flush()
    return audit


def _recover_concurrent_retry(
    session: Session,
    *,
    error: IntegrityError,
    request_key: str,
    action: str,
    fingerprint: str,
) -> MatchResultOperationDTO:
    with session.begin_nested():
        audit = _load_audit(session, request_key, lock=True)
        if audit is None:
            raise MatchResultConflictError("concurrent room-match result mutation conflicted") from error
        return _idempotent_result(audit, action=action, fingerprint=fingerprint)


def _idempotent_result(
    audit: RaceOperationAudit,
    *,
    action: str,
    fingerprint: str,
) -> MatchResultOperationDTO:
    if audit.action != action or audit.request_fingerprint != fingerprint:
        raise MatchResultConflictError(
            "idempotency key payload does not match the original room-match result operation"
        )
    return _result_from_json(action=action, audit_id=audit.id, value=audit.after_json)


def _load_race(session: Session, race_id: int, *, lock: bool) -> Race:
    query = select(Race).where(Race.id == race_id).order_by(Race.id)
    if lock:
        query = query.with_for_update()
    race = session.scalar(query)
    if race is None or race.race_kind != "room_match":
        raise MatchResultError("room-match race not found")
    return race


def _load_submission(
    session: Session,
    race_id: int,
    revision_number: int,
    *,
    lock: bool,
) -> MatchResultSubmission:
    history = _load_submission_history(session, race_id, lock=lock)
    for submission in history:
        if submission.revision_number == revision_number:
            return submission
    raise MatchResultError("room-match result revision not found")


def _load_latest_submission(
    session: Session,
    race_id: int,
    *,
    lock: bool,
) -> MatchResultSubmission | None:
    history = _load_submission_history(session, race_id, lock=lock)
    return history[-1] if history else None


def _load_submission_history(
    session: Session,
    race_id: int,
    *,
    lock: bool,
) -> list[MatchResultSubmission]:
    query = (
        select(MatchResultSubmission)
        .where(MatchResultSubmission.race_id == race_id)
        .order_by(MatchResultSubmission.revision_number, MatchResultSubmission.id)
    )
    if lock:
        query = query.with_for_update()
    history = list(session.scalars(query))
    for submission in history:
        _validate_submission_row(submission)
    if [submission.revision_number for submission in history] != list(range(1, len(history) + 1)):
        raise MatchResultConflictError("persisted result revision sequence is not contiguous")
    for index, submission in enumerate(history):
        predecessor = history[index - 1] if index else None
        if predecessor is None:
            if submission.supersedes_submission_id is not None:
                raise MatchResultConflictError("initial result revision has invalid predecessor lineage")
            continue
        if submission.supersedes_submission_id != predecessor.id:
            raise MatchResultConflictError("persisted result revision predecessor lineage is invalid")
        if predecessor.submission_status not in {
            MatchResultSubmissionStatus.SUPERSEDED.value,
            MatchResultSubmissionStatus.REJECTED.value,
        }:
            raise MatchResultConflictError("persisted predecessor result revision state is invalid")
    if history and history[-1].submission_status == MatchResultSubmissionStatus.SUPERSEDED.value:
        raise MatchResultConflictError("latest result revision cannot be superseded")
    return history


def _load_publication(
    session: Session,
    race_id: int,
    *,
    lock: bool,
) -> MatchResultPublication | None:
    query = (
        select(MatchResultPublication)
        .where(MatchResultPublication.race_id == race_id)
        .order_by(MatchResultPublication.id)
    )
    if lock:
        query = query.with_for_update()
    publication = session.scalar(query)
    if publication is not None:
        _validate_publication_row(publication)
    return publication


def _load_entries(session: Session, race_id: int, *, lock: bool) -> list[RaceEntry]:
    query = (
        select(RaceEntry)
        .where(RaceEntry.race_id == race_id, RaceEntry.entry_kind == "room_match")
        .order_by(RaceEntry.entry_number, RaceEntry.id)
    )
    if lock:
        query = query.with_for_update()
    return list(session.scalars(query))


def _load_race_results(session: Session, race_id: int, *, lock: bool) -> list[RaceResult]:
    query = select(RaceResult).where(RaceResult.race_id == race_id).order_by(RaceResult.entry_number, RaceResult.id)
    if lock:
        query = query.with_for_update()
    return list(session.scalars(query))


def _load_audit(session: Session, request_key: str, *, lock: bool = False) -> RaceOperationAudit | None:
    query = select(RaceOperationAudit).where(RaceOperationAudit.idempotency_key == request_key)
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


def _ensure_match_entry_kind_exclusive(session: Session, race_id: int) -> None:
    incompatible = session.scalar(
        select(RaceEntry.id)
        .where(RaceEntry.race_id == race_id, RaceEntry.entry_kind != "room_match")
        .order_by(RaceEntry.id)
        .limit(1)
    )
    if incompatible is not None:
        raise MatchResultConflictError("room-match race contains an incompatible final entry snapshot")


def _validate_confirmed_result_set(
    session: Session,
    *,
    race: Race,
    submission: MatchResultSubmission,
    entries: list[RaceEntry],
    lock: bool,
) -> list[RaceResult]:
    results = _load_race_results(session, race.id, lock=lock)
    expected = _submission_results(submission)
    validate_complete_result(expected, final_entry_numbers=[entry.entry_number for entry in entries])
    if [(row.entry_number, row.rank) for row in results] != [(item.entry_number, item.rank) for item in expected]:
        raise MatchResultConflictError("confirmed result rows do not match the reviewed result revision")
    entry_by_number = {entry.entry_number: entry for entry in entries}
    expected_by_entry_number = {item.entry_number: item for item in expected}
    result_ids = [row.id for row in results]
    provenance_query = (
        select(MatchResultProvenance)
        .where(MatchResultProvenance.race_result_id.in_(result_ids))
        .order_by(MatchResultProvenance.race_result_id)
    )
    if lock:
        provenance_query = provenance_query.with_for_update()
    provenance = list(session.scalars(provenance_query)) if result_ids else []
    provenance_by_result = {item.race_result_id: item for item in provenance}
    if len(provenance_by_result) != len(results):
        raise MatchResultConflictError("confirmed result provenance is incomplete")
    for row in results:
        entry = entry_by_number.get(row.entry_number)
        source = provenance_by_result.get(row.id)
        source_result = expected_by_entry_number.get(row.entry_number)
        if (
            entry is None
            or source is None
            or source_result is None
            or source.match_result_submission_id != submission.id
            or row.game_account_id != entry.game_account_id
            or row.owner_at_event_persona_id != entry.owner_at_event_persona_id
            or row.character_name != entry.horse_name_or_label
            or row.character_evaluation_rank != source_result.character_evaluation_rank
            or row.popularity_rank != source_result.popularity_rank
            or row.finish_time_ms != source_result.finish_time_ms
            or row.finish_margin_text != source_result.finish_margin_text
            or row.converted_rank is not None
            or row.is_betting_excluded
            or row.is_rating_excluded
            or row.is_result_void
            or row.source_import_record_id is not None
        ):
            raise MatchResultConflictError("confirmed native result snapshot is inconsistent")
    return results


def _validate_submission_row(submission: MatchResultSubmission) -> None:
    review_pair = (submission.reviewed_at is None) == (submission.reviewed_by_discord_user_id is None)
    rejection_pair = (
        (submission.rejected_at is None)
        == (submission.rejected_by_discord_user_id is None)
        == (submission.rejection_reason is None)
    )
    confirmation_pair = (submission.confirmed_at is None) == (submission.confirmed_by_discord_user_id is None)
    if not review_pair or not rejection_pair or not confirmation_pair:
        raise MatchResultConflictError("persisted result revision metadata is inconsistent")
    validate_submission_state(
        submission_status=submission.submission_status,
        is_current=submission.current_marker == "current",
        has_review_metadata=submission.reviewed_at is not None,
        has_rejection_metadata=submission.rejected_at is not None,
        has_confirmation_metadata=submission.confirmed_at is not None,
    )
    if submission.race_id is None or submission.revision_number <= 0:
        raise MatchResultConflictError("persisted result revision identity is invalid")
    normalize_match_type(submission.match_type)
    _submission_results(submission)


def _validate_publication_row(publication: MatchResultPublication) -> None:
    try:
        status = MatchResultPublicationStatus(publication.status)
    except ValueError as exc:
        raise MatchResultConflictError("persisted publication status is invalid") from exc
    if publication.attempt_count < 0:
        raise MatchResultConflictError("persisted publication attempt count is invalid")
    if status is MatchResultPublicationStatus.PENDING:
        valid = (
            publication.discord_message_id is None
            and publication.last_error_code is None
            and publication.published_at is None
        )
    elif status is MatchResultPublicationStatus.SENT:
        valid = (
            publication.discord_message_id is not None
            and publication.last_error_code is None
            and publication.published_at is not None
        )
    else:
        valid = (
            publication.discord_message_id is None
            and publication.last_error_code is not None
            and publication.published_at is None
        )
    if not valid:
        raise MatchResultConflictError("persisted publication state is inconsistent")


def _state_json(
    session: Session,
    race: Race,
    *,
    revision_number: int | None = None,
) -> dict[str, object]:
    submission = (
        _load_submission(session, race.id, revision_number, lock=False)
        if revision_number is not None
        else _load_latest_submission(session, race.id, lock=False)
    )
    publication = _load_publication(session, race.id, lock=False)
    if (
        race.status == MatchRaceStatus.RESULT_CONFIRMED.value
        and submission is not None
        and submission.submission_status == MatchResultSubmissionStatus.CONFIRMED.value
    ):
        entries = _load_entries(session, race.id, lock=False)
        result_rows = _validate_confirmed_result_set(
            session,
            race=race,
            submission=submission,
            entries=entries,
            lock=False,
        )
        results = [_confirmed_result_json(row) for row in result_rows]
    elif submission is not None:
        results = _result_input_json(_submission_results(submission))
    else:
        results = [_confirmed_result_json(row) for row in _load_race_results(session, race.id, lock=False)]
    return {
        "race": {
            "id": race.id,
            "event_id": race.event_id,
            "name": race.name,
            "status": race.status,
        },
        "submission": _submission_json(submission) if submission is not None else None,
        "results": results,
        "publication": _publication_json(publication) if publication is not None else None,
    }


def _result_from_state(
    session: Session,
    *,
    action: str,
    audit_id: int,
    race: Race,
    revision_number: int | None = None,
) -> MatchResultOperationDTO:
    return _result_from_json(
        action=action,
        audit_id=audit_id,
        value=_state_json(session, race, revision_number=revision_number),
    )


def _result_from_json(
    *,
    action: str,
    audit_id: int,
    value: Mapping[str, Any],
) -> MatchResultOperationDTO:
    race = _mapping(value.get("race"), field="audit race")
    submission_value = value.get("submission")
    results_value = value.get("results")
    publication_value = value.get("publication")
    if not isinstance(results_value, list):
        raise MatchResultConflictError("stored room-match result operation snapshot is invalid")
    return MatchResultOperationDTO(
        action=action,
        audit_id=audit_id,
        race_id=_json_positive_int(race.get("id"), field="race ID"),
        event_id=_json_optional_positive_int(race.get("event_id"), field="event ID"),
        race_name=_json_text(race.get("name"), field="race name"),
        race_status=_json_text(race.get("status"), field="race status"),
        submission=_submission_from_json(submission_value) if submission_value is not None else None,
        results=tuple(_result_line_from_json(item) for item in results_value),
        publication=_publication_from_json(publication_value) if publication_value is not None else None,
    )


def _submission_json(submission: MatchResultSubmission) -> dict[str, object]:
    return {
        "id": submission.id,
        "revision_number": submission.revision_number,
        "supersedes_submission_id": submission.supersedes_submission_id,
        "match_type": submission.match_type,
        "status": submission.submission_status,
        "is_current": submission.current_marker == "current",
        "submitted_by_discord_user_id": submission.submitted_by_discord_user_id,
        "reviewed_by_discord_user_id": submission.reviewed_by_discord_user_id,
        "rejected_by_discord_user_id": submission.rejected_by_discord_user_id,
        "rejection_reason": submission.rejection_reason,
        "confirmed_by_discord_user_id": submission.confirmed_by_discord_user_id,
        "created_at": _datetime_json(submission.created_at),
        "reviewed_at": _datetime_json(submission.reviewed_at),
        "rejected_at": _datetime_json(submission.rejected_at),
        "confirmed_at": _datetime_json(submission.confirmed_at),
    }


def _submission_from_json(value: object) -> MatchResultSubmissionDTO:
    data = _mapping(value, field="audit submission")
    is_current = data.get("is_current")
    if not isinstance(is_current, bool):
        raise MatchResultConflictError("stored submission current marker is invalid")
    return MatchResultSubmissionDTO(
        id=_json_positive_int(data.get("id"), field="submission ID"),
        revision_number=_json_positive_int(data.get("revision_number"), field="revision number"),
        supersedes_submission_id=_json_optional_positive_int(
            data.get("supersedes_submission_id"), field="predecessor submission ID"
        ),
        match_type=_json_text(data.get("match_type"), field="match type"),
        status=_json_text(data.get("status"), field="submission status"),
        is_current=is_current,
        submitted_by_discord_user_id=_json_text(data.get("submitted_by_discord_user_id"), field="submitter ID"),
        reviewed_by_discord_user_id=_json_optional_text(data.get("reviewed_by_discord_user_id"), field="reviewer ID"),
        rejected_by_discord_user_id=_json_optional_text(data.get("rejected_by_discord_user_id"), field="rejector ID"),
        rejection_reason=_json_optional_text(data.get("rejection_reason"), field="rejection reason"),
        confirmed_by_discord_user_id=_json_optional_text(
            data.get("confirmed_by_discord_user_id"), field="confirmer ID"
        ),
        created_at=_datetime_from_json(data.get("created_at"), required=True),
        reviewed_at=_datetime_from_json(data.get("reviewed_at")),
        rejected_at=_datetime_from_json(data.get("rejected_at")),
        confirmed_at=_datetime_from_json(data.get("confirmed_at")),
    )


def _publication_json(publication: MatchResultPublication) -> dict[str, object]:
    return {
        "id": publication.id,
        "race_id": publication.race_id,
        "submission_id": publication.match_result_submission_id,
        "target_channel_id": publication.target_channel_id,
        "status": publication.status,
        "attempt_count": publication.attempt_count,
        "discord_message_id": publication.discord_message_id,
        "last_error_code": publication.last_error_code,
        "payload": publication.payload_json,
        "created_at": _datetime_json(publication.created_at),
        "updated_at": _datetime_json(publication.updated_at),
        "published_at": _datetime_json(publication.published_at),
    }


def _publication_from_json(value: object) -> MatchResultPublicationDTO:
    data = _mapping(value, field="audit publication")
    payload = _mapping(data.get("payload"), field="publication payload")
    return MatchResultPublicationDTO(
        id=_json_positive_int(data.get("id"), field="publication ID"),
        race_id=_json_positive_int(data.get("race_id"), field="publication race ID"),
        submission_id=_json_positive_int(data.get("submission_id"), field="publication submission ID"),
        target_channel_id=_json_text(data.get("target_channel_id"), field="target channel ID"),
        status=_json_text(data.get("status"), field="publication status"),
        attempt_count=_json_nonnegative_int(data.get("attempt_count"), field="attempt count"),
        discord_message_id=_json_optional_text(data.get("discord_message_id"), field="Discord message ID"),
        last_error_code=_json_optional_text(data.get("last_error_code"), field="publication error code"),
        payload=_publication_payload_from_json(payload),
        created_at=_datetime_from_json(data.get("created_at"), required=True),
        updated_at=_datetime_from_json(data.get("updated_at"), required=True),
        published_at=_datetime_from_json(data.get("published_at")),
    )


def _publication_payload_from_json(value: Mapping[str, Any]) -> MatchResultPublicationPayloadDTO:
    race = _mapping(value.get("race"), field="publication payload race")
    submission = _mapping(value.get("submission"), field="publication payload submission")
    results = value.get("results")
    if not isinstance(results, list):
        raise MatchResultConflictError("stored publication payload results are invalid")
    lines: list[MatchResultPublicationLineDTO] = []
    for item in results:
        line = _mapping(item, field="publication payload result")
        lines.append(
            MatchResultPublicationLineDTO(
                entry_number=_json_positive_int(line.get("entry_number"), field="entry number"),
                rank=_json_positive_int(line.get("rank"), field="rank"),
                display_name=_json_optional_text(line.get("display_name"), field="display name"),
                game_account_id=_json_optional_positive_int(line.get("game_account_id"), field="game account ID"),
                character_name=_json_optional_text(line.get("character_name"), field="character name"),
                character_evaluation_rank=_json_optional_text(
                    line.get("character_evaluation_rank"),
                    field="character evaluation rank",
                ),
                popularity_rank=_json_optional_positive_int(
                    line.get("popularity_rank"),
                    field="popularity rank",
                ),
                finish_time_ms=_json_optional_positive_int(
                    line.get("finish_time_ms"),
                    field="finish time milliseconds",
                ),
                finish_margin_text=_json_optional_text(
                    line.get("finish_margin_text"),
                    field="finish margin",
                ),
            )
        )
    return MatchResultPublicationPayloadDTO(
        race_id=_json_positive_int(race.get("id"), field="publication race ID"),
        race_name=_json_text(race.get("name"), field="publication race name"),
        submission_id=_json_positive_int(
            submission.get("id"),
            field="publication submission ID",
        ),
        revision_number=_json_positive_int(
            submission.get("revision_number"),
            field="publication revision number",
        ),
        match_type=_json_text(submission.get("match_type"), field="publication match type"),
        results=tuple(lines),
    )


def _result_line_from_json(value: object) -> MatchResultLineDTO:
    data = _mapping(value, field="audit result line")
    return MatchResultLineDTO(
        entry_number=_json_positive_int(data.get("entry_number"), field="entry number"),
        rank=_json_positive_int(data.get("rank"), field="rank"),
        game_account_id=_json_optional_positive_int(data.get("game_account_id"), field="game account ID"),
        character_name=_json_optional_text(data.get("character_name"), field="character name"),
        character_evaluation_rank=_json_optional_text(
            data.get("character_evaluation_rank"),
            field="character evaluation rank",
        ),
        popularity_rank=_json_optional_positive_int(data.get("popularity_rank"), field="popularity rank"),
        finish_time_ms=_json_optional_positive_int(
            data.get("finish_time_ms"),
            field="finish time milliseconds",
        ),
        finish_margin_text=_json_optional_text(data.get("finish_margin_text"), field="finish margin"),
    )


def _publication_payload(
    race: Race,
    submission: MatchResultSubmission,
    entries: list[RaceEntry],
    results: list[RaceResult],
) -> dict[str, object]:
    entry_by_number = {entry.entry_number: entry for entry in entries}
    return {
        "race": {"id": race.id, "name": race.name},
        "submission": {
            "id": submission.id,
            "revision_number": submission.revision_number,
            "match_type": submission.match_type,
        },
        "results": [
            {
                "entry_number": result.entry_number,
                "rank": result.rank,
                "display_name": entry_by_number[result.entry_number].player_name,
                "game_account_id": result.game_account_id,
                "character_name": result.character_name,
                "character_evaluation_rank": result.character_evaluation_rank,
                "popularity_rank": result.popularity_rank,
                "finish_time_ms": result.finish_time_ms,
                "finish_margin_text": result.finish_margin_text,
            }
            for result in sorted(results, key=lambda item: item.rank)
        ],
    }


def _confirmed_result_json(result: RaceResult) -> dict[str, object]:
    return {
        "entry_number": result.entry_number,
        "rank": result.rank,
        "game_account_id": result.game_account_id,
        "owner_at_event_persona_id": result.owner_at_event_persona_id,
        "character_name": result.character_name,
        "character_evaluation_rank": result.character_evaluation_rank,
        "popularity_rank": result.popularity_rank,
        "finish_time_ms": result.finish_time_ms,
        "finish_margin_text": result.finish_margin_text,
    }


def _submission_results(submission: MatchResultSubmission) -> tuple[MatchResultInput, ...]:
    raw = submission.raw_input_json
    if not isinstance(raw, Mapping):
        raise MatchResultConflictError("persisted result revision payload is invalid")
    if set(raw) != {"match_type", "results"} or raw.get("match_type") != submission.match_type:
        raise MatchResultConflictError("persisted result revision payload is invalid")
    values = raw.get("results")
    if not isinstance(values, list):
        raise MatchResultConflictError("persisted result revision payload is invalid")
    try:
        parsed_values: list[MatchResultInput] = []
        for item in values:
            line = _mapping(item, field="result payload line")
            legacy_keys = {"entry_number", "rank"}
            detail_keys = legacy_keys | {
                "character_evaluation_rank",
                "popularity_rank",
                "finish_time_ms",
                "finish_margin_text",
            }
            if set(line) not in (legacy_keys, detail_keys):
                raise MatchResultConflictError("persisted result revision payload is invalid")
            parsed_values.append(
                MatchResultInput(
                    entry_number=_json_positive_int(line.get("entry_number"), field="entry number"),
                    rank=_json_positive_int(line.get("rank"), field="rank"),
                    character_evaluation_rank=_json_optional_text(
                        line.get("character_evaluation_rank"),
                        field="character evaluation rank",
                    ),
                    popularity_rank=_json_optional_positive_int(
                        line.get("popularity_rank"),
                        field="popularity rank",
                    ),
                    finish_time_ms=_json_optional_positive_int(
                        line.get("finish_time_ms"),
                        field="finish time milliseconds",
                    ),
                    finish_margin_text=_json_optional_text(
                        line.get("finish_margin_text"),
                        field="finish margin",
                    ),
                )
            )
        parsed = tuple(parsed_values)
        return normalize_result_input(parsed)
    except MatchResultError as exc:
        raise MatchResultConflictError("persisted result revision payload is invalid") from exc


def _raw_input_json(
    match_type: str,
    results: tuple[MatchResultInput, ...],
) -> dict[str, object]:
    return {"match_type": match_type, "results": _result_input_json(results)}


def _result_input_json(results: tuple[MatchResultInput, ...]) -> list[dict[str, object]]:
    return [asdict(item) for item in results]


def _normalize_revision_command(
    command: ReviewMatchResultCommand
    | CorrectMatchResultCommand
    | RejectMatchResultCommand
    | ConfirmMatchResultCommand,
    *,
    require_reason: bool = False,
) -> tuple[int, int, str, str, str | None]:
    reason = _optional_text(command.reason, field="reason", maximum=255)
    if require_reason and reason is None:
        raise MatchResultError("rejection reason is required")
    return (
        _positive_int(command.race_id, field="race ID"),
        _positive_int(command.revision_number, field="revision number"),
        _text(command.actor_discord_user_id, field="actor ID", maximum=32),
        _text(command.idempotency_key, field="idempotency key", maximum=128),
        reason,
    )


def _fingerprint(action: str, payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        {"action": action, "payload": payload},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _text(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise MatchResultError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or not normalized.isprintable():
        raise MatchResultError(f"{field} must contain 1 to {maximum} printable characters")
    return normalized


def _optional_text(value: str | None, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, field=field, maximum=maximum)


def _error_code(value: str) -> str:
    normalized = _text(value, field="publication error code", maximum=64)
    if not all(character.isupper() or character.isdigit() or character == "_" for character in normalized):
        raise MatchResultError("publication error code must use uppercase letters, digits, or underscores")
    return normalized


def _discord_snowflake(value: str, *, field: str) -> str:
    normalized = _text(value, field=field, maximum=32)
    if not normalized.isascii() or not normalized.isdigit() or int(normalized) <= 0:
        raise MatchResultError(f"{field} must be a positive decimal Discord snowflake")
    return normalized


def _positive_int(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MatchResultError(f"{field} must be a positive integer")
    return value


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MatchResultConflictError(f"stored {field} is invalid")
    return value


def _json_text(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise MatchResultConflictError(f"stored {field} is invalid")
    return value


def _json_optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _json_text(value, field=field)


def _json_positive_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MatchResultConflictError(f"stored {field} is invalid")
    return value


def _json_nonnegative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MatchResultConflictError(f"stored {field} is invalid")
    return value


def _json_optional_positive_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    return _json_positive_int(value, field=field)


def _datetime_json(value: datetime | None) -> str | None:
    if value is None:
        return None
    return database_datetime_as_utc(value).isoformat()


def _datetime_from_json(value: object, *, required: bool = False) -> datetime | None:
    if value is None:
        if required:
            raise MatchResultConflictError("stored result timestamp is missing")
        return None
    if not isinstance(value, str):
        raise MatchResultConflictError("stored result timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MatchResultConflictError("stored result timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MatchResultConflictError("stored result timestamp is invalid")
    return parsed.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)
