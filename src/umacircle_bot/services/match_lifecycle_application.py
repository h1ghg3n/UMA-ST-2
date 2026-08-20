from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import Race
from umacircle_bot.services.application import run_application_command, run_application_query
from umacircle_bot.services.autocomplete_queries import (
    AutocompleteChoice,
    RoomRaceChoicePurpose,
    autocomplete_room_races,
)
from umacircle_bot.services.dtos import RaceOperationResultDTO
from umacircle_bot.services.match_races import (
    CloseMatchBettingCommand,
    CreateMatchRaceCommand,
    EditMatchRaceCommand,
    OpenMatchBettingCommand,
    RegisteredFinalEntrySnapshotInput,
    ReplaceRegisteredFinalEntrySnapshotCommand,
    SetRaceConditionCommand,
    close_match_betting,
    create_match_race,
    edit_match_race,
    get_match_race,
    open_match_betting,
    replace_registered_final_entry_snapshot,
    set_match_condition,
)


@dataclass(frozen=True, slots=True)
class MatchRaceCreateCommand:
    name: str
    starts_at: datetime
    description: str | None
    reason: str | None
    actor_discord_user_id: str
    interaction_id: str


@dataclass(frozen=True, slots=True)
class MatchRaceEditCommand:
    race_id: int
    name: str
    starts_at: datetime
    description: str | None
    reason: str | None
    actor_discord_user_id: str
    interaction_id: str


@dataclass(frozen=True, slots=True)
class MatchRaceConditionCommand:
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
    reason: str | None
    actor_discord_user_id: str
    interaction_id: str


@dataclass(frozen=True, slots=True)
class MatchRaceEntriesCommand:
    race_id: int
    entries: tuple[RegisteredFinalEntrySnapshotInput, ...]
    reason: str | None
    actor_discord_user_id: str
    interaction_id: str


@dataclass(frozen=True, slots=True)
class MatchRaceBettingCommand:
    race_id: int
    reason: str | None
    actor_discord_user_id: str
    interaction_id: str


@dataclass(frozen=True, slots=True)
class MatchRaceBettingBatchCommand:
    race_ids: tuple[int, ...]
    opening: bool
    reason: str | None
    actor_discord_user_id: str
    interaction_id: str


def execute_match_race_create(command: MatchRaceCreateCommand) -> RaceOperationResultDTO:
    service_command = CreateMatchRaceCommand(
        name=command.name,
        starts_at=command.starts_at,
        description=command.description,
        actor_discord_user_id=command.actor_discord_user_id,
        idempotency_key=_single_operation_key(command.interaction_id),
        reason=command.reason,
    )
    return run_application_command(lambda session: create_match_race(session, command=service_command))


def execute_match_race_edit_preserving_event(
    command: MatchRaceEditCommand,
) -> RaceOperationResultDTO:
    """Edit a Race while preserving its event association under the Race lock."""

    def operation(session: Session) -> RaceOperationResultDTO:
        race = session.scalar(select(Race).where(Race.id == command.race_id).with_for_update())
        if race is None:
            raise ValueError("수정할 룸매치 경기를 찾을 수 없습니다.")
        return edit_match_race(
            session,
            command=EditMatchRaceCommand(
                race_id=command.race_id,
                name=command.name,
                starts_at=command.starts_at,
                description=command.description,
                event_id=race.event_id,
                actor_discord_user_id=command.actor_discord_user_id,
                idempotency_key=_single_operation_key(command.interaction_id),
                reason=command.reason,
            ),
        )

    return run_application_command(operation)


def execute_match_race_condition_set(
    command: MatchRaceConditionCommand,
) -> RaceOperationResultDTO:
    service_command = SetRaceConditionCommand(
        race_id=command.race_id,
        grade=command.grade,
        venue=command.venue,
        track_surface=command.track_surface,
        distance=command.distance,
        direction=command.direction,
        season=command.season,
        weather=command.weather,
        track_condition=command.track_condition,
        condition_label=command.condition_label,
        actor_discord_user_id=command.actor_discord_user_id,
        idempotency_key=_single_operation_key(command.interaction_id),
        reason=command.reason,
    )
    return run_application_command(lambda session: set_match_condition(session, command=service_command))


def execute_match_race_entries_replacement(
    command: MatchRaceEntriesCommand,
) -> RaceOperationResultDTO:
    service_command = ReplaceRegisteredFinalEntrySnapshotCommand(
        race_id=command.race_id,
        entries=command.entries,
        actor_discord_user_id=command.actor_discord_user_id,
        idempotency_key=_single_operation_key(command.interaction_id),
        reason=command.reason,
    )
    return run_application_command(
        lambda session: replace_registered_final_entry_snapshot(
            session,
            command=service_command,
        )
    )


def execute_match_betting_open(
    command: MatchRaceBettingCommand,
) -> RaceOperationResultDTO:
    as_of = _now_utc()
    service_command = OpenMatchBettingCommand(
        race_id=command.race_id,
        as_of=as_of,
        actor_discord_user_id=command.actor_discord_user_id,
        idempotency_key=_single_operation_key(command.interaction_id),
        reason=command.reason,
    )
    return run_application_command(lambda session: open_match_betting(session, command=service_command))


def execute_match_betting_close(
    command: MatchRaceBettingCommand,
) -> RaceOperationResultDTO:
    as_of = _now_utc()
    service_command = CloseMatchBettingCommand(
        race_id=command.race_id,
        as_of=as_of,
        actor_discord_user_id=command.actor_discord_user_id,
        idempotency_key=_single_operation_key(command.interaction_id),
        reason=command.reason,
    )
    return run_application_command(lambda session: close_match_betting(session, command=service_command))


def execute_match_betting_batch_transition(
    command: MatchRaceBettingBatchCommand,
) -> tuple[RaceOperationResultDTO, ...]:
    ordered_ids = _normalize_lifecycle_batch_ids(command.race_ids)
    as_of = _now_utc()

    def operation(session: Session) -> tuple[RaceOperationResultDTO, ...]:
        results: list[RaceOperationResultDTO] = []
        for race_id in ordered_ids:
            idempotency_key = f"race-control:{command.interaction_id}:{race_id}"
            if command.opening:
                result = open_match_betting(
                    session,
                    command=OpenMatchBettingCommand(
                        race_id=race_id,
                        as_of=as_of,
                        actor_discord_user_id=command.actor_discord_user_id,
                        idempotency_key=idempotency_key,
                        reason=command.reason,
                    ),
                )
            else:
                result = close_match_betting(
                    session,
                    command=CloseMatchBettingCommand(
                        race_id=race_id,
                        as_of=as_of,
                        actor_discord_user_id=command.actor_discord_user_id,
                        idempotency_key=idempotency_key,
                        reason=command.reason,
                    ),
                )
            results.append(result)
        return tuple(results)

    return run_application_command(operation)


def query_match_race(*, race_id: int) -> RaceOperationResultDTO:
    return run_application_query(lambda session: get_match_race(session, race_id=race_id))


def query_match_race_choices(
    *,
    purpose: RoomRaceChoicePurpose,
    query: str = "",
    allowed_statuses: tuple[str, ...] | None = None,
) -> tuple[AutocompleteChoice, ...]:
    """Return bounded Room Match choices through the read-only application runner."""

    def operation(session) -> tuple[AutocompleteChoice, ...]:
        if allowed_statuses is None:
            return autocomplete_room_races(
                session,
                purpose=purpose,
                query=query,
            )
        return autocomplete_room_races(
            session,
            purpose=purpose,
            query=query,
            allowed_statuses=allowed_statuses,
        )

    return run_application_query(operation)


def _single_operation_key(interaction_id: str) -> str:
    return f"race-control:{interaction_id}"


def _normalize_lifecycle_batch_ids(values: tuple[int, ...]) -> tuple[int, ...]:
    if not values or len(values) > 25:
        raise ValueError("경기는 1개 이상 25개 이하로 선택해야 합니다.")
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values):
        raise ValueError("선택한 경기 ID가 올바르지 않습니다.")
    if len(values) != len(set(values)):
        raise ValueError("같은 경기를 중복 선택할 수 없습니다.")
    return tuple(sorted(values))


def _now_utc() -> datetime:
    return datetime.now(UTC)
