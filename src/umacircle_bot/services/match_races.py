import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Bet,
    DiscordAccount,
    GameAccount,
    Race,
    RaceCondition,
    RaceEntry,
    RaceOperationAudit,
    RaceResult,
)
from umacircle_bot.domain.errors import MatchRaceError, RaceMutationConflictError
from umacircle_bot.domain.identity import validate_registration_pid
from umacircle_bot.domain.races import (
    MatchRaceSnapshot,
    MatchRaceStatus,
    normalize_match_race_status,
    validate_betting_close,
    validate_betting_open,
    validate_setup_mutation,
)
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.services.dtos import (
    MatchEntrySnapshotDTO,
    MatchRaceDTO,
    RaceConditionDTO,
    RaceOperationResultDTO,
)
from umacircle_bot.services.match_rating import normalize_rating_grade

RACE_MANAGE_CAPABILITY = "race.manage"
ENTRY_MANAGE_CAPABILITY = "race.entry.manage"
BETTING_CONTROL_CAPABILITY = "race.betting.open_close"


@dataclass(frozen=True, slots=True)
class CreateMatchRaceCommand:
    name: str
    starts_at: datetime
    actor_discord_user_id: str
    idempotency_key: str
    event_id: int | None = None
    description: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class EditMatchRaceCommand:
    race_id: int
    name: str
    starts_at: datetime
    actor_discord_user_id: str
    idempotency_key: str
    event_id: int | None = None
    description: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SetRaceConditionCommand:
    race_id: int
    grade: str
    venue: str
    track_surface: str
    distance: int
    direction: str
    season: str
    weather: str
    track_condition: str
    condition_label: str
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class FinalEntrySnapshotInput:
    entry_number: int
    display_name: str
    game_account_id: int | None = None
    character_name: str | None = None


@dataclass(frozen=True, slots=True)
class ReplaceFinalEntrySnapshotCommand:
    race_id: int
    entries: tuple[FinalEntrySnapshotInput, ...]
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None
    request_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class RegisteredFinalEntrySnapshotInput:
    uma_pid: str
    character_name: str


@dataclass(frozen=True, slots=True)
class ReplaceRegisteredFinalEntrySnapshotCommand:
    race_id: int
    entries: tuple[RegisteredFinalEntrySnapshotInput, ...]
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class OpenMatchBettingCommand:
    race_id: int
    as_of: datetime
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CloseMatchBettingCommand:
    race_id: int
    as_of: datetime
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


def create_match_race(
    session: Session,
    *,
    command: CreateMatchRaceCommand,
) -> RaceOperationResultDTO:
    action = "room_race_create"
    name = _text(command.name, field="race name", maximum=200)
    description = _optional_text(command.description, field="race description", maximum=10_000)
    starts_at = _datetime(command.starts_at, field="race start time")
    event_id = _optional_positive_int(command.event_id, field="event ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    payload = {
        "name": name,
        "description": description,
        "starts_at": _datetime_json(starts_at),
        "event_id": event_id,
        "actor": actor,
    }
    fingerprint = _fingerprint(action, payload)

    try:
        with session.begin_nested():
            existing = _load_audit(session, request_key)
            if existing is not None:
                return _idempotent_result(existing, action=action, fingerprint=fingerprint)
            race = Race(
                event_id=event_id,
                race_kind="room_match",
                name=name,
                description=description,
                starts_at=starts_at,
                status=MatchRaceStatus.SETUP.value,
            )
            session.add(race)
            session.flush()
            audit = _append_audit(
                session,
                race=race,
                action=action,
                capability=RACE_MANAGE_CAPABILITY,
                actor=actor,
                request_key=request_key,
                fingerprint=fingerprint,
                before=None,
                reason=reason,
            )
            return _result(session, race=race, audit=audit)
    except IntegrityError as exc:
        return _recover_concurrent_retry(
            session,
            error=exc,
            request_key=request_key,
            action=action,
            fingerprint=fingerprint,
        )


def edit_match_race(
    session: Session,
    *,
    command: EditMatchRaceCommand,
) -> RaceOperationResultDTO:
    action = "room_race_edit"
    race_id = _positive_int(command.race_id, field="race ID")
    name = _text(command.name, field="race name", maximum=200)
    description = _optional_text(command.description, field="race description", maximum=10_000)
    starts_at = _datetime(command.starts_at, field="race start time")
    event_id = _optional_positive_int(command.event_id, field="event ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    values = {
        "name": name,
        "description": description,
        "starts_at": _datetime_json(starts_at),
        "event_id": event_id,
    }
    fingerprint = _fingerprint(action, {"race_id": race_id, "actor": actor, **values})

    def mutate(race: Race) -> dict[str, object] | None:
        validate_setup_mutation(status=race.status, has_results=_has_results(session, race.id))
        before = _race_json(race, entry_count=len(_load_entries(session, race.id)))
        race.name = name
        race.description = description
        race.starts_at = starts_at
        race.event_id = event_id
        session.flush()
        return before

    return _execute_race_operation(
        session,
        race_id=race_id,
        action=action,
        capability=RACE_MANAGE_CAPABILITY,
        actor=actor,
        request_key=request_key,
        fingerprint=fingerprint,
        reason=reason,
        mutate=mutate,
    )


def set_match_condition(
    session: Session,
    *,
    command: SetRaceConditionCommand,
) -> RaceOperationResultDTO:
    action = "room_race_condition_set"
    race_id = _positive_int(command.race_id, field="race ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    values = {
        "grade": normalize_rating_grade(_text(command.grade, field="grade", maximum=16)),
        "venue": _text(command.venue, field="venue", maximum=64),
        "track_surface": _text(command.track_surface, field="track surface", maximum=32),
        "distance": _positive_int(command.distance, field="distance"),
        "direction": _text(command.direction, field="direction", maximum=16),
        "season": _text(command.season, field="season", maximum=16),
        "weather": _text(command.weather, field="weather", maximum=32),
        "track_condition": _text(command.track_condition, field="track condition", maximum=32),
        "condition_label": _text(command.condition_label, field="condition label", maximum=32),
    }
    fingerprint = _fingerprint(action, {"race_id": race_id, "actor": actor, **values})

    def mutate(race: Race) -> dict[str, object] | None:
        validate_setup_mutation(status=race.status, has_results=_has_results(session, race.id))
        values["participant_count"] = len(_load_entries(session, race.id))
        condition = session.scalar(select(RaceCondition).where(RaceCondition.race_id == race.id))
        before = _condition_json(condition) if condition is not None else None
        if condition is None:
            condition = RaceCondition(race_id=race.id, **values)
            session.add(condition)
        else:
            for field, value in values.items():
                setattr(condition, field, value)
        session.flush()
        return before

    return _execute_race_operation(
        session,
        race_id=race_id,
        action=action,
        capability=RACE_MANAGE_CAPABILITY,
        actor=actor,
        request_key=request_key,
        fingerprint=fingerprint,
        reason=reason,
        mutate=mutate,
    )


def replace_final_entry_snapshot(
    session: Session,
    *,
    command: ReplaceFinalEntrySnapshotCommand,
) -> RaceOperationResultDTO:
    action = "room_race_entries_set"
    race_id = _positive_int(command.race_id, field="race ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    entries = _normalize_entries(command.entries)
    payload = {
        "race_id": race_id,
        "actor": actor,
        "entries": [asdict(entry) for entry in entries],
    }
    fingerprint = command.request_fingerprint or _fingerprint(action, payload)

    def mutate(race: Race) -> dict[str, object] | None:
        has_results = _has_results(session, race.id)
        validate_setup_mutation(status=race.status, has_results=has_results)
        if _has_bets(session, race.id):
            raise RaceMutationConflictError("race entries with existing bets cannot be replaced")
        linked_ids = sorted({entry.game_account_id for entry in entries if entry.game_account_id is not None})
        owner_by_account_id: dict[int, str | None] = {}
        if linked_ids:
            account_owners = session.execute(
                select(GameAccount.id, GameAccount.persona_id)
                .where(GameAccount.id.in_(linked_ids))
                .order_by(GameAccount.id)
                .with_for_update()
            )
            for account_id, persona_id in account_owners:
                owner_by_account_id[account_id] = persona_id
            if set(linked_ids) != set(owner_by_account_id):
                raise MatchRaceError("linked game account does not exist")
        current = _load_entries(session, race.id)
        condition = session.scalar(select(RaceCondition).where(RaceCondition.race_id == race.id))
        before = {
            "entries": [_entry_json(entry) for entry in current],
            "condition_participant_count": condition.participant_count if condition is not None else None,
        }
        session.execute(
            delete(RaceEntry).where(
                RaceEntry.race_id == race.id,
                RaceEntry.entry_kind == "room_match",
            )
        )
        session.add_all(
            [
                RaceEntry(
                    race_id=race.id,
                    entry_number=entry.entry_number,
                    entry_kind="room_match",
                    entry_number_source="declared",
                    game_account_id=entry.game_account_id,
                    owner_at_event_persona_id=(
                        owner_by_account_id[entry.game_account_id] if entry.game_account_id is not None else None
                    ),
                    player_name=entry.display_name,
                    horse_name_or_label=entry.character_name,
                )
                for entry in entries
            ]
        )
        session.flush()
        if condition is not None:
            condition.participant_count = len(entries)
            session.flush()
        return before

    return _execute_race_operation(
        session,
        race_id=race_id,
        action=action,
        capability=ENTRY_MANAGE_CAPABILITY,
        actor=actor,
        request_key=request_key,
        fingerprint=fingerprint,
        reason=reason,
        mutate=mutate,
    )


def replace_registered_final_entry_snapshot(
    session: Session,
    *,
    command: ReplaceRegisteredFinalEntrySnapshotCommand,
) -> RaceOperationResultDTO:
    race_id = _positive_int(command.race_id, field="race ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    if not command.entries:
        raise MatchRaceError("registered entry snapshot must contain at least one entry")

    normalized = tuple(
        RegisteredFinalEntrySnapshotInput(
            uma_pid=validate_registration_pid(entry.uma_pid),
            character_name=_text(entry.character_name, field="horse name", maximum=100),
        )
        for entry in command.entries
    )
    pids = [entry.uma_pid for entry in normalized]
    if len(pids) != len(set(pids)):
        raise MatchRaceError("entry snapshot contains duplicate PID")

    fingerprint = _fingerprint(
        "room_race_entries_set",
        {
            "race_id": race_id,
            "actor": actor,
            "entries": [asdict(entry) for entry in normalized],
        },
    )
    existing = _load_audit(session, request_key)
    if existing is not None:
        return _idempotent_result(existing, action="room_race_entries_set", fingerprint=fingerprint)

    rows = session.execute(
        select(GameAccount.id, GameAccount.uma_pid, GameAccount.nickname, DiscordAccount.discord_nickname)
        .join(DiscordAccount, DiscordAccount.id == GameAccount.discord_account_id)
        .where(GameAccount.uma_pid.in_(pids))
        .order_by(GameAccount.id)
    ).all()
    accounts_by_pid = {row.uma_pid: row for row in rows}
    missing_pids = [pid for pid in pids if pid not in accounts_by_pid]
    if missing_pids:
        raise MatchRaceError("registered game account not found for every PID")

    entries = tuple(
        FinalEntrySnapshotInput(
            entry_number=index,
            display_name=accounts_by_pid[entry.uma_pid].nickname or accounts_by_pid[entry.uma_pid].discord_nickname,
            game_account_id=accounts_by_pid[entry.uma_pid].id,
            character_name=entry.character_name,
        )
        for index, entry in enumerate(normalized, start=1)
    )
    return replace_final_entry_snapshot(
        session,
        command=ReplaceFinalEntrySnapshotCommand(
            race_id=race_id,
            entries=entries,
            actor_discord_user_id=actor,
            idempotency_key=request_key,
            reason=reason,
            request_fingerprint=fingerprint,
        ),
    )


def open_match_betting(
    session: Session,
    *,
    command: OpenMatchBettingCommand,
) -> RaceOperationResultDTO:
    action = "room_race_betting_open"
    race_id = _positive_int(command.race_id, field="race ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    as_of = _datetime(command.as_of, field="open time")
    payload = {"race_id": race_id, "actor": actor}
    fingerprint = _fingerprint(action, payload)

    def mutate(race: Race) -> dict[str, object] | None:
        entries = _load_entries(session, race.id)
        condition = session.scalar(select(RaceCondition).where(RaceCondition.race_id == race.id))
        if condition is not None and condition.participant_count != len(entries):
            raise RaceMutationConflictError("race condition participant count does not match final entries")
        if condition is not None and condition.grade != "OP" and len(entries) < 2:
            raise RaceMutationConflictError("rated race betting requires at least two final entries")
        validate_betting_open(
            MatchRaceSnapshot(
                race_kind=race.race_kind,
                status=normalize_match_race_status(race.status),
                starts_at=database_datetime_as_utc(race.starts_at) if race.starts_at is not None else None,
                has_condition=condition is not None,
                entry_count=len(entries),
                has_results=_has_results(session, race.id),
            )
        )
        before = _race_json(race, entry_count=len(entries))
        race.status = MatchRaceStatus.BETTING_OPEN.value
        race.betting_opened_at = as_of
        race.betting_closed_at = None
        session.flush()
        return before

    return _execute_race_operation(
        session,
        race_id=race_id,
        action=action,
        capability=BETTING_CONTROL_CAPABILITY,
        actor=actor,
        request_key=request_key,
        fingerprint=fingerprint,
        reason=reason,
        mutate=mutate,
    )


def close_match_betting(
    session: Session,
    *,
    command: CloseMatchBettingCommand,
) -> RaceOperationResultDTO:
    action = "room_race_betting_close"
    race_id = _positive_int(command.race_id, field="race ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    as_of = _datetime(command.as_of, field="close time")
    fingerprint = _fingerprint(action, {"race_id": race_id, "actor": actor})

    def mutate(race: Race) -> dict[str, object] | None:
        validate_betting_close(
            status=race.status,
            betting_opened_at=(
                database_datetime_as_utc(race.betting_opened_at) if race.betting_opened_at is not None else None
            ),
            as_of=as_of,
        )
        before = _race_json(race, entry_count=len(_load_entries(session, race.id)))
        race.status = MatchRaceStatus.BETTING_CLOSED.value
        race.betting_closed_at = as_of
        session.flush()
        return before

    return _execute_race_operation(
        session,
        race_id=race_id,
        action=action,
        capability=BETTING_CONTROL_CAPABILITY,
        actor=actor,
        request_key=request_key,
        fingerprint=fingerprint,
        reason=reason,
        mutate=mutate,
    )


def get_match_race(session: Session, *, race_id: int) -> RaceOperationResultDTO:
    normalized_id = _positive_int(race_id, field="race ID")
    race = _load_race(session, normalized_id, lock=False)
    _ensure_match_entry_kind_exclusive(session, race.id)
    return _result(session, race=race, audit_id=0, action="room_race_show")


def _execute_race_operation(
    session: Session,
    *,
    race_id: int,
    action: str,
    capability: str,
    actor: str,
    request_key: str,
    fingerprint: str,
    reason: str | None,
    mutate,
) -> RaceOperationResultDTO:
    try:
        with session.begin_nested():
            race = _load_race(session, race_id, lock=True)
            audit = _load_audit(session, request_key)
            if audit is not None:
                return _idempotent_result(audit, action=action, fingerprint=fingerprint)
            if race.external_source is not None:
                raise RaceMutationConflictError("imported historical races are read-only")
            _ensure_match_entry_kind_exclusive(session, race.id)
            before = mutate(race)
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
            return _result(session, race=race, audit=audit)
    except IntegrityError as exc:
        return _recover_concurrent_retry(
            session,
            error=exc,
            request_key=request_key,
            action=action,
            fingerprint=fingerprint,
        )


def _append_audit(
    session: Session,
    *,
    race: Race,
    action: str,
    capability: str,
    actor: str,
    request_key: str,
    fingerprint: str,
    before: dict[str, object] | None,
    reason: str | None,
) -> RaceOperationAudit:
    after = _state_json(session, race)
    audit = RaceOperationAudit(
        race_id=race.id,
        action=action,
        capability=capability,
        actor_discord_user_id=actor,
        idempotency_key=request_key,
        request_fingerprint=fingerprint,
        before_json=before,
        after_json=after,
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
) -> RaceOperationResultDTO:
    with session.begin_nested():
        audit = _load_audit(session, request_key, lock=True)
        if audit is None:
            raise error
        return _idempotent_result(audit, action=action, fingerprint=fingerprint)


def _idempotent_result(
    audit: RaceOperationAudit,
    *,
    action: str,
    fingerprint: str,
) -> RaceOperationResultDTO:
    if audit.action != action or audit.request_fingerprint != fingerprint:
        raise RaceMutationConflictError("idempotency key payload does not match the original race operation")
    return _result_from_json(action=action, audit_id=audit.id, value=audit.after_json)


def _load_race(session: Session, race_id: int, *, lock: bool) -> Race:
    query = select(Race).where(Race.id == race_id).order_by(Race.id)
    if lock:
        query = query.with_for_update()
    race = session.scalar(query)
    if race is None or race.race_kind != "room_match":
        raise MatchRaceError("room-match race not found")
    return race


def _load_audit(session: Session, request_key: str, *, lock: bool = False) -> RaceOperationAudit | None:
    query = select(RaceOperationAudit).where(RaceOperationAudit.idempotency_key == request_key)
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


def _load_entries(session: Session, race_id: int) -> list[RaceEntry]:
    return list(
        session.scalars(
            select(RaceEntry)
            .where(RaceEntry.race_id == race_id, RaceEntry.entry_kind == "room_match")
            .order_by(RaceEntry.entry_number)
        )
    )


def _ensure_match_entry_kind_exclusive(session: Session, race_id: int) -> None:
    incompatible_entry = session.scalar(
        select(RaceEntry.id)
        .where(
            RaceEntry.race_id == race_id,
            RaceEntry.entry_kind != "room_match",
        )
        .order_by(RaceEntry.id)
        .limit(1)
    )
    if incompatible_entry is not None:
        raise RaceMutationConflictError("room-match race contains an incompatible final entry snapshot")


def _has_bets(session: Session, race_id: int) -> bool:
    return session.scalar(select(Bet.id).where(Bet.race_id == race_id).order_by(Bet.id).limit(1)) is not None


def _has_results(session: Session, race_id: int) -> bool:
    return (
        session.scalar(
            select(RaceResult.id)
            .where(RaceResult.race_id == race_id)
            .order_by(RaceResult.race_id, RaceResult.entry_number)
            .limit(1)
        )
        is not None
    )


def _result(
    session: Session,
    *,
    race: Race,
    audit: RaceOperationAudit | None = None,
    audit_id: int | None = None,
    action: str | None = None,
) -> RaceOperationResultDTO:
    entries = tuple(_entry_dto(entry) for entry in _load_entries(session, race.id))
    condition = session.scalar(select(RaceCondition).where(RaceCondition.race_id == race.id))
    return RaceOperationResultDTO(
        action=action or (audit.action if audit is not None else "room_race_show"),
        audit_id=audit.id if audit is not None else (audit_id or 0),
        race=_race_dto(race, entry_count=len(entries)),
        condition=_condition_dto(condition) if condition is not None else None,
        entries=entries,
    )


def _state_json(session: Session, race: Race) -> dict[str, object]:
    entries = _load_entries(session, race.id)
    condition = session.scalar(select(RaceCondition).where(RaceCondition.race_id == race.id))
    return {
        "race": _race_json(race, entry_count=len(entries)),
        "condition": _condition_json(condition) if condition is not None else None,
        "entries": [_entry_json(entry) for entry in entries],
    }


def _result_from_json(*, action: str, audit_id: int, value: Mapping[str, Any]) -> RaceOperationResultDTO:
    race = _mapping(value.get("race"), field="audit race")
    condition_value = value.get("condition")
    entries_value = value.get("entries")
    if not isinstance(entries_value, list):
        raise MatchRaceError("stored race operation result is invalid")
    return RaceOperationResultDTO(
        action=action,
        audit_id=audit_id,
        race=MatchRaceDTO(
            id=_json_int(race.get("id"), field="race ID"),
            event_id=_json_optional_int(race.get("event_id"), field="event ID"),
            name=_json_text(race.get("name"), field="race name"),
            description=_json_optional_text(race.get("description"), field="race description"),
            starts_at=_datetime_from_json(race.get("starts_at")),
            status=_json_text(race.get("status"), field="race status"),
            betting_window_version=_json_int(
                race.get("betting_window_version"),
                field="betting window version",
            ),
            betting_opened_at=_datetime_from_json(race.get("betting_opened_at")),
            betting_closed_at=_datetime_from_json(race.get("betting_closed_at")),
            entry_count=_json_int(race.get("entry_count"), field="entry count", allow_zero=True),
        ),
        condition=_condition_from_json(condition_value) if condition_value is not None else None,
        entries=tuple(_entry_from_json(item) for item in entries_value),
    )


def _race_dto(race: Race, *, entry_count: int) -> MatchRaceDTO:
    return MatchRaceDTO(
        id=race.id,
        event_id=race.event_id,
        name=race.name,
        description=race.description,
        starts_at=database_datetime_as_utc(race.starts_at) if race.starts_at is not None else None,
        status=race.status,
        betting_window_version=race.betting_window_version,
        betting_opened_at=(
            database_datetime_as_utc(race.betting_opened_at) if race.betting_opened_at is not None else None
        ),
        betting_closed_at=(
            database_datetime_as_utc(race.betting_closed_at) if race.betting_closed_at is not None else None
        ),
        entry_count=entry_count,
    )


def _condition_dto(condition: RaceCondition) -> RaceConditionDTO:
    return RaceConditionDTO(
        grade=condition.grade,
        venue=condition.venue,
        track_surface=condition.track_surface,
        distance=condition.distance,
        direction=condition.direction,
        season=condition.season,
        weather=condition.weather,
        track_condition=condition.track_condition,
        condition_label=condition.condition_label,
        participant_count=condition.participant_count,
    )


def _entry_dto(entry: RaceEntry) -> MatchEntrySnapshotDTO:
    if entry.player_name is None:
        raise MatchRaceError("room-match entry display name is missing")
    return MatchEntrySnapshotDTO(
        entry_number=entry.entry_number,
        display_name=entry.player_name,
        game_account_id=entry.game_account_id,
        character_name=entry.horse_name_or_label,
    )


def _race_json(race: Race, *, entry_count: int) -> dict[str, object]:
    return {
        "id": race.id,
        "event_id": race.event_id,
        "name": race.name,
        "description": race.description,
        "starts_at": _datetime_json(race.starts_at),
        "status": race.status,
        "betting_window_version": race.betting_window_version,
        "betting_opened_at": _datetime_json(race.betting_opened_at),
        "betting_closed_at": _datetime_json(race.betting_closed_at),
        "entry_count": entry_count,
    }


def _condition_json(condition: RaceCondition) -> dict[str, object]:
    return asdict(_condition_dto(condition))


def _entry_json(entry: RaceEntry) -> dict[str, object]:
    return {
        **asdict(_entry_dto(entry)),
        "owner_at_event_persona_id": entry.owner_at_event_persona_id,
    }


def _condition_from_json(value: object) -> RaceConditionDTO:
    data = _mapping(value, field="audit condition")
    return RaceConditionDTO(
        grade=_json_text(data.get("grade"), field="grade"),
        venue=_json_text(data.get("venue"), field="venue"),
        track_surface=_json_text(data.get("track_surface"), field="track surface"),
        distance=_json_int(data.get("distance"), field="distance"),
        direction=_json_text(data.get("direction"), field="direction"),
        season=_json_text(data.get("season"), field="season"),
        weather=_json_text(data.get("weather"), field="weather"),
        track_condition=_json_text(data.get("track_condition"), field="track condition"),
        condition_label=_json_text(data.get("condition_label"), field="condition label"),
        participant_count=_json_int(data.get("participant_count"), field="participant count"),
    )


def _entry_from_json(value: object) -> MatchEntrySnapshotDTO:
    data = _mapping(value, field="audit entry")
    return MatchEntrySnapshotDTO(
        entry_number=_json_int(data.get("entry_number"), field="entry number"),
        display_name=_json_text(data.get("display_name"), field="display name"),
        game_account_id=_json_optional_int(data.get("game_account_id"), field="game account ID"),
        character_name=_json_optional_text(data.get("character_name"), field="character name"),
    )


def _normalize_entries(values: Sequence[FinalEntrySnapshotInput]) -> tuple[FinalEntrySnapshotInput, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise MatchRaceError("final entry snapshot must contain at least one entry")
    normalized = tuple(
        FinalEntrySnapshotInput(
            entry_number=_positive_int(value.entry_number, field="entry number"),
            display_name=_text(value.display_name, field="display name", maximum=100),
            game_account_id=_optional_positive_int(value.game_account_id, field="game account ID"),
            character_name=_optional_text(value.character_name, field="character name", maximum=100),
        )
        for value in values
    )
    numbers = [entry.entry_number for entry in normalized]
    if len(numbers) != len(set(numbers)):
        raise MatchRaceError("final entry snapshot contains duplicate entry numbers")
    return tuple(sorted(normalized, key=lambda entry: entry.entry_number))


def _fingerprint(action: str, payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        {"action": action, "payload": payload}, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _text(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise MatchRaceError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise MatchRaceError(f"{field} must contain between 1 and {maximum} characters")
    return normalized


def _optional_text(value: str | None, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, field=field, maximum=maximum)


def _positive_int(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MatchRaceError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: int | None, *, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field=field)


def _datetime(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MatchRaceError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _datetime_json(value: datetime | None) -> str | None:
    if value is None:
        return None
    return database_datetime_as_utc(value).isoformat()


def _datetime_from_json(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MatchRaceError("stored timestamp is invalid")
    try:
        return _datetime(datetime.fromisoformat(value), field="stored timestamp")
    except ValueError as exc:
        raise MatchRaceError("stored timestamp is invalid") from exc


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MatchRaceError(f"{field} is invalid")
    return value


def _json_text(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise MatchRaceError(f"stored {field} is invalid")
    return value


def _json_optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _json_text(value, field=field)


def _json_int(value: object, *, field: str, allow_zero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < (0 if allow_zero else 1):
        raise MatchRaceError(f"stored {field} is invalid")
    return value


def _json_optional_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    return _json_int(value, field=field)
