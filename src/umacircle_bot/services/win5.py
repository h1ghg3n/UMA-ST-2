import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    CirclePointAccount,
    CirclePointTransaction,
    GameAccount,
    GameEvent,
    Race,
    RaceEntry,
    Win5Entry,
    Win5Judgement,
    Win5OperationAudit,
    Win5Pick,
    Win5Result,
    Win5Round,
    Win5RoundRace,
    Win5Score,
    Win5ScoreEvent,
    Win5Season,
)
from umacircle_bot.domain.betting import validate_circle_point_balance
from umacircle_bot.domain.errors import (
    AccountNotFoundError,
    BettingRuleError,
    IdentityStateError,
    Win5LifecycleError,
    Win5MutationConflictError,
    Win5RuleError,
    Win5SubmissionError,
)
from umacircle_bot.domain.identity import IdentityStatus
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.domain.win5 import (
    MAX_OPEN_ROUNDS_PER_SEASON,
    PICK_COUNT_BY_TIER,
    Win5PredictionTier,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SubmissionStatus,
    build_win5_picks_fingerprint,
    build_win5_submission_fingerprint,
    judge_win5_prediction,
    judge_win5_special_winner,
    normalize_prediction_tier,
    normalize_win5_picks,
    normalize_win5_result_order,
    normalize_win5_special_result_order,
    win5_circle_point_reward,
)
from umacircle_bot.services.accounts import resolve_owned_game_account
from umacircle_bot.services.dtos import (
    Win5AccountInfoDTO,
    Win5EntryDTO,
    Win5JudgementDTO,
    Win5MutationResultDTO,
    Win5RaceEntryDTO,
    Win5ResultDTO,
    Win5ResultMutationDTO,
    Win5RoundDTO,
    Win5RoundRaceDTO,
    Win5ScoreDTO,
    Win5ScoringResultDTO,
    Win5SeasonDTO,
    Win5SeasonInfoDTO,
    Win5StandingDTO,
    Win5SubmissionRoundDTO,
)
from umacircle_bot.services.persona_wallets import lock_persona_wallets_for_game_accounts

WIN5_SEASON_MANAGE_CAPABILITY = "win5.season.manage"
WIN5_ROUND_MANAGE_CAPABILITY = "win5.round.manage"
WIN5_SUBMIT_CAPABILITY = "win5.submit"
WIN5_CANCEL_CAPABILITY = "win5.cancel"
WIN5_RESULT_MANAGE_CAPABILITY = "win5.result.manage"
WIN5_SCORE_MANAGE_CAPABILITY = "win5.score.manage"


@dataclass(frozen=True, slots=True)
class CreateWin5SeasonCommand:
    name: str
    actor_discord_user_id: str
    idempotency_key: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Win5SeasonTransitionCommand:
    season_id: int
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Win5RoundEntryInput:
    entry_number: int
    display_name: str


@dataclass(frozen=True, slots=True)
class Win5SpecialRaceInput:
    race_name: str
    entries: tuple[Win5RoundEntryInput, ...] = ()
    starts_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CreateWin5RoundCommand:
    season_id: int
    round_number: int
    race_name: str
    starts_at: datetime
    entries: tuple[Win5RoundEntryInput, ...]
    actor_discord_user_id: str
    idempotency_key: str
    round_label: str | None = None
    event_id: int | None = None
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CreateWin5SpecialRoundCommand:
    season_id: int
    round_number: int | None
    races: tuple[Win5SpecialRaceInput, ...]
    actor_discord_user_id: str
    idempotency_key: str
    round_label: str | None = None
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Win5RoundTransitionCommand:
    round_id: int
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class EnterWin5ResultCommand:
    round_id: int
    result_order: tuple[int, ...]
    actor_discord_user_id: str
    idempotency_key: str
    race_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CorrectWin5ResultCommand:
    round_id: int
    result_order: tuple[int, ...]
    actor_discord_user_id: str
    idempotency_key: str
    reason: str
    race_id: int | None = None


@dataclass(frozen=True, slots=True)
class ScoreWin5RoundCommand:
    round_id: int
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CancelWin5SubmissionCommand:
    submission_id: int
    game_account_id: int
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Win5RoundOwnership:
    normal_race: Race | None
    normal_entries: tuple[RaceEntry, ...]
    special_races: tuple["_Win5SpecialRaceOwnership", ...]


@dataclass(frozen=True, slots=True)
class _Win5SpecialRaceOwnership:
    round_race: Win5RoundRace
    race: Race
    entries: tuple[RaceEntry, ...]


def create_win5_season(
    session: Session,
    *,
    command: CreateWin5SeasonCommand,
) -> Win5MutationResultDTO:
    action = "win5_season_create"
    name = _text(command.name, field="season name", maximum=100)
    starts_at = _optional_datetime(command.starts_at, field="season start time")
    ends_at = _optional_datetime(command.ends_at, field="season end time")
    if starts_at is not None and ends_at is not None and ends_at <= starts_at:
        raise Win5LifecycleError("WIN5 season end time must be after its start time")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    fingerprint = _fingerprint(
        action,
        {
            "name": name,
            "starts_at": _datetime_json(starts_at),
            "ends_at": _datetime_json(ends_at),
            "actor": actor,
        },
    )
    try:
        with session.begin_nested():
            locked_seasons = _lock_seasons_for_number_allocation(session)
            existing = _load_audit(session, request_key)
            if existing is not None:
                return _mutation_from_audit(session, existing, action=action, fingerprint=fingerprint)
            claimed_numbers = tuple(
                season.season_number for season in locked_seasons if season.status != Win5SeasonStatus.CANCELLED.value
            )
            if any(number <= 0 for number in claimed_numbers):
                raise Win5MutationConflictError("WIN5 season numbering state is invalid")
            season_number = max(claimed_numbers, default=0) + 1
            season = Win5Season(
                season_number=season_number,
                season_number_marker=season_number,
                name=name,
                starts_at=starts_at,
                ends_at=ends_at,
                status=Win5SeasonStatus.DRAFT.value,
                active_marker=None,
            )
            session.add(season)
            session.flush()
            audit = _append_audit(
                session,
                action=action,
                capability=WIN5_SEASON_MANAGE_CAPABILITY,
                actor=actor,
                request_key=request_key,
                fingerprint=fingerprint,
                season_id=season.id,
                before=None,
                after=_season_json(season),
                reason=reason,
            )
            return Win5MutationResultDTO(action=action, audit_id=audit.id, season=_season_dto(season))
    except IntegrityError as exc:
        return _recover_season_create_integrity(
            session,
            error=exc,
            request_key=request_key,
            action=action,
            fingerprint=fingerprint,
        )


def activate_win5_season(
    session: Session,
    *,
    command: Win5SeasonTransitionCommand,
) -> Win5MutationResultDTO:
    return _transition_season(
        session,
        command=command,
        action="win5_season_activate",
        expected=Win5SeasonStatus.DRAFT,
        target=Win5SeasonStatus.ACTIVE,
    )


def close_win5_season(
    session: Session,
    *,
    command: Win5SeasonTransitionCommand,
) -> Win5MutationResultDTO:
    return _transition_season(
        session,
        command=command,
        action="win5_season_close",
        expected=Win5SeasonStatus.ACTIVE,
        target=Win5SeasonStatus.CLOSED,
    )


def cancel_win5_season(
    session: Session,
    *,
    command: Win5SeasonTransitionCommand,
) -> Win5MutationResultDTO:
    return _transition_season(
        session,
        command=command,
        action="win5_season_cancel",
        expected=Win5SeasonStatus.DRAFT,
        target=Win5SeasonStatus.CANCELLED,
    )


def create_win5_round(
    session: Session,
    *,
    command: CreateWin5RoundCommand,
) -> Win5MutationResultDTO:
    action = "win5_round_create"
    season_id = _positive_int(command.season_id, field="season ID")
    round_number = _positive_int(command.round_number, field="round number")
    race_name = _text(command.race_name, field="race name", maximum=200)
    starts_at = _datetime(command.starts_at, field="race start time")
    entries = _normalize_round_entries(command.entries)
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    round_label = _optional_text(command.round_label, field="round label", maximum=64)
    event_id = _optional_positive_int(command.event_id, field="event ID")
    opens_at = _optional_datetime(command.opens_at, field="round opens_at")
    closes_at = _optional_datetime(command.closes_at, field="round closes_at")
    if opens_at is not None and closes_at is not None and closes_at <= opens_at:
        raise Win5LifecycleError("WIN5 round closes_at must be after opens_at")
    reason = _optional_text(command.reason, field="reason", maximum=255)
    fingerprint = _fingerprint(
        action,
        {
            "season_id": season_id,
            "round_number": round_number,
            "race_name": race_name,
            "starts_at": _datetime_json(starts_at),
            "entries": [asdict(entry) for entry in entries],
            "round_label": round_label,
            "event_id": event_id,
            "opens_at": _datetime_json(opens_at),
            "closes_at": _datetime_json(closes_at),
            "actor": actor,
        },
    )
    try:
        with session.begin_nested():
            season = _lock_season(session, season_id)
            existing = _load_audit(session, request_key)
            if existing is not None:
                return _mutation_from_audit(session, existing, action=action, fingerprint=fingerprint)
            if season.status not in {Win5SeasonStatus.DRAFT.value, Win5SeasonStatus.ACTIVE.value}:
                raise Win5LifecycleError("WIN5 rounds can only be created in a draft or active season")
            if event_id is not None and session.get(GameEvent, event_id) is None:
                raise Win5MutationConflictError("WIN5 round event does not exist")
            duplicate_round = session.scalar(
                select(Win5Round.id)
                .where(
                    Win5Round.season_id == season.id,
                    Win5Round.round_number == round_number,
                )
                .with_for_update()
            )
            if duplicate_round is not None:
                raise Win5MutationConflictError("WIN5 season already has this round number")
            race = Race(
                event_id=event_id,
                race_kind="win5",
                name=race_name,
                starts_at=starts_at,
                status="scheduled",
            )
            session.add(race)
            session.flush()
            session.add_all(
                [
                    RaceEntry(
                        race_id=race.id,
                        entry_number=entry.entry_number,
                        entry_kind="win5_horse",
                        entry_number_source="declared",
                        player_name=None,
                        horse_name_or_label=entry.display_name,
                    )
                    for entry in entries
                ]
            )
            round_ = Win5Round(
                season_id=season.id,
                race_id=race.id,
                round_type=Win5RoundType.NORMAL.value,
                round_number=round_number,
                round_label=round_label,
                status=Win5RoundStatus.SETUP.value,
                opens_at=opens_at,
                closes_at=closes_at,
            )
            session.add(round_)
            session.flush()
            audit = _append_audit(
                session,
                action=action,
                capability=WIN5_ROUND_MANAGE_CAPABILITY,
                actor=actor,
                request_key=request_key,
                fingerprint=fingerprint,
                season_id=season.id,
                round_id=round_.id,
                before=None,
                after=_round_json(round_, race=race, entries=entries),
                reason=reason,
            )
            return Win5MutationResultDTO(
                action=action,
                audit_id=audit.id,
                round=_round_dto(session, round_),
            )
    except IntegrityError as exc:
        return _recover_round_create_integrity(
            session,
            error=exc,
            request_key=request_key,
            action=action,
            fingerprint=fingerprint,
        )


def create_win5_special_round(
    session: Session,
    *,
    command: CreateWin5SpecialRoundCommand,
) -> Win5MutationResultDTO:
    action = "win5_special_round_create"
    season_id = _positive_int(command.season_id, field="season ID")
    requested_round_number = _optional_positive_int(command.round_number, field="round number")
    races = _normalize_special_races(command.races)
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    round_label = _optional_text(command.round_label, field="round label", maximum=64)
    opens_at = _optional_datetime(command.opens_at, field="round opens_at")
    closes_at = _optional_datetime(command.closes_at, field="round closes_at")
    if opens_at is not None and closes_at is not None and closes_at <= opens_at:
        raise Win5LifecycleError("WIN5 round closes_at must be after opens_at")
    reason = _optional_text(command.reason, field="reason", maximum=255)
    fingerprint = _fingerprint(
        action,
        {
            "season_id": season_id,
            "round_number": requested_round_number,
            "races": [
                {
                    "race_name": race.race_name,
                    "starts_at": _datetime_json(race.starts_at),
                    "entries": [
                        {
                            "entry_number": entry.entry_number,
                            "display_name": entry.display_name,
                        }
                        for entry in race.entries
                    ],
                }
                for race in races
            ],
            "round_label": round_label,
            "opens_at": _datetime_json(opens_at),
            "closes_at": _datetime_json(closes_at),
            "actor": actor,
        },
    )
    try:
        with session.begin_nested():
            season = _lock_season(session, season_id)
            existing = _load_audit(session, request_key)
            if existing is not None:
                return _mutation_from_audit(session, existing, action=action, fingerprint=fingerprint)
            if season.status not in {Win5SeasonStatus.DRAFT.value, Win5SeasonStatus.ACTIVE.value}:
                raise Win5LifecycleError("WIN5 rounds can only be created in a draft or active season")
            round_number = requested_round_number
            if round_number is None:
                latest_round_number = session.scalar(
                    select(func.max(Win5Round.round_number)).where(
                        Win5Round.season_id == season.id,
                    )
                )
                round_number = (latest_round_number or 0) + 1
            duplicate_round = session.scalar(
                select(Win5Round.id)
                .where(
                    Win5Round.season_id == season.id,
                    Win5Round.round_number == round_number,
                )
                .with_for_update()
            )
            if duplicate_round is not None:
                raise Win5MutationConflictError("WIN5 season already has this round number")
            round_ = Win5Round(
                season_id=season.id,
                race_id=None,
                round_type=Win5RoundType.SPECIAL.value,
                round_number=round_number,
                round_label=round_label,
                status=Win5RoundStatus.SETUP.value,
                opens_at=opens_at,
                closes_at=closes_at,
            )
            session.add(round_)
            session.flush()
            created_races: list[Race] = []
            for race_input in races:
                race = Race(
                    race_kind="win5",
                    name=race_input.race_name,
                    starts_at=race_input.starts_at,
                    status="scheduled",
                )
                session.add(race)
                session.flush()
                created_races.append(race)
            session.add_all(
                [
                    Win5RoundRace(
                        round_id=round_.id,
                        race_id=race.id,
                        display_order=display_order,
                    )
                    for display_order, race in enumerate(created_races, start=1)
                ]
            )
            session.flush()
            audit = _append_audit(
                session,
                action=action,
                capability=WIN5_ROUND_MANAGE_CAPABILITY,
                actor=actor,
                request_key=request_key,
                fingerprint=fingerprint,
                season_id=season.id,
                round_id=round_.id,
                before=None,
                after=_round_json_from_db(session, round_),
                reason=reason,
            )
            return Win5MutationResultDTO(
                action=action,
                audit_id=audit.id,
                round=_round_dto(session, round_),
            )
    except IntegrityError as exc:
        return _recover_round_create_integrity(
            session,
            error=exc,
            request_key=request_key,
            action=action,
            fingerprint=fingerprint,
        )


def open_win5_round(
    session: Session,
    *,
    command: Win5RoundTransitionCommand,
) -> Win5MutationResultDTO:
    return _transition_round(
        session,
        command=command,
        action="win5_round_open",
        expected=Win5RoundStatus.SETUP,
        target=Win5RoundStatus.OPEN,
    )


def close_win5_round(
    session: Session,
    *,
    command: Win5RoundTransitionCommand,
) -> Win5MutationResultDTO:
    return _transition_round(
        session,
        command=command,
        action="win5_round_close",
        expected=Win5RoundStatus.OPEN,
        target=Win5RoundStatus.CLOSED,
    )


def enter_win5_result(
    session: Session,
    *,
    command: EnterWin5ResultCommand,
) -> Win5ResultMutationDTO:
    action = "win5_result_enter"
    round_id = _positive_int(command.round_id, field="round ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    try:
        requested_result_order = _normalize_stored_result_order(command.result_order)
    except Win5RuleError as exc:
        raise Win5LifecycleError(str(exc)) from exc
    requested_race_id = _optional_positive_int(command.race_id, field="race ID")
    fingerprint = _fingerprint(
        action,
        {
            "round_id": round_id,
            "race_id": requested_race_id,
            "result_order": requested_result_order,
            "actor": actor,
        },
    )
    try:
        with session.begin_nested():
            round_ = _lock_round(session, round_id)
            ownership = _validate_win5_round_ownership(session, round_=round_, lock=True)
            try:
                result_order = _normalize_result_order_for_round(
                    round_,
                    requested_result_order,
                )
            except Win5RuleError as exc:
                raise Win5LifecycleError(str(exc)) from exc
            existing = _load_audit(session, request_key)
            if existing is not None:
                return _result_mutation_from_audit(
                    session,
                    existing,
                    action=action,
                    fingerprint=fingerprint,
                )
            if round_.status != Win5RoundStatus.CLOSED.value:
                raise Win5LifecycleError("WIN5 round must be closed before entering its result")
            race, entries = _select_result_race(
                round_=round_,
                ownership=ownership,
                requested_race_id=requested_race_id,
            )
            blocking_round_id = _active_other_round_for_race(
                session,
                race_id=race.id,
                current_round_id=round_.id,
            )
            if blocking_round_id is not None:
                raise Win5LifecycleError(
                    "all WIN5 rounds for this race must stop accepting submissions before result entry"
                )
            if round_.round_type == Win5RoundType.NORMAL.value:
                _validate_result_belongs_to_round(entries=entries, result_order=result_order)
            existing_result = session.scalar(
                select(Win5Result)
                .where(Win5Result.race_id == race.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if existing_result is None:
                result = Win5Result(
                    round_id=round_.id,
                    event_id=race.event_id,
                    season_id=round_.season_id,
                    race_id=race.id,
                    result_order=list(result_order),
                )
                session.add(result)
                session.flush()
                before_result = None
            else:
                result = existing_result
                _validate_result_for_race(
                    race=race,
                    entries=entries,
                    result=result,
                    special=round_.round_type == Win5RoundType.SPECIAL.value,
                )
                if _normalize_result_order_for_round(round_, result.result_order) != result_order:
                    raise Win5MutationConflictError("WIN5 race authoritative result conflicts with this request")
                before_result = _result_json(result)
            if _all_required_results_entered(session, round_=round_, ownership=ownership):
                round_.status = Win5RoundStatus.RESULT_ENTERED.value
            session.flush()
            audit = _append_audit(
                session,
                action=action,
                capability=WIN5_RESULT_MANAGE_CAPABILITY,
                actor=actor,
                request_key=request_key,
                fingerprint=fingerprint,
                season_id=round_.season_id,
                round_id=round_.id,
                before={
                    "round_status": Win5RoundStatus.CLOSED.value,
                    "result": before_result,
                },
                after={
                    "round_status": round_.status,
                    "result": _result_json(result),
                },
                reason=reason,
            )
            return Win5ResultMutationDTO(
                action=action,
                audit_id=audit.id,
                round=_round_dto(session, round_),
                result=_result_dto(result),
            )
    except IntegrityError as exc:
        session.expire_all()
        audit = _load_audit(session, request_key, lock=True)
        if audit is not None:
            return _result_mutation_from_audit(
                session,
                audit,
                action=action,
                fingerprint=fingerprint,
            )
        raise exc


def _select_result_race(
    *,
    round_: Win5Round,
    ownership: _Win5RoundOwnership,
    requested_race_id: int | None,
) -> tuple[Race, tuple[RaceEntry, ...]]:
    if round_.round_type == Win5RoundType.NORMAL.value:
        if requested_race_id is not None and requested_race_id != round_.race_id:
            raise Win5LifecycleError("WIN5 result race does not belong to the selected round")
        if ownership.normal_race is None:
            raise Win5MutationConflictError("WIN5 normal round race changed")
        return ownership.normal_race, ownership.normal_entries
    if requested_race_id is None:
        raise Win5LifecycleError("WIN5 special round result requires a selected race ID")
    selected = next(
        (item for item in ownership.special_races if item.race.id == requested_race_id),
        None,
    )
    if selected is None:
        raise Win5LifecycleError("WIN5 result race does not belong to the selected special round")
    if selected.race.status == "voided":
        raise Win5LifecycleError("voided WIN5 special races do not accept results")
    return selected.race, selected.entries


def _all_required_results_entered(
    session: Session,
    *,
    round_: Win5Round,
    ownership: _Win5RoundOwnership,
) -> bool:
    if round_.round_type == Win5RoundType.NORMAL.value:
        required_race_ids = (round_.race_id,)
    else:
        required_race_ids = tuple(item.race.id for item in ownership.special_races if item.race.status != "voided")
    if not required_race_ids:
        return True
    result_race_ids = tuple(
        session.scalars(
            select(Win5Result.race_id)
            .where(
                Win5Result.race_id.in_(required_race_ids),
            )
            .order_by(Win5Result.race_id)
        )
    )
    return result_race_ids == tuple(sorted(required_race_ids))


def score_win5_round(
    session: Session,
    *,
    command: ScoreWin5RoundCommand,
) -> Win5ScoringResultDTO:
    action = "win5_round_score"
    round_id = _positive_int(command.round_id, field="round ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    fingerprint = _fingerprint(action, {"round_id": round_id, "actor": actor})
    try:
        with session.begin_nested():
            round_ = _lock_round(session, round_id)
            ownership = _validate_win5_round_ownership(session, round_=round_, lock=True)
            existing = _load_audit(session, request_key)
            if existing is not None:
                return _scoring_from_audit(
                    session,
                    existing,
                    action=action,
                    fingerprint=fingerprint,
                )
            if round_.status not in {
                Win5RoundStatus.RESULT_ENTERED.value,
                Win5RoundStatus.CLOSED.value,
            }:
                raise Win5LifecycleError("WIN5 round must have an entered result before scoring")
            before_round_status = round_.status
            if before_round_status == Win5RoundStatus.CLOSED.value and (
                round_.round_type != Win5RoundType.SPECIAL.value
                or not _all_required_results_entered(
                    session,
                    round_=round_,
                    ownership=ownership,
                )
            ):
                raise Win5LifecycleError("WIN5 round must have an entered result before scoring")
            results = _load_round_results(
                session,
                round_=round_,
                ownership=ownership,
                lock=True,
            )
            _validate_round_results(
                round_=round_,
                ownership=ownership,
                results=results,
            )
            result_by_race_id = {result.race_id: result for result in results}
            # Round is the mutation lock root. Once result_entered, submit and
            # cancel cannot change entries, so locking child scans would only
            # widen MariaDB next-key locks across unrelated Rounds.
            all_accepted_entries = tuple(
                session.scalars(
                    select(Win5Entry)
                    .where(
                        Win5Entry.round_id == round_.id,
                        Win5Entry.status == Win5SubmissionStatus.ACCEPTED.value,
                    )
                    .order_by(Win5Entry.id)
                    .execution_options(populate_existing=True)
                )
            )
            accepted_entries = _scorable_entries(
                round_=round_,
                ownership=ownership,
                entries=all_accepted_entries,
            )
            _validate_unscored_entries(session, round_=round_, entries=accepted_entries)
            account_ids = tuple(sorted({entry.game_account_id for entry in accepted_entries}))
            _lock_game_accounts(session, account_ids)
            circle_point_account_by_game_account = (
                _lock_circle_point_accounts(session, account_ids=account_ids)
                if round_.round_type == Win5RoundType.NORMAL.value
                else {}
            )
            score_by_account = _lock_scores(session, season_id=round_.season_id, account_ids=account_ids)
            judged_at = datetime.now(UTC)
            judgements: list[Win5Judgement] = []
            judgement_result_orders: list[tuple[int, ...]] = []
            room_point_rewards: list[dict[str, int]] = []
            for entry in accepted_entries:
                prediction_tier = normalize_prediction_tier(
                    entry.prediction_tier,
                    allow_special=True,
                )
                picks = _load_entry_picks(
                    session,
                    entry.id,
                    prediction_tier=prediction_tier,
                )
                if prediction_tier is Win5PredictionTier.SPECIAL_WINNER:
                    special_race = _special_race_for_entry(
                        ownership=ownership,
                        entry=entry,
                    )
                    result = result_by_race_id.get(special_race.race.id)
                    if result is None:
                        raise Win5MutationConflictError("WIN5 special race result is missing")
                    result_order = normalize_win5_special_result_order(result.result_order)
                    judgement = judge_win5_special_winner(
                        predicted_entry_number=picks[0],
                        result_order=result_order,
                    )
                else:
                    if round_.round_type != Win5RoundType.NORMAL.value or round_.race_id is None:
                        raise Win5MutationConflictError("WIN5 normal submission scope changed")
                    result = result_by_race_id.get(round_.race_id)
                    if result is None:
                        raise Win5MutationConflictError("WIN5 round result is missing")
                    result_order = normalize_win5_result_order(result.result_order)
                    judgement = judge_win5_prediction(
                        picks,
                        result_order,
                        prediction_tier=prediction_tier,
                    )
                room_point_delta = win5_circle_point_reward(
                    prediction_tier=prediction_tier,
                    exact_position_count=judgement.exact_position_count,
                )
                judgement_row = Win5Judgement(
                    win5_entry_id=entry.id,
                    season_id=round_.season_id,
                    prediction_tier=judgement.prediction_tier.value,
                    exact_position_count=judgement.exact_position_count,
                    on_board_wrong_position_count=judgement.on_board_wrong_position_count,
                    off_board_count=judgement.off_board_count,
                    season_score_delta=judgement.season_score_delta,
                    top1_score_delta=judgement.top1_score_delta,
                    judgement_detail_json={
                        "prediction_tier": judgement.prediction_tier.value,
                        "picks": list(judgement.picks),
                        "result_order": list(result_order),
                        "exact_position_count": judgement.exact_position_count,
                        "on_board_wrong_position_count": judgement.on_board_wrong_position_count,
                        "off_board_count": judgement.off_board_count,
                        "room_point_delta": room_point_delta,
                    },
                    judged_at=judged_at,
                    judged_by_discord_user_id=actor,
                )
                session.add(judgement_row)
                score = score_by_account.get(entry.game_account_id)
                if score is None:
                    score = Win5Score(
                        season_id=round_.season_id,
                        game_account_id=entry.game_account_id,
                        season_score=0,
                        top1_score=0,
                    )
                    session.add(score)
                    score_by_account[entry.game_account_id] = score
                score.season_score += judgement.season_score_delta
                score.top1_score += judgement.top1_score_delta
                session.add(
                    Win5ScoreEvent(
                        season_id=round_.season_id,
                        game_account_id=entry.game_account_id,
                        win5_entry_id=entry.id,
                        season_score_delta=judgement.season_score_delta,
                        top1_score_delta=judgement.top1_score_delta,
                        reason=f"WIN5 round #{round_.id} score",
                    )
                )
                if room_point_delta:
                    point_account = circle_point_account_by_game_account[entry.game_account_id]
                    try:
                        validate_circle_point_balance(point_account.balance + room_point_delta)
                    except BettingRuleError as exc:
                        raise Win5LifecycleError("WIN5 room point reward exceeds the supported balance") from exc
                    point_account.balance += room_point_delta
                    session.add(
                        CirclePointTransaction(
                            persona_id=point_account.persona_id,
                            game_account_id=entry.game_account_id,
                            type="win5_reward",
                            amount=room_point_delta,
                            reason=f"WIN5 round #{round_.id} exact-position reward",
                            source="win5_score",
                            created_by_discord_user_id=actor,
                            idempotency_key=f"win5_reward:entry:{entry.id}",
                        )
                    )
                    room_point_rewards.append({"entry_id": entry.id, "amount": room_point_delta})
                judgements.append(judgement_row)
                judgement_result_orders.append(result_order)
            round_.status = Win5RoundStatus.SCORED.value
            session.flush()
            judgement_dtos = tuple(
                _judgement_dto(
                    session,
                    judgement,
                    game_account_id=entry.game_account_id,
                    result_order=result_order,
                )
                for judgement, entry, result_order in zip(
                    judgements,
                    accepted_entries,
                    judgement_result_orders,
                    strict=True,
                )
            )
            score_dtos = tuple(_score_dto(score_by_account[account_id]) for account_id in account_ids)
            audit = _append_audit(
                session,
                action=action,
                capability=WIN5_SCORE_MANAGE_CAPABILITY,
                actor=actor,
                request_key=request_key,
                fingerprint=fingerprint,
                season_id=round_.season_id,
                round_id=round_.id,
                before={"round_status": before_round_status},
                after={
                    "round_status": round_.status,
                    "result_ids": [result.id for result in results],
                    "judgement_ids": [judgement.id for judgement in judgements],
                    "score_event_entry_ids": [entry.id for entry in accepted_entries],
                    "room_point_rewards": room_point_rewards,
                    "scores": [_score_json(score) for score in score_dtos],
                },
                reason=reason,
            )
            return Win5ScoringResultDTO(
                action=action,
                audit_id=audit.id,
                round=_round_dto(session, round_),
                result=_result_dto(results[0]) if round_.round_type == Win5RoundType.NORMAL.value else None,
                results=tuple(_result_dto(result) for result in results),
                judgements=judgement_dtos,
                scores=score_dtos,
            )
    except IntegrityError as exc:
        session.expire_all()
        audit = _load_audit(session, request_key, lock=True)
        if audit is not None:
            return _scoring_from_audit(
                session,
                audit,
                action=action,
                fingerprint=fingerprint,
            )
        raise exc


def submit_win5_prediction(
    session: Session,
    *,
    game_account_id: int,
    season_id: int,
    round_id: int,
    picks: Sequence[int],
    prediction_tier: Win5PredictionTier | str = Win5PredictionTier.TOP5,
    actor_discord_user_id: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> Win5EntryDTO:
    del now  # Manual round state is authoritative; timestamps are metadata only.
    action = "win5_submission_accept"
    game_account_id = _positive_int(game_account_id, field="game account ID", error_type=Win5SubmissionError)
    season_id = _positive_int(season_id, field="season ID", error_type=Win5SubmissionError)
    round_id = _positive_int(round_id, field="round ID", error_type=Win5SubmissionError)
    actor = _text(actor_discord_user_id, field="actor ID", maximum=32, error_type=Win5SubmissionError)
    request_key = _text(idempotency_key, field="idempotency key", maximum=128, error_type=Win5SubmissionError)
    try:
        tier = normalize_prediction_tier(prediction_tier)
        normalized_picks = normalize_win5_picks(
            picks,
            prediction_tier=tier,
        )
    except Win5RuleError as exc:
        raise Win5SubmissionError(str(exc)) from exc
    picks_fingerprint = build_win5_picks_fingerprint(
        prediction_tier=tier,
        picks=normalized_picks,
    )
    fingerprint = build_win5_submission_fingerprint(
        game_account_id=game_account_id,
        season_id=season_id,
        round_id=round_id,
        prediction_tier=tier,
        picks=normalized_picks,
    )

    try:
        with session.begin_nested():
            round_ = _lock_round(session, round_id, error_type=Win5SubmissionError)
            ownership = _validate_win5_round_ownership(session, round_=round_, lock=True)
            existing = _load_audit(session, request_key)
            if existing is not None:
                return _submission_from_audit(session, existing, action=action, fingerprint=fingerprint)
            if round_.season_id != season_id:
                raise Win5SubmissionError("WIN5 round does not belong to the selected season")
            if round_.round_type != Win5RoundType.NORMAL.value:
                raise Win5SubmissionError("WIN5 special rounds require a race-specific prediction")
            season = session.get(Win5Season, season_id, populate_existing=True)
            if season is None:
                raise Win5SubmissionError("WIN5 season not found")
            if season.status != Win5SeasonStatus.ACTIVE.value:
                raise Win5SubmissionError("WIN5 season is not active")
            if round_.status != Win5RoundStatus.OPEN.value:
                raise Win5SubmissionError("WIN5 round is not open for submissions")
            if _ownership_has_result(session, ownership=ownership):
                raise Win5SubmissionError("WIN5 race result is already available")
            _require_confirmed_win5_game_account(session, game_account_id=game_account_id)
            _validate_picks_belong_to_round(entries=ownership.normal_entries, picks=normalized_picks)
            accepted_submission = session.scalar(
                select(Win5Entry.id).where(
                    Win5Entry.round_id == round_.id,
                    Win5Entry.game_account_id == game_account_id,
                    Win5Entry.status == Win5SubmissionStatus.ACCEPTED.value,
                )
            )
            if accepted_submission is not None:
                raise Win5SubmissionError("WIN5 account already has an accepted submission for this round")
            entry = Win5Entry(
                season_id=season.id,
                round_id=round_.id,
                game_account_id=game_account_id,
                idempotency_key=request_key,
                request_fingerprint=fingerprint,
                ordered_picks_fingerprint=picks_fingerprint,
                prediction_tier=tier.value,
                special_round_race_id=None,
                accepted_marker=Win5SubmissionStatus.ACCEPTED.value,
                normal_accepted_marker=Win5SubmissionStatus.ACCEPTED.value,
                status=Win5SubmissionStatus.ACCEPTED.value,
            )
            session.add(entry)
            session.flush()
            session.add_all(
                [
                    Win5Pick(
                        win5_entry_id=entry.id,
                        pick_order=pick_order,
                        entry_number=entry_number,
                    )
                    for pick_order, entry_number in enumerate(normalized_picks, start=1)
                ]
            )
            session.flush()
            dto = _entry_dto(session, entry)
            _append_audit(
                session,
                action=action,
                capability=WIN5_SUBMIT_CAPABILITY,
                actor=actor,
                request_key=request_key,
                fingerprint=fingerprint,
                season_id=season.id,
                round_id=round_.id,
                win5_entry_id=entry.id,
                before=None,
                after=_entry_json(dto),
                reason=None,
            )
            return dto
    except IntegrityError as exc:
        session.expire_all()
        audit = _load_audit(session, request_key, lock=True)
        if audit is not None:
            return _submission_from_audit(session, audit, action=action, fingerprint=fingerprint)
        accepted_submission = session.scalar(
            select(Win5Entry.id).where(
                Win5Entry.round_id == round_id,
                Win5Entry.game_account_id == game_account_id,
                Win5Entry.status == Win5SubmissionStatus.ACCEPTED.value,
            )
        )
        if accepted_submission is not None:
            raise Win5SubmissionError("WIN5 account already has an accepted submission for this round") from exc
        raise


def correct_win5_result(
    session: Session,
    *,
    command: CorrectWin5ResultCommand,
) -> Win5ResultMutationDTO:
    action = "win5_result_correct"
    round_id = _positive_int(command.round_id, field="round ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _text(command.reason, field="correction reason", maximum=255)
    requested_race_id = _optional_positive_int(command.race_id, field="race ID")
    try:
        requested_result_order = _normalize_stored_result_order(command.result_order)
    except Win5RuleError as exc:
        raise Win5LifecycleError(str(exc)) from exc
    fingerprint = _fingerprint(
        action,
        {
            "round_id": round_id,
            "race_id": requested_race_id,
            "result_order": requested_result_order,
            "reason": reason,
            "actor": actor,
        },
    )
    try:
        with session.begin_nested():
            round_ = _lock_round(session, round_id)
            ownership = _validate_win5_round_ownership(session, round_=round_, lock=True)
            existing = _load_audit(session, request_key)
            if existing is not None:
                return _result_mutation_from_audit(
                    session,
                    existing,
                    action=action,
                    fingerprint=fingerprint,
                )
            if round_.status != Win5RoundStatus.RESULT_ENTERED.value:
                raise Win5LifecycleError("WIN5 results can only be corrected before scoring")
            if round_.round_type != Win5RoundType.NORMAL.value:
                raise Win5LifecycleError("only normal WIN5 round results can be corrected")
            try:
                result_order = _normalize_result_order_for_round(round_, requested_result_order)
            except Win5RuleError as exc:
                raise Win5LifecycleError(str(exc)) from exc
            race, entries = _select_result_race(
                round_=round_,
                ownership=ownership,
                requested_race_id=requested_race_id,
            )
            _validate_result_belongs_to_round(entries=entries, result_order=result_order)
            result = session.scalar(
                select(Win5Result)
                .where(Win5Result.race_id == race.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if result is None:
                raise Win5MutationConflictError("WIN5 result to correct does not exist")
            _validate_result_for_race(race=race, entries=entries, result=result, special=False)
            before_result = _result_json(result)
            if _normalize_result_order_for_round(round_, result.result_order) == result_order:
                raise Win5MutationConflictError("WIN5 corrected result must differ from the current result")
            result.result_order = list(result_order)
            session.flush()
            audit = _append_audit(
                session,
                action=action,
                capability=WIN5_RESULT_MANAGE_CAPABILITY,
                actor=actor,
                request_key=request_key,
                fingerprint=fingerprint,
                season_id=round_.season_id,
                round_id=round_.id,
                before={
                    "round_status": round_.status,
                    "result": before_result,
                },
                after={
                    "round_status": round_.status,
                    "result": _result_json(result),
                },
                reason=reason,
            )
            return Win5ResultMutationDTO(
                action=action,
                audit_id=audit.id,
                round=_round_dto(session, round_),
                result=_result_dto(result),
            )
    except IntegrityError as exc:
        session.expire_all()
        audit = _load_audit(session, request_key, lock=True)
        if audit is not None:
            return _result_mutation_from_audit(
                session,
                audit,
                action=action,
                fingerprint=fingerprint,
            )
        raise exc


def submit_win5_special_prediction(
    session: Session,
    *,
    game_account_id: int,
    season_id: int,
    round_id: int,
    race_id: int,
    predicted_entry_number: int,
    actor_discord_user_id: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> Win5EntryDTO:
    del now
    action = "win5_special_submission_accept"
    game_account_id = _positive_int(
        game_account_id,
        field="game account ID",
        error_type=Win5SubmissionError,
    )
    season_id = _positive_int(season_id, field="season ID", error_type=Win5SubmissionError)
    round_id = _positive_int(round_id, field="round ID", error_type=Win5SubmissionError)
    race_id = _positive_int(race_id, field="race ID", error_type=Win5SubmissionError)
    actor = _text(
        actor_discord_user_id,
        field="actor ID",
        maximum=32,
        error_type=Win5SubmissionError,
    )
    request_key = _text(
        idempotency_key,
        field="idempotency key",
        maximum=128,
        error_type=Win5SubmissionError,
    )
    tier = Win5PredictionTier.SPECIAL_WINNER
    try:
        normalized_picks = normalize_win5_picks(
            (predicted_entry_number,),
            prediction_tier=tier,
        )
    except Win5RuleError as exc:
        raise Win5SubmissionError(str(exc)) from exc

    fingerprint: str | None = None
    special_round_race_id: int | None = None
    try:
        with session.begin_nested():
            round_ = _lock_round(session, round_id, error_type=Win5SubmissionError)
            ownership = _validate_win5_round_ownership(session, round_=round_, lock=True)
            selected = next(
                (item for item in ownership.special_races if item.race.id == race_id),
                None,
            )
            if selected is None:
                raise Win5SubmissionError("WIN5 race does not belong to the selected special round")
            special_round_race_id = selected.round_race.id
            fingerprint = build_win5_submission_fingerprint(
                game_account_id=game_account_id,
                season_id=season_id,
                round_id=round_id,
                prediction_tier=tier,
                special_round_race_id=special_round_race_id,
                picks=normalized_picks,
            )
            existing = _load_audit(session, request_key)
            if existing is not None:
                return _submission_from_audit(
                    session,
                    existing,
                    action=action,
                    fingerprint=fingerprint,
                )
            if round_.season_id != season_id:
                raise Win5SubmissionError("WIN5 round does not belong to the selected season")
            if round_.round_type != Win5RoundType.SPECIAL.value:
                raise Win5SubmissionError("WIN5 round is not a special round")
            season = session.get(Win5Season, season_id, populate_existing=True)
            if season is None or season.status != Win5SeasonStatus.ACTIVE.value:
                raise Win5SubmissionError("WIN5 season is not active")
            if round_.status != Win5RoundStatus.OPEN.value:
                raise Win5SubmissionError("WIN5 round is not open for submissions")
            if selected.race.status == "voided":
                raise Win5SubmissionError("WIN5 special race is voided")
            if _race_has_result(session, race_id=selected.race.id):
                raise Win5SubmissionError("WIN5 race result is already available")
            _require_confirmed_win5_game_account(session, game_account_id=game_account_id)
            accepted_submission = session.scalar(
                select(Win5Entry.id).where(
                    Win5Entry.special_round_race_id == selected.round_race.id,
                    Win5Entry.game_account_id == game_account_id,
                    Win5Entry.status == Win5SubmissionStatus.ACCEPTED.value,
                )
            )
            if accepted_submission is not None:
                raise Win5SubmissionError("WIN5 account already has an accepted prediction for this special race")
            picks_fingerprint = build_win5_picks_fingerprint(
                prediction_tier=tier,
                picks=normalized_picks,
            )
            entry = Win5Entry(
                season_id=season.id,
                round_id=round_.id,
                game_account_id=game_account_id,
                idempotency_key=request_key,
                request_fingerprint=fingerprint,
                ordered_picks_fingerprint=picks_fingerprint,
                prediction_tier=tier.value,
                special_round_race_id=special_round_race_id,
                accepted_marker=Win5SubmissionStatus.ACCEPTED.value,
                normal_accepted_marker=None,
                status=Win5SubmissionStatus.ACCEPTED.value,
            )
            session.add(entry)
            session.flush()
            session.add(
                Win5Pick(
                    win5_entry_id=entry.id,
                    pick_order=1,
                    entry_number=normalized_picks[0],
                )
            )
            session.flush()
            dto = _entry_dto(session, entry)
            _append_audit(
                session,
                action=action,
                capability=WIN5_SUBMIT_CAPABILITY,
                actor=actor,
                request_key=request_key,
                fingerprint=fingerprint,
                season_id=season.id,
                round_id=round_.id,
                win5_entry_id=entry.id,
                before=None,
                after=_entry_json(dto),
                reason=None,
            )
            return dto
    except IntegrityError as exc:
        session.expire_all()
        audit = _load_audit(session, request_key, lock=True)
        if audit is not None and fingerprint is not None:
            return _submission_from_audit(
                session,
                audit,
                action=action,
                fingerprint=fingerprint,
            )
        if special_round_race_id is not None:
            accepted_submission = session.scalar(
                select(Win5Entry.id).where(
                    Win5Entry.special_round_race_id == special_round_race_id,
                    Win5Entry.game_account_id == game_account_id,
                    Win5Entry.status == Win5SubmissionStatus.ACCEPTED.value,
                )
            )
            if accepted_submission is not None:
                raise Win5SubmissionError(
                    "WIN5 account already has an accepted prediction for this special race"
                ) from exc
        raise


def cancel_win5_submission(
    session: Session,
    *,
    command: CancelWin5SubmissionCommand,
    now: datetime | None = None,
) -> Win5EntryDTO:
    action = "win5_submission_cancel"
    submission_id = _positive_int(command.submission_id, field="submission ID", error_type=Win5SubmissionError)
    game_account_id = _positive_int(
        command.game_account_id,
        field="game account ID",
        error_type=Win5SubmissionError,
    )
    actor = _text(
        command.actor_discord_user_id,
        field="actor ID",
        maximum=32,
        error_type=Win5SubmissionError,
    )
    request_key = _text(
        command.idempotency_key,
        field="idempotency key",
        maximum=128,
        error_type=Win5SubmissionError,
    )
    reason = _optional_text(command.reason, field="reason", maximum=255, error_type=Win5SubmissionError)
    cancelled_at = _current_time(now)
    fingerprint = _fingerprint(
        action,
        {
            "submission_id": submission_id,
            "game_account_id": game_account_id,
            "actor": actor,
        },
    )
    try:
        with session.begin_nested():
            initial = session.get(Win5Entry, submission_id)
            if initial is None:
                raise Win5SubmissionError("WIN5 submission not found")
            round_ = _lock_round(session, initial.round_id, error_type=Win5SubmissionError)
            _validate_win5_round_ownership(session, round_=round_, lock=True)
            existing = _load_audit(session, request_key)
            if existing is not None:
                return _submission_from_audit(session, existing, action=action, fingerprint=fingerprint)
            entry = session.scalar(select(Win5Entry).where(Win5Entry.id == submission_id).with_for_update())
            if entry is None:
                raise Win5SubmissionError("WIN5 submission not found")
            if entry.season_id != round_.season_id or entry.round_id != round_.id:
                raise Win5MutationConflictError("WIN5 submission provenance changed")
            if entry.game_account_id != game_account_id:
                raise Win5SubmissionError("WIN5 submission does not belong to the selected game account")
            if round_.status != Win5RoundStatus.OPEN.value:
                raise Win5SubmissionError("WIN5 submissions can only be cancelled while the round is open")
            if entry.status != Win5SubmissionStatus.ACCEPTED.value:
                raise Win5SubmissionError("WIN5 submission is not accepted")
            before = _entry_json(_entry_dto(session, entry))
            entry.status = Win5SubmissionStatus.CANCELLED.value
            entry.accepted_marker = None
            entry.normal_accepted_marker = None
            entry.cancelled_at = cancelled_at
            entry.cancelled_by_discord_user_id = actor
            session.flush()
            dto = _entry_dto(session, entry)
            _append_audit(
                session,
                action=action,
                capability=WIN5_CANCEL_CAPABILITY,
                actor=actor,
                request_key=request_key,
                fingerprint=fingerprint,
                season_id=round_.season_id,
                round_id=round_.id,
                win5_entry_id=entry.id,
                before=before,
                after=_entry_json(dto),
                reason=reason,
            )
            return dto
    except IntegrityError as exc:
        session.expire_all()
        audit = _load_audit(session, request_key, lock=True)
        if audit is not None:
            return _submission_from_audit(session, audit, action=action, fingerprint=fingerprint)
        raise exc


def list_open_win5_rounds(session: Session) -> tuple[Win5RoundDTO, ...]:
    rounds = session.scalars(
        select(Win5Round)
        .join(Win5Season, Win5Season.id == Win5Round.season_id)
        .where(
            Win5Season.status == Win5SeasonStatus.ACTIVE.value,
            Win5Round.status == Win5RoundStatus.OPEN.value,
        )
        .order_by(Win5Round.round_number, Win5Round.id)
    )
    return tuple(_round_dto(session, round_) for round_ in rounds)


def get_active_win5_season_info(session: Session, *, discord_user_id: str) -> Win5SeasonInfoDTO:
    season = session.scalar(
        select(Win5Season).where(
            Win5Season.status == Win5SeasonStatus.ACTIVE.value,
            Win5Season.active_marker == "active",
        )
    )
    if season is None:
        raise Win5LifecycleError("활성 WIN5 시즌이 없습니다.")

    game_account_id = resolve_owned_game_account(session, discord_user_id=discord_user_id).id

    total_round_count = session.scalar(select(func.count(Win5Round.id)).where(Win5Round.season_id == season.id))
    open_round_count = session.scalar(
        select(func.count(Win5Round.id)).where(
            Win5Round.season_id == season.id,
            Win5Round.status == Win5RoundStatus.OPEN.value,
        )
    )
    latest_open_round = session.scalar(
        select(Win5Round)
        .where(
            Win5Round.season_id == season.id,
            Win5Round.status == Win5RoundStatus.OPEN.value,
        )
        .order_by(Win5Round.updated_at.desc(), Win5Round.id.desc())
        .limit(1)
    )
    score = session.execute(
        select(Win5Score.season_score, Win5Score.top1_score).where(
            Win5Score.season_id == season.id,
            Win5Score.game_account_id == game_account_id,
        )
    ).one_or_none()
    return Win5SeasonInfoDTO(
        season=_season_dto(season),
        total_round_count=total_round_count or 0,
        open_round_count=open_round_count or 0,
        latest_open_round=_round_dto(session, latest_open_round) if latest_open_round is not None else None,
        season_score=score.season_score if score is not None else 0,
        top1_score=score.top1_score if score is not None else 0,
    )


def get_account_win5_info(session: Session, *, discord_user_id: str) -> Win5AccountInfoDTO:
    game_account_id = resolve_owned_game_account(session, discord_user_id=discord_user_id).id

    season = session.scalar(
        select(Win5Season).where(
            Win5Season.status == Win5SeasonStatus.ACTIVE.value,
            Win5Season.active_marker == "active",
        )
    )
    if season is None:
        return Win5AccountInfoDTO(season=None, season_score=0, top1_score=0)

    score = session.execute(
        select(Win5Score.season_score, Win5Score.top1_score).where(
            Win5Score.season_id == season.id,
            Win5Score.game_account_id == game_account_id,
        )
    ).one_or_none()
    return Win5AccountInfoDTO(
        season=_season_dto(season),
        season_score=score.season_score if score is not None else 0,
        top1_score=score.top1_score if score is not None else 0,
    )


def list_win5_submissions(
    session: Session,
    *,
    game_account_id: int,
    round_id: int,
) -> tuple[Win5EntryDTO, ...]:
    game_account_id = _positive_int(game_account_id, field="game account ID", error_type=Win5SubmissionError)
    round_id = _positive_int(round_id, field="round ID", error_type=Win5SubmissionError)
    entries = session.scalars(
        select(Win5Entry)
        .where(
            Win5Entry.game_account_id == game_account_id,
            Win5Entry.round_id == round_id,
        )
        .order_by(Win5Entry.id)
    )
    return tuple(_entry_dto(session, entry) for entry in entries)


def list_active_win5_submission_rounds(
    session: Session,
    *,
    game_account_id: int,
    scope: str,
) -> tuple[Win5SubmissionRoundDTO, ...]:
    game_account_id = _positive_int(game_account_id, field="game account ID", error_type=Win5SubmissionError)
    if session.get(GameAccount, game_account_id) is None:
        raise AccountNotFoundError("registered game account not found")
    if scope not in {"open", "scored"}:
        raise Win5SubmissionError("WIN5 submission view scope is not supported")

    season_id = session.scalar(
        select(Win5Season.id).where(
            Win5Season.status == Win5SeasonStatus.ACTIVE.value,
            Win5Season.active_marker == "active",
        )
    )
    if season_id is None:
        raise Win5LifecycleError("활성 WIN5 시즌이 없습니다.")

    round_status = Win5RoundStatus.OPEN.value if scope == "open" else Win5RoundStatus.SCORED.value
    statement = select(Win5Round).where(
        Win5Round.season_id == season_id,
        Win5Round.status == round_status,
    )
    if scope == "open":
        statement = statement.order_by(Win5Round.round_number, Win5Round.id)
    else:
        submitted_round_ids = select(Win5Entry.round_id).where(
            Win5Entry.game_account_id == game_account_id,
        )
        statement = (
            statement.where(Win5Round.id.in_(submitted_round_ids))
            .order_by(Win5Round.round_number.desc(), Win5Round.id.desc())
            .limit(25)
        )

    summaries: list[Win5SubmissionRoundDTO] = []
    for round_ in session.scalars(statement):
        entries = tuple(
            session.scalars(
                select(Win5Entry)
                .where(
                    Win5Entry.round_id == round_.id,
                    Win5Entry.game_account_id == game_account_id,
                )
                .order_by(Win5Entry.id)
            )
        )
        entry_dtos = tuple(_entry_dto(session, entry) for entry in entries)
        judgement_dtos: tuple[Win5JudgementDTO, ...] = ()
        if scope == "scored":
            ownership = _validate_win5_round_ownership(session, round_=round_, lock=False)
            results = _load_round_results(
                session,
                round_=round_,
                ownership=ownership,
                lock=False,
            )
            _validate_round_results(
                round_=round_,
                ownership=ownership,
                results=results,
            )
            result_by_race_id = {result.race_id: result for result in results}
            judgement_rows = session.execute(
                select(Win5Judgement, Win5Entry)
                .join(Win5Entry, Win5Entry.id == Win5Judgement.win5_entry_id)
                .where(
                    Win5Entry.round_id == round_.id,
                    Win5Entry.game_account_id == game_account_id,
                )
                .order_by(Win5Entry.id)
            ).all()
            judgement_dtos = tuple(
                _judgement_dto(
                    session,
                    judgement,
                    game_account_id=game_account_id,
                    result_order=_result_order_for_entry(
                        round_=round_,
                        ownership=ownership,
                        entry=entry,
                        result_by_race_id=result_by_race_id,
                    ),
                )
                for judgement, entry in judgement_rows
            )

        summaries.append(
            Win5SubmissionRoundDTO(
                round=_round_dto(session, round_),
                submissions=entry_dtos,
                judgements=judgement_dtos,
            )
        )
    return tuple(summaries)


def list_win5_standings(
    session: Session,
    *,
    season_id: int,
    ranking: str = "season",
) -> tuple[Win5StandingDTO, ...]:
    season_id = _positive_int(season_id, field="season ID")
    if ranking not in {"season", "top1"}:
        raise Win5LifecycleError("WIN5 ranking must be season or top1")
    if session.get(Win5Season, season_id) is None:
        raise Win5LifecycleError("WIN5 season not found")
    score_column = Win5Score.season_score if ranking == "season" else Win5Score.top1_score
    rows = session.execute(
        select(Win5Score, GameAccount)
        .join(GameAccount, GameAccount.id == Win5Score.game_account_id)
        .where(Win5Score.season_id == season_id)
        .order_by(score_column.desc(), Win5Score.game_account_id)
    ).all()
    standings: list[Win5StandingDTO] = []
    previous_score: int | None = None
    previous_rank = 0
    for position, (score, account) in enumerate(rows, start=1):
        value = score.season_score if ranking == "season" else score.top1_score
        if value != previous_score:
            previous_rank = position
            previous_score = value
        standings.append(
            Win5StandingDTO(
                rank=previous_rank,
                game_account_id=account.id,
                display_name=account.ingame_name or account.nickname or f"Account #{account.id}",
                score=value,
            )
        )
    return tuple(standings)


def _transition_season(
    session: Session,
    *,
    command: Win5SeasonTransitionCommand,
    action: str,
    expected: Win5SeasonStatus,
    target: Win5SeasonStatus,
) -> Win5MutationResultDTO:
    season_id = _positive_int(command.season_id, field="season ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    fingerprint = _fingerprint(action, {"season_id": season_id, "actor": actor})
    try:
        with session.begin_nested():
            locked_seasons: tuple[Win5Season, ...] | None = None
            if target is Win5SeasonStatus.ACTIVE:
                locked_seasons = _lock_seasons_in_global_order(session)
                season = next((candidate for candidate in locked_seasons if candidate.id == season_id), None)
                if season is None:
                    raise Win5LifecycleError("WIN5 season not found")
            else:
                season = _lock_season(session, season_id)
            existing = _load_audit(session, request_key)
            if existing is not None:
                return _mutation_from_audit(session, existing, action=action, fingerprint=fingerprint)
            if season.status != expected.value:
                raise Win5LifecycleError(f"WIN5 season must be {expected.value} before {target.value}")
            if target is Win5SeasonStatus.ACTIVE:
                assert locked_seasons is not None
                other_active = next(
                    (
                        candidate
                        for candidate in locked_seasons
                        if candidate.id != season.id and candidate.status == Win5SeasonStatus.ACTIVE.value
                    ),
                    None,
                )
                if other_active is not None:
                    raise Win5LifecycleError("another WIN5 season is already active")
            if target is Win5SeasonStatus.CLOSED:
                incomplete_round = session.scalar(
                    select(Win5Round.id).where(
                        Win5Round.season_id == season.id,
                        Win5Round.status != Win5RoundStatus.SCORED.value,
                    )
                )
                if incomplete_round is not None:
                    raise Win5LifecycleError("all WIN5 rounds must be scored before closing the season")
            before = _season_json(season)
            season.status = target.value
            season.active_marker = "active" if target is Win5SeasonStatus.ACTIVE else None
            if target is Win5SeasonStatus.CANCELLED:
                season.season_number_marker = None
            session.flush()
            audit = _append_audit(
                session,
                action=action,
                capability=WIN5_SEASON_MANAGE_CAPABILITY,
                actor=actor,
                request_key=request_key,
                fingerprint=fingerprint,
                season_id=season.id,
                before=before,
                after=_season_json(season),
                reason=reason,
            )
            return Win5MutationResultDTO(action=action, audit_id=audit.id, season=_season_dto(season))
    except IntegrityError as exc:
        return _recover_mutation_retry(
            session,
            error=exc,
            request_key=request_key,
            action=action,
            fingerprint=fingerprint,
        )


def _lock_seasons_in_global_order(session: Session) -> tuple[Win5Season, ...]:
    return tuple(
        session.scalars(
            select(Win5Season).order_by(Win5Season.id).with_for_update().execution_options(populate_existing=True)
        )
    )


def _lock_seasons_for_number_allocation(
    session: Session,
) -> tuple[Win5Season, ...]:
    first_season_id = session.scalar(select(Win5Season.id).order_by(Win5Season.id).limit(1))
    if first_season_id is None:
        # Locking an empty InnoDB range gives concurrent creators matching gap
        # locks and can deadlock when both insert the first row. The unique
        # number marker safely arbitrates this one empty-table race instead.
        return ()
    return _lock_seasons_in_global_order(session)


def _transition_round(
    session: Session,
    *,
    command: Win5RoundTransitionCommand,
    action: str,
    expected: Win5RoundStatus,
    target: Win5RoundStatus,
) -> Win5MutationResultDTO:
    round_id = _positive_int(command.round_id, field="round ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    fingerprint = _fingerprint(action, {"round_id": round_id, "actor": actor})
    try:
        with session.begin_nested():
            initial = session.get(Win5Round, round_id)
            if initial is None:
                raise Win5LifecycleError("WIN5 round not found")
            season = _lock_season(session, initial.season_id)
            round_ = _lock_round(session, round_id)
            ownership = _validate_win5_round_ownership(session, round_=round_, lock=True)
            existing = _load_audit(session, request_key)
            if existing is not None:
                return _mutation_from_audit(session, existing, action=action, fingerprint=fingerprint)
            if round_.season_id != season.id:
                raise Win5MutationConflictError("WIN5 round provenance changed")
            if round_.status != expected.value:
                raise Win5LifecycleError(f"WIN5 round must be {expected.value} before {target.value}")
            if target is Win5RoundStatus.OPEN:
                if season.status != Win5SeasonStatus.ACTIVE.value:
                    raise Win5LifecycleError("WIN5 season must be active before opening a round")
                if _ownership_has_result(session, ownership=ownership):
                    raise Win5LifecycleError("WIN5 round contains a race with an authoritative result")
                open_round_ids = tuple(
                    session.scalars(
                        select(Win5Round.id)
                        .where(
                            Win5Round.season_id == season.id,
                            Win5Round.status == Win5RoundStatus.OPEN.value,
                            Win5Round.id != round_.id,
                        )
                        .order_by(Win5Round.id)
                        .with_for_update()
                    )
                )
                if len(open_round_ids) >= MAX_OPEN_ROUNDS_PER_SEASON:
                    raise Win5LifecycleError("WIN5 season already has three open rounds")
            before = _round_json_from_db(session, round_)
            round_.status = target.value
            session.flush()
            audit = _append_audit(
                session,
                action=action,
                capability=WIN5_ROUND_MANAGE_CAPABILITY,
                actor=actor,
                request_key=request_key,
                fingerprint=fingerprint,
                season_id=season.id,
                round_id=round_.id,
                before=before,
                after=_round_json_from_db(session, round_),
                reason=reason,
            )
            return Win5MutationResultDTO(
                action=action,
                audit_id=audit.id,
                round=_round_dto(session, round_),
            )
    except IntegrityError as exc:
        return _recover_mutation_retry(
            session,
            error=exc,
            request_key=request_key,
            action=action,
            fingerprint=fingerprint,
        )


def _lock_season(
    session: Session,
    season_id: int,
    *,
    error_type: type[Win5RuleError] = Win5LifecycleError,
) -> Win5Season:
    season = session.scalar(
        select(Win5Season)
        .where(Win5Season.id == season_id)
        .order_by(Win5Season.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if season is None:
        raise error_type("WIN5 season not found")
    return season


def _lock_round(
    session: Session,
    round_id: int,
    *,
    error_type: type[Win5RuleError] = Win5LifecycleError,
) -> Win5Round:
    round_ = session.scalar(
        select(Win5Round)
        .where(Win5Round.id == round_id)
        .order_by(Win5Round.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if round_ is None:
        raise error_type("WIN5 round not found")
    return round_


def _load_audit(
    session: Session,
    request_key: str,
    *,
    lock: bool = False,
) -> Win5OperationAudit | None:
    query = select(Win5OperationAudit).where(Win5OperationAudit.idempotency_key == request_key)
    if lock:
        query = query.with_for_update()
    return session.scalar(query.execution_options(populate_existing=True))


def _append_audit(
    session: Session,
    *,
    action: str,
    capability: str,
    actor: str,
    request_key: str,
    fingerprint: str,
    season_id: int | None,
    before: dict[str, object] | None,
    after: dict[str, object],
    reason: str | None,
    round_id: int | None = None,
    win5_entry_id: int | None = None,
) -> Win5OperationAudit:
    audit = Win5OperationAudit(
        action=action,
        capability=capability,
        actor_discord_user_id=actor,
        season_id=season_id,
        round_id=round_id,
        win5_entry_id=win5_entry_id,
        idempotency_key=request_key,
        request_fingerprint=fingerprint,
        before_json=before,
        after_json=after,
        reason=reason,
    )
    session.add(audit)
    session.flush()
    return audit


def _mutation_from_audit(
    session: Session,
    audit: Win5OperationAudit,
    *,
    action: str,
    fingerprint: str,
) -> Win5MutationResultDTO:
    _validate_audit(audit, action=action, fingerprint=fingerprint)
    season = session.get(Win5Season, audit.season_id) if audit.season_id is not None else None
    round_ = session.get(Win5Round, audit.round_id) if audit.round_id is not None else None
    entry = session.get(Win5Entry, audit.win5_entry_id) if audit.win5_entry_id is not None else None
    return Win5MutationResultDTO(
        action=action,
        audit_id=audit.id,
        season=_season_dto(season) if season is not None else None,
        round=_round_dto(session, round_) if round_ is not None else None,
        submission=_entry_dto(session, entry) if entry is not None else None,
    )


def _submission_from_audit(
    session: Session,
    audit: Win5OperationAudit,
    *,
    action: str,
    fingerprint: str,
) -> Win5EntryDTO:
    _validate_audit(audit, action=action, fingerprint=fingerprint)
    if audit.win5_entry_id is None:
        raise Win5MutationConflictError("WIN5 audit target is missing")
    entry = session.get(Win5Entry, audit.win5_entry_id)
    if entry is None:
        raise Win5MutationConflictError("WIN5 audit target changed")
    return _entry_dto(session, entry)


def _validate_audit(audit: Win5OperationAudit, *, action: str, fingerprint: str) -> None:
    if audit.action != action or audit.request_fingerprint != fingerprint:
        raise Win5MutationConflictError("WIN5 idempotency key was already used with different input")


def _recover_mutation_retry(
    session: Session,
    *,
    error: IntegrityError,
    request_key: str,
    action: str,
    fingerprint: str,
) -> Win5MutationResultDTO:
    session.expire_all()
    audit = _load_audit(session, request_key, lock=True)
    if audit is None:
        raise error
    return _mutation_from_audit(session, audit, action=action, fingerprint=fingerprint)


def _recover_season_create_integrity(
    session: Session,
    *,
    error: IntegrityError,
    request_key: str,
    action: str,
    fingerprint: str,
) -> Win5MutationResultDTO:
    session.expire_all()
    audit = _load_audit(session, request_key, lock=True)
    if audit is not None:
        return _mutation_from_audit(session, audit, action=action, fingerprint=fingerprint)
    if _is_season_number_marker_unique_violation(error):
        raise Win5MutationConflictError("WIN5 season numbering changed concurrently; retry the request") from error
    raise error


def _recover_round_create_integrity(
    session: Session,
    *,
    error: IntegrityError,
    request_key: str,
    action: str,
    fingerprint: str,
) -> Win5MutationResultDTO:
    session.expire_all()
    audit = _load_audit(session, request_key, lock=True)
    if audit is not None:
        return _mutation_from_audit(session, audit, action=action, fingerprint=fingerprint)
    if _is_round_number_unique_violation(error):
        raise Win5MutationConflictError("WIN5 season already has this round number") from error
    if _is_special_round_race_unique_violation(error):
        raise Win5MutationConflictError("WIN5 race already belongs to a special round") from error
    if _is_race_event_foreign_key_violation(error):
        raise Win5MutationConflictError("WIN5 round event does not exist") from error
    raise error


def _result_mutation_from_audit(
    session: Session,
    audit: Win5OperationAudit,
    *,
    action: str,
    fingerprint: str,
) -> Win5ResultMutationDTO:
    _validate_audit(audit, action=action, fingerprint=fingerprint)
    if audit.round_id is None:
        raise Win5MutationConflictError("WIN5 result audit target is missing")
    round_ = session.get(Win5Round, audit.round_id, populate_existing=True)
    if round_ is None:
        raise Win5MutationConflictError("WIN5 result audit round changed")
    ownership = _validate_win5_round_ownership(session, round_=round_, lock=False)
    result_snapshot = audit.after_json.get("result")
    if not isinstance(result_snapshot, dict) or not isinstance(result_snapshot.get("id"), int):
        raise Win5MutationConflictError("WIN5 result audit target changed")
    result = session.get(Win5Result, result_snapshot["id"], populate_existing=True)
    if result is None:
        raise Win5MutationConflictError("WIN5 result audit target changed")
    _validate_result_provenance(
        round_=round_,
        ownership=ownership,
        result=result,
    )
    return Win5ResultMutationDTO(
        action=action,
        audit_id=audit.id,
        round=_round_dto(session, round_),
        result=_result_dto(result),
    )


def _scoring_from_audit(
    session: Session,
    audit: Win5OperationAudit,
    *,
    action: str,
    fingerprint: str,
) -> Win5ScoringResultDTO:
    _validate_audit(audit, action=action, fingerprint=fingerprint)
    if audit.round_id is None:
        raise Win5MutationConflictError("WIN5 scoring audit target is missing")
    round_ = session.get(Win5Round, audit.round_id, populate_existing=True)
    if round_ is None or round_.status != Win5RoundStatus.SCORED.value:
        raise Win5MutationConflictError("WIN5 scored round changed")
    ownership = _validate_win5_round_ownership(session, round_=round_, lock=False)
    results = _load_round_results(
        session,
        round_=round_,
        ownership=ownership,
        lock=False,
    )
    _validate_round_results(round_=round_, ownership=ownership, results=results)
    result_by_race_id = {result.race_id: result for result in results}
    rows = session.execute(
        select(Win5Judgement, Win5Entry)
        .join(Win5Entry, Win5Entry.id == Win5Judgement.win5_entry_id)
        .where(
            Win5Entry.round_id == round_.id,
            Win5Entry.status == Win5SubmissionStatus.ACCEPTED.value,
        )
        .order_by(Win5Entry.id)
    ).all()
    all_accepted_entries = tuple(
        session.scalars(
            select(Win5Entry)
            .where(
                Win5Entry.round_id == round_.id,
                Win5Entry.status == Win5SubmissionStatus.ACCEPTED.value,
            )
            .order_by(Win5Entry.id)
        )
    )
    accepted_count = len(
        _scorable_entries(
            round_=round_,
            ownership=ownership,
            entries=all_accepted_entries,
        )
    )
    if accepted_count != len(rows):
        raise Win5MutationConflictError("WIN5 scored judgement set changed")
    judgements = tuple(
        _judgement_dto(
            session,
            judgement,
            game_account_id=entry.game_account_id,
            result_order=_result_order_for_entry(
                round_=round_,
                ownership=ownership,
                entry=entry,
                result_by_race_id=result_by_race_id,
            ),
        )
        for judgement, entry in rows
    )
    account_ids = tuple(sorted({entry.game_account_id for _judgement, entry in rows}))
    current_scores = {
        score.game_account_id: score
        for score in session.scalars(
            select(Win5Score).where(
                Win5Score.season_id == round_.season_id,
                Win5Score.game_account_id.in_(account_ids),
            )
        )
    }
    scores = _score_snapshots_from_audit(audit)
    if tuple(score.game_account_id for score in scores) != account_ids:
        raise Win5MutationConflictError("WIN5 score projection changed")
    if any(
        account_id not in current_scores
        or current_scores[account_id].season_score < snapshot.season_score
        or current_scores[account_id].top1_score < snapshot.top1_score
        for account_id, snapshot in zip(account_ids, scores, strict=True)
    ):
        raise Win5MutationConflictError("WIN5 score projection changed")
    return Win5ScoringResultDTO(
        action=action,
        audit_id=audit.id,
        round=_round_dto(session, round_),
        result=_result_dto(results[0]) if round_.round_type == Win5RoundType.NORMAL.value else None,
        results=tuple(_result_dto(result) for result in results),
        judgements=judgements,
        scores=scores,
    )


def _validate_result_belongs_to_round(
    *,
    entries: Sequence[RaceEntry],
    result_order: Sequence[int],
) -> None:
    entry_numbers = {entry.entry_number for entry in entries}
    if not set(result_order).issubset(entry_numbers):
        raise Win5LifecycleError("WIN5 result must reference entries in the selected round")


def _normalize_stored_result_order(
    result_order: Sequence[int],
) -> tuple[int, ...]:
    if not isinstance(result_order, (str, bytes)) and isinstance(result_order, Sequence) and len(result_order) == 1:
        return normalize_win5_special_result_order(result_order)
    return normalize_win5_result_order(result_order)


def _normalize_result_order_for_round(
    round_: Win5Round,
    result_order: Sequence[int],
) -> tuple[int, ...]:
    if round_.round_type == Win5RoundType.SPECIAL.value:
        return normalize_win5_special_result_order(result_order)
    return normalize_win5_result_order(result_order)


def _validate_result_provenance(
    *,
    round_: Win5Round,
    ownership: _Win5RoundOwnership,
    result: Win5Result,
) -> None:
    if round_.round_type == Win5RoundType.NORMAL.value:
        selected = (
            (ownership.normal_race, ownership.normal_entries)
            if ownership.normal_race is not None and result.race_id == ownership.normal_race.id
            else None
        )
    else:
        item = next(
            (special_race for special_race in ownership.special_races if special_race.race.id == result.race_id),
            None,
        )
        selected = (item.race, item.entries) if item is not None else None
    if selected is None:
        raise Win5MutationConflictError("WIN5 result race provenance changed")
    race, entries = selected
    _validate_result_for_race(
        race=race,
        entries=entries,
        result=result,
        special=round_.round_type == Win5RoundType.SPECIAL.value,
    )


def _validate_result_for_race(
    *,
    race: Race,
    entries: Sequence[RaceEntry],
    result: Win5Result,
    special: bool,
) -> None:
    if result.race_id != race.id:
        raise Win5MutationConflictError("WIN5 result race provenance changed")
    if result.event_id != race.event_id:
        raise Win5MutationConflictError("WIN5 result event provenance changed")
    result_order = (
        normalize_win5_special_result_order(result.result_order)
        if special
        else normalize_win5_result_order(result.result_order)
    )
    if entries and not set(result_order).issubset({entry.entry_number for entry in entries}):
        raise Win5MutationConflictError("WIN5 result entries changed")


def _ownership_race_ids(
    ownership: _Win5RoundOwnership,
) -> tuple[int, ...]:
    if ownership.normal_race is not None:
        return (ownership.normal_race.id,)
    return tuple(item.race.id for item in ownership.special_races)


def _ownership_has_result(
    session: Session,
    *,
    ownership: _Win5RoundOwnership,
) -> bool:
    race_ids = _ownership_race_ids(ownership)
    return (
        session.scalar(
            select(Win5Result.id).where(Win5Result.race_id.in_(race_ids)).order_by(Win5Result.race_id).limit(1)
        )
        is not None
    )


def _race_has_result(
    session: Session,
    *,
    race_id: int,
) -> bool:
    return session.scalar(select(Win5Result.id).where(Win5Result.race_id == race_id).limit(1)) is not None


def _active_other_round_for_race(
    session: Session,
    *,
    race_id: int,
    current_round_id: int,
) -> int | None:
    active_statuses = (
        Win5RoundStatus.SETUP.value,
        Win5RoundStatus.OPEN.value,
    )
    normal_round_id = session.scalar(
        select(Win5Round.id)
        .where(
            Win5Round.race_id == race_id,
            Win5Round.id != current_round_id,
            Win5Round.status.in_(active_statuses),
        )
        .order_by(Win5Round.id)
        .limit(1)
    )
    if normal_round_id is not None:
        return normal_round_id
    return session.scalar(
        select(Win5Round.id)
        .join(Win5RoundRace, Win5RoundRace.round_id == Win5Round.id)
        .where(
            Win5RoundRace.race_id == race_id,
            Win5Round.id != current_round_id,
            Win5Round.status.in_(active_statuses),
        )
        .order_by(Win5Round.id)
        .limit(1)
    )


def _validate_round_results(
    *,
    round_: Win5Round,
    ownership: _Win5RoundOwnership,
    results: Sequence[Win5Result],
) -> None:
    for result in results:
        _validate_result_provenance(
            round_=round_,
            ownership=ownership,
            result=result,
        )
    result_race_ids = tuple(sorted(result.race_id for result in results))
    if len(result_race_ids) != len(set(result_race_ids)):
        raise Win5MutationConflictError("WIN5 round has duplicate race results")
    if round_.round_type == Win5RoundType.NORMAL.value:
        required_race_ids = (round_.race_id,)
    else:
        required_race_ids = tuple(
            sorted(item.race.id for item in ownership.special_races if item.race.status != "voided")
        )
    if not set(required_race_ids).issubset(result_race_ids):
        raise Win5MutationConflictError("WIN5 round result set changed")


def _load_round_results(
    session: Session,
    *,
    round_: Win5Round,
    ownership: _Win5RoundOwnership,
    lock: bool,
) -> tuple[Win5Result, ...]:
    if round_.round_type == Win5RoundType.NORMAL.value:
        race_ids = (round_.race_id,)
    else:
        race_ids = tuple(item.race.id for item in ownership.special_races)
    query = (
        select(Win5Result)
        .where(Win5Result.race_id.in_(race_ids))
        .order_by(Win5Result.race_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    return tuple(session.scalars(query))


def _special_race_for_entry(
    *,
    ownership: _Win5RoundOwnership,
    entry: Win5Entry,
) -> _Win5SpecialRaceOwnership:
    selected = next(
        (item for item in ownership.special_races if item.round_race.id == entry.special_round_race_id),
        None,
    )
    if selected is None:
        raise Win5MutationConflictError("WIN5 special submission race changed")
    return selected


def _scorable_entries(
    *,
    round_: Win5Round,
    ownership: _Win5RoundOwnership,
    entries: Sequence[Win5Entry],
) -> tuple[Win5Entry, ...]:
    if round_.round_type == Win5RoundType.NORMAL.value:
        return tuple(entries)
    voided_membership_ids = {item.round_race.id for item in ownership.special_races if item.race.status == "voided"}
    return tuple(entry for entry in entries if entry.special_round_race_id not in voided_membership_ids)


def _result_order_for_entry(
    *,
    round_: Win5Round,
    ownership: _Win5RoundOwnership,
    entry: Win5Entry,
    result_by_race_id: dict[int, Win5Result],
) -> tuple[int, ...]:
    if entry.prediction_tier == Win5PredictionTier.SPECIAL_WINNER.value:
        race_id = _special_race_for_entry(ownership=ownership, entry=entry).race.id
        normalize_result = normalize_win5_special_result_order
    else:
        if round_.race_id is None:
            raise Win5MutationConflictError("WIN5 normal submission race changed")
        race_id = round_.race_id
        normalize_result = normalize_win5_result_order
    result = result_by_race_id.get(race_id)
    if result is None:
        raise Win5MutationConflictError("WIN5 scored result changed")
    return normalize_result(result.result_order)


def _validate_unscored_entries(
    session: Session,
    *,
    round_: Win5Round,
    entries: Sequence[Win5Entry],
) -> None:
    entry_ids = tuple(entry.id for entry in entries)
    any_judgement = session.scalar(
        select(Win5Judgement.id)
        .join(Win5Entry, Win5Entry.id == Win5Judgement.win5_entry_id)
        .where(Win5Entry.round_id == round_.id)
        .limit(1)
    )
    any_score_event = session.scalar(
        select(Win5ScoreEvent.id)
        .join(Win5Entry, Win5Entry.id == Win5ScoreEvent.win5_entry_id)
        .where(Win5Entry.round_id == round_.id)
        .limit(1)
    )
    if any_judgement is not None or any_score_event is not None:
        raise Win5MutationConflictError("WIN5 round already has partial scoring rows")
    if entry_ids:
        pick_counts = dict(
            session.execute(
                select(Win5Pick.win5_entry_id, func.count())
                .where(Win5Pick.win5_entry_id.in_(entry_ids))
                .group_by(Win5Pick.win5_entry_id)
            ).all()
        )
        for entry in entries:
            try:
                tier = normalize_prediction_tier(
                    entry.prediction_tier,
                    allow_special=True,
                )
            except Win5RuleError as exc:
                raise Win5MutationConflictError("WIN5 accepted submission tier changed") from exc
            if pick_counts.get(entry.id) != PICK_COUNT_BY_TIER[tier]:
                raise Win5MutationConflictError("WIN5 accepted submission picks changed")


def _lock_game_accounts(session: Session, account_ids: Sequence[int]) -> None:
    if not account_ids:
        return
    locked_ids = tuple(
        session.scalars(
            select(GameAccount.id).where(GameAccount.id.in_(account_ids)).order_by(GameAccount.id).with_for_update()
        )
    )
    if locked_ids != tuple(account_ids):
        raise Win5MutationConflictError("WIN5 scoring account set changed")


def _lock_circle_point_accounts(
    session: Session,
    *,
    account_ids: Sequence[int],
) -> dict[int, CirclePointAccount]:
    if not account_ids:
        return {}
    try:
        wallets = lock_persona_wallets_for_game_accounts(session, game_account_ids=account_ids)
    except AccountNotFoundError as exc:
        raise Win5MutationConflictError("WIN5 scoring room point account set changed") from exc
    return {game_account_id: wallet for game_account_id, (_game_account, wallet) in wallets.items()}


def _require_confirmed_win5_game_account(session: Session, *, game_account_id: int) -> None:
    account = session.scalar(
        select(GameAccount)
        .where(GameAccount.id == game_account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise AccountNotFoundError("game account not found")
    if account.uma_pid is None or account.identity_status != IdentityStatus.CONFIRMED.value:
        raise IdentityStateError("WIN5 submission requires a confirmed PID registration")


def _lock_scores(
    session: Session,
    *,
    season_id: int,
    account_ids: Sequence[int],
) -> dict[int, Win5Score]:
    if not account_ids:
        return {}
    return {
        score.game_account_id: score
        for score in session.scalars(
            select(Win5Score)
            .where(
                Win5Score.season_id == season_id,
                Win5Score.game_account_id.in_(account_ids),
            )
            .order_by(Win5Score.game_account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }


def _load_entry_picks(
    session: Session,
    entry_id: int,
    *,
    prediction_tier: Win5PredictionTier | str | None = None,
) -> tuple[int, ...]:
    picks = tuple(
        session.scalars(
            select(Win5Pick.entry_number).where(Win5Pick.win5_entry_id == entry_id).order_by(Win5Pick.pick_order)
        )
    )
    try:
        if prediction_tier is None:
            stored_tier = session.scalar(select(Win5Entry.prediction_tier).where(Win5Entry.id == entry_id))
            if stored_tier is None:
                raise Win5RuleError("WIN5 prediction tier is missing")
            prediction_tier = stored_tier
        return normalize_win5_picks(
            picks,
            prediction_tier=prediction_tier,
        )
    except Win5RuleError as exc:
        raise Win5MutationConflictError("WIN5 accepted submission picks changed") from exc


def _is_round_number_unique_violation(error: IntegrityError) -> bool:
    message = _integrity_error_message(error)
    return (
        "uq_win5_rounds_season_round_number" in message
        or "unique constraint failed: win5_rounds.season_id, win5_rounds.round_number" in message
    )


def _is_season_number_marker_unique_violation(error: IntegrityError) -> bool:
    message = _integrity_error_message(error)
    return (
        "uq_win5_seasons_season_number_marker" in message
        or "unique constraint failed: win5_seasons.season_number_marker" in message
    )


def _is_special_round_race_unique_violation(error: IntegrityError) -> bool:
    message = _integrity_error_message(error)
    return "uq_win5_round_races_race_id" in message or "unique constraint failed: win5_round_races.race_id" in message


def _is_race_event_foreign_key_violation(error: IntegrityError) -> bool:
    if _integrity_error_code(error) != 1452:
        return False
    message = _integrity_error_message(error)
    return (
        "foreign key (`event_id`) references `game_events` (`id`)" in message
        or "foreign key (event_id) references game_events (id)" in message
    )


def _integrity_error_code(error: IntegrityError) -> int | None:
    original = error.orig
    errno = getattr(original, "errno", None)
    if isinstance(errno, int):
        return errno
    arguments = getattr(original, "args", ())
    if arguments and isinstance(arguments[0], int):
        return arguments[0]
    return None


def _integrity_error_message(error: IntegrityError) -> str:
    return " ".join(str(argument) for argument in getattr(error.orig, "args", (error.orig,))).lower()


def _validate_win5_round_ownership(
    session: Session,
    *,
    round_: Win5Round,
    lock: bool,
) -> _Win5RoundOwnership:
    try:
        round_type = Win5RoundType(round_.round_type)
    except ValueError as exc:
        raise Win5MutationConflictError("WIN5 round ownership graph is invalid: round type changed") from exc
    if round_type is Win5RoundType.NORMAL:
        if round_.race_id is None:
            raise Win5MutationConflictError("WIN5 round ownership graph is invalid: race is missing")
        race, entries = _load_win5_race_ownership(
            session,
            race_id=round_.race_id,
            lock=lock,
            require_entries=True,
        )
        special_race = session.scalar(select(Win5RoundRace.id).where(Win5RoundRace.round_id == round_.id).limit(1))
        if special_race is not None:
            raise Win5MutationConflictError("WIN5 normal round has special race membership")
        return _Win5RoundOwnership(
            normal_race=race,
            normal_entries=entries,
            special_races=(),
        )

    if round_.race_id is not None:
        raise Win5MutationConflictError("WIN5 special round has a direct race")
    membership_query = (
        select(Win5RoundRace)
        .where(Win5RoundRace.round_id == round_.id)
        .order_by(Win5RoundRace.display_order, Win5RoundRace.id)
    )
    if lock:
        membership_query = membership_query.with_for_update()
    memberships = tuple(session.scalars(membership_query.execution_options(populate_existing=True)))
    if not memberships:
        raise Win5MutationConflictError("WIN5 special round has no races")
    expected_orders = tuple(range(1, len(memberships) + 1))
    if tuple(membership.display_order for membership in memberships) != expected_orders:
        raise Win5MutationConflictError("WIN5 special round race order changed")
    special_races: list[_Win5SpecialRaceOwnership] = []
    for membership in memberships:
        race, entries = _load_win5_race_ownership(
            session,
            race_id=membership.race_id,
            lock=lock,
            require_entries=False,
        )
        special_races.append(
            _Win5SpecialRaceOwnership(
                round_race=membership,
                race=race,
                entries=entries,
            )
        )
    return _Win5RoundOwnership(
        normal_race=None,
        normal_entries=(),
        special_races=tuple(special_races),
    )


def _load_win5_race_ownership(
    session: Session,
    *,
    race_id: int,
    lock: bool,
    require_entries: bool,
) -> tuple[Race, tuple[RaceEntry, ...]]:
    race_query = select(Race).where(Race.id == race_id)
    if lock:
        race_query = race_query.with_for_update()
    race = session.scalar(race_query.execution_options(populate_existing=True))
    if race is None:
        raise Win5MutationConflictError("WIN5 round ownership graph is invalid: race is missing")
    if race.race_kind != "win5":
        raise Win5MutationConflictError("WIN5 round ownership graph is invalid: race kind is not win5")
    entries_query = select(RaceEntry).where(RaceEntry.race_id == race.id).order_by(RaceEntry.entry_number)
    if lock:
        entries_query = entries_query.with_for_update()
    entries = tuple(session.scalars(entries_query.execution_options(populate_existing=True)))
    if require_entries and len(entries) < 5:
        raise Win5MutationConflictError("WIN5 round ownership graph is invalid: fewer than five entries")
    if any(entry.entry_kind != "win5_horse" for entry in entries):
        raise Win5MutationConflictError("WIN5 round ownership graph is invalid: incompatible race entry")
    return race, entries


def _validate_picks_belong_to_round(
    *,
    entries: Sequence[RaceEntry],
    picks: Sequence[int],
) -> None:
    existing = {entry.entry_number for entry in entries if entry.entry_number in picks}
    if existing != set(picks):
        raise Win5SubmissionError("WIN5 picks must reference entries in the selected round")


def _normalize_round_entries(entries: Sequence[Win5RoundEntryInput]) -> tuple[Win5RoundEntryInput, ...]:
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
        raise Win5LifecycleError("WIN5 round entries must be a sequence")
    normalized = tuple(
        Win5RoundEntryInput(
            entry_number=_positive_int(entry.entry_number, field="entry number"),
            display_name=_text(entry.display_name, field="entry display name", maximum=100),
        )
        for entry in entries
    )
    if len(normalized) < 5:
        raise Win5LifecycleError("WIN5 round requires at least five entries")
    numbers = [entry.entry_number for entry in normalized]
    if len(set(numbers)) != len(numbers):
        raise Win5LifecycleError("WIN5 round entry numbers must be unique")
    return tuple(sorted(normalized, key=lambda entry: entry.entry_number))


def _normalize_special_races(
    races: Sequence[Win5SpecialRaceInput],
) -> tuple[Win5SpecialRaceInput, ...]:
    if isinstance(races, (str, bytes)) or not isinstance(races, Sequence):
        raise Win5LifecycleError("WIN5 special races must be a sequence")
    normalized = tuple(
        Win5SpecialRaceInput(
            race_name=_text(race.race_name, field="race name", maximum=200),
            starts_at=_optional_datetime(race.starts_at, field="race start time"),
            entries=(),
        )
        for race in races
    )
    if any(race.entries for race in races):
        raise Win5LifecycleError("WIN5 special races must be created with race names only")
    if not normalized:
        raise Win5LifecycleError("WIN5 special round requires at least one race")
    race_names = [race.race_name for race in normalized]
    if len(set(race_names)) != len(race_names):
        raise Win5LifecycleError("WIN5 special round race names must be unique")
    return normalized


def _season_dto(season: Win5Season) -> Win5SeasonDTO:
    if season.season_number is None:
        raise Win5LifecycleError("WIN5 season number is unavailable")
    return Win5SeasonDTO(
        id=season.id,
        season_number=season.season_number,
        name=season.name,
        starts_at=_stored_optional_datetime(season.starts_at),
        ends_at=_stored_optional_datetime(season.ends_at),
        status=season.status,
        created_at=database_datetime_as_utc(season.created_at),
        updated_at=database_datetime_as_utc(season.updated_at),
    )


def _round_dto(session: Session, round_: Win5Round) -> Win5RoundDTO:
    ownership = _validate_win5_round_ownership(session, round_=round_, lock=False)
    if ownership.normal_race is not None:
        race_name = ownership.normal_race.name
        race_starts_at = _stored_optional_datetime(ownership.normal_race.starts_at)
        entries = _race_entry_dtos(ownership.normal_entries)
        special_races: tuple[Win5RoundRaceDTO, ...] = ()
    else:
        race_name = None
        race_starts_at = None
        entries = ()
        special_races = tuple(
            Win5RoundRaceDTO(
                id=item.round_race.id,
                race_id=item.race.id,
                display_order=item.round_race.display_order,
                race_name=item.race.name,
                race_starts_at=_stored_optional_datetime(item.race.starts_at),
                entries=_race_entry_dtos(item.entries),
            )
            for item in ownership.special_races
        )
    return Win5RoundDTO(
        id=round_.id,
        season_id=round_.season_id,
        race_id=round_.race_id,
        round_type=round_.round_type,
        round_number=round_.round_number,
        round_label=round_.round_label,
        status=round_.status,
        opens_at=_stored_optional_datetime(round_.opens_at),
        closes_at=_stored_optional_datetime(round_.closes_at),
        race_name=race_name,
        race_starts_at=race_starts_at,
        entries=entries,
        special_races=special_races,
        created_at=database_datetime_as_utc(round_.created_at),
        updated_at=database_datetime_as_utc(round_.updated_at),
    )


def _race_entry_dtos(entries: Sequence[RaceEntry]) -> tuple[Win5RaceEntryDTO, ...]:
    return tuple(
        Win5RaceEntryDTO(
            entry_number=entry.entry_number,
            display_name=entry.horse_name_or_label or "",
        )
        for entry in entries
    )


def _entry_dto(session: Session, entry: Win5Entry) -> Win5EntryDTO:
    picks = tuple(
        session.scalars(
            select(Win5Pick.entry_number).where(Win5Pick.win5_entry_id == entry.id).order_by(Win5Pick.pick_order)
        )
    )
    special_race_id = None
    if entry.special_round_race_id is not None:
        special_race_id = session.scalar(
            select(Win5RoundRace.race_id).where(Win5RoundRace.id == entry.special_round_race_id)
        )
        if special_race_id is None:
            raise Win5MutationConflictError("WIN5 special submission race membership changed")
    return Win5EntryDTO(
        id=entry.id,
        season_id=entry.season_id,
        round_id=entry.round_id,
        game_account_id=entry.game_account_id,
        prediction_tier=entry.prediction_tier,
        special_round_race_id=entry.special_round_race_id,
        special_race_id=special_race_id,
        picks=picks,
        status=entry.status,
        idempotency_key=entry.idempotency_key,
        cancelled_at=_stored_optional_datetime(entry.cancelled_at),
        cancelled_by_discord_user_id=entry.cancelled_by_discord_user_id,
        created_at=database_datetime_as_utc(entry.created_at),
        updated_at=database_datetime_as_utc(entry.updated_at),
    )


def _result_dto(result: Win5Result) -> Win5ResultDTO:
    if result.race_id is None:
        raise Win5MutationConflictError("WIN5 result race provenance is missing")
    return Win5ResultDTO(
        id=result.id,
        season_id=result.season_id,
        round_id=result.round_id,
        race_id=result.race_id,
        result_order=_normalize_stored_result_order(result.result_order),
        created_at=database_datetime_as_utc(result.created_at),
        updated_at=database_datetime_as_utc(result.updated_at),
    )


def _judgement_dto(
    session: Session,
    judgement: Win5Judgement,
    *,
    game_account_id: int,
    result_order: Sequence[int],
) -> Win5JudgementDTO:
    if judgement.judged_at is None or judgement.judged_by_discord_user_id is None:
        raise Win5MutationConflictError("WIN5 judgement audit provenance is missing")
    entry_tier = session.scalar(select(Win5Entry.prediction_tier).where(Win5Entry.id == judgement.win5_entry_id))
    if entry_tier != judgement.prediction_tier:
        raise Win5MutationConflictError("WIN5 judgement prediction tier changed")
    return Win5JudgementDTO(
        id=judgement.id,
        win5_entry_id=judgement.win5_entry_id,
        season_id=judgement.season_id,
        game_account_id=game_account_id,
        prediction_tier=judgement.prediction_tier,
        picks=_load_entry_picks(session, judgement.win5_entry_id),
        result_order=(
            normalize_win5_special_result_order(result_order)
            if judgement.prediction_tier == Win5PredictionTier.SPECIAL_WINNER.value
            else normalize_win5_result_order(result_order)
        ),
        exact_position_count=judgement.exact_position_count,
        on_board_wrong_position_count=judgement.on_board_wrong_position_count,
        off_board_count=judgement.off_board_count,
        season_score_delta=judgement.season_score_delta,
        top1_score_delta=judgement.top1_score_delta,
        judged_at=database_datetime_as_utc(judgement.judged_at),
        judged_by_discord_user_id=judgement.judged_by_discord_user_id,
    )


def _score_dto(score: Win5Score) -> Win5ScoreDTO:
    return Win5ScoreDTO(
        season_id=score.season_id,
        game_account_id=score.game_account_id,
        season_score=score.season_score,
        top1_score=score.top1_score,
        created_at=database_datetime_as_utc(score.created_at),
        updated_at=database_datetime_as_utc(score.updated_at),
    )


def _score_json(score: Win5ScoreDTO) -> dict[str, object]:
    return {
        "season_id": score.season_id,
        "game_account_id": score.game_account_id,
        "season_score": score.season_score,
        "top1_score": score.top1_score,
        "created_at": _datetime_json(score.created_at),
        "updated_at": _datetime_json(score.updated_at),
    }


def _score_snapshots_from_audit(audit: Win5OperationAudit) -> tuple[Win5ScoreDTO, ...]:
    values = audit.after_json.get("scores")
    if not isinstance(values, list):
        raise Win5MutationConflictError("WIN5 score audit snapshot changed")
    scores: list[Win5ScoreDTO] = []
    try:
        for value in values:
            if not isinstance(value, dict):
                raise ValueError
            season_id = value["season_id"]
            game_account_id = value["game_account_id"]
            season_score = value["season_score"]
            top1_score = value["top1_score"]
            if any(
                not isinstance(item, int) or isinstance(item, bool)
                for item in (
                    season_id,
                    game_account_id,
                    season_score,
                    top1_score,
                )
            ):
                raise ValueError
            scores.append(
                Win5ScoreDTO(
                    season_id=season_id,
                    game_account_id=game_account_id,
                    season_score=season_score,
                    top1_score=top1_score,
                    created_at=_datetime_from_json(value["created_at"]),
                    updated_at=_datetime_from_json(value["updated_at"]),
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise Win5MutationConflictError("WIN5 score audit snapshot changed") from exc
    return tuple(scores)


def _season_json(season: Win5Season) -> dict[str, object]:
    return {
        "id": season.id,
        "season_number": season.season_number,
        "name": season.name,
        "status": season.status,
        "starts_at": _datetime_json(season.starts_at),
        "ends_at": _datetime_json(season.ends_at),
    }


def _round_json(
    round_: Win5Round,
    *,
    race: Race,
    entries: Sequence[Win5RoundEntryInput],
) -> dict[str, object]:
    return {
        "id": round_.id,
        "season_id": round_.season_id,
        "race_id": race.id,
        "round_type": round_.round_type,
        "round_number": round_.round_number,
        "round_label": round_.round_label,
        "status": round_.status,
        "race_name": race.name,
        "race_starts_at": _datetime_json(race.starts_at),
        "entries": [asdict(entry) for entry in entries],
    }


def _round_json_from_db(session: Session, round_: Win5Round) -> dict[str, object]:
    dto = _round_dto(session, round_)
    return {
        "id": dto.id,
        "season_id": dto.season_id,
        "race_id": dto.race_id,
        "round_type": dto.round_type,
        "round_number": dto.round_number,
        "round_label": dto.round_label,
        "status": dto.status,
        "race_name": dto.race_name,
        "race_starts_at": _datetime_json(dto.race_starts_at),
        "entries": [asdict(entry) for entry in dto.entries],
        "special_races": [
            {
                "id": race.id,
                "race_id": race.race_id,
                "display_order": race.display_order,
                "race_name": race.race_name,
                "race_starts_at": _datetime_json(race.race_starts_at),
                "entries": [asdict(entry) for entry in race.entries],
            }
            for race in dto.special_races
        ],
    }


def _entry_json(entry: Win5EntryDTO) -> dict[str, object]:
    return {
        "id": entry.id,
        "season_id": entry.season_id,
        "round_id": entry.round_id,
        "game_account_id": entry.game_account_id,
        "prediction_tier": entry.prediction_tier,
        "special_round_race_id": entry.special_round_race_id,
        "picks": entry.picks,
        "status": entry.status,
        "cancelled_at": _datetime_json(entry.cancelled_at),
        "cancelled_by_discord_user_id": entry.cancelled_by_discord_user_id,
    }


def _result_json(result: Win5Result) -> dict[str, object]:
    return {
        "id": result.id,
        "season_id": result.season_id,
        "round_id": result.round_id,
        "race_id": result.race_id,
        "result_order": list(_normalize_stored_result_order(result.result_order)),
    }


def _fingerprint(action: str, payload: dict[str, object]) -> str:
    serialized = json.dumps(
        {"action": action, "payload": payload},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(serialized.encode("utf-8")).hexdigest()


def _positive_int(
    value: int,
    *,
    field: str,
    error_type: type[Win5RuleError] = Win5LifecycleError,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise error_type(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: int | None, *, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field=field)


def _text(
    value: str,
    *,
    field: str,
    maximum: int,
    error_type: type[Win5RuleError] = Win5LifecycleError,
) -> str:
    if not isinstance(value, str):
        raise error_type(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise error_type(f"{field} must contain between 1 and {maximum} characters")
    return normalized


def _optional_text(
    value: str | None,
    *,
    field: str,
    maximum: int,
    error_type: type[Win5RuleError] = Win5LifecycleError,
) -> str | None:
    if value is None:
        return None
    return _text(value, field=field, maximum=maximum, error_type=error_type)


def _datetime(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise Win5LifecycleError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise Win5LifecycleError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_datetime(value: datetime | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    return _datetime(value, field=field)


def _current_time(value: datetime | None) -> datetime:
    return datetime.now(UTC) if value is None else _datetime(value, field="current time")


def _stored_optional_datetime(value: datetime | None) -> datetime | None:
    return database_datetime_as_utc(value) if value is not None else None


def _datetime_json(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _datetime_from_json(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed.astimezone(UTC)
