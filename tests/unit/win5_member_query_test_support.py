"""Shared fakes and fixtures for WIN5 member query tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest
from sqlalchemy import create_engine

from uma_st2.application.execution import QueryRunner
from uma_st2.application.win5 import (
    Win5AcceptedNormalSubmission,
    Win5AcceptedSpecialSubmission,
    Win5ActiveSeasonInfo,
    Win5ActiveSeasonUnavailableError,
    Win5CancellableSubmission,
    Win5CancellableSubmissionUnavailableError,
    Win5MemberPersona,
    Win5MemberQueries,
    Win5MemberQueryApprovalPendingError,
    Win5MemberQueryIdentityError,
    Win5MemberQueryInvalidSourceError,
    Win5NormalSubmissionEditorSource,
    Win5NormalSubmissionInvalidSourceError,
    Win5NormalSubmissionPick,
    Win5NormalSubmissionRoundChoice,
    Win5NormalSubmissionRoundUnavailableError,
    Win5NormalSubmissionSource,
    Win5OpenRoundCard,
    Win5RaceCard,
    Win5RaceEntryOption,
    Win5SeasonChoice,
    Win5SpecialSubmissionEditorSource,
    Win5SpecialSubmissionInvalidSourceError,
    Win5SpecialSubmissionPick,
    Win5SpecialSubmissionRoundChoice,
    Win5SpecialSubmissionRoundUnavailableError,
    Win5SpecialSubmissionSource,
    Win5StandingEntry,
    Win5Standings,
    Win5StandingScore,
    Win5StandingsSeasonUnavailableError,
    Win5StandingsSource,
)
from uma_st2.compose import compose_win5_member_queries
from uma_st2.domain.identity import PersonaStatus
from uma_st2.domain.win5 import (
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SubmissionPick,
    Win5SubmissionStatus,
    Win5SubmissionTier,
)
from uma_st2.infrastructure.database import Base, DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    DiscordAccountORM,
    GameAccountORM,
    PersonaORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5RoundORM,
    Win5ScoreORM,
    Win5SeasonORM,
    Win5SubmissionORM,
    Win5SubmissionPickORM,
)

NOW = datetime(2026, 8, 25, 0, 0)
ROUND_OPENS_AT = datetime(2026, 9, 1, 9, 0)
ROUND_CLOSES_AT = datetime(2026, 9, 2, 9, 0)
RACE_SCHEDULED_AT = datetime(2026, 9, 2, 8, 30)


def _member(
    *,
    status: PersonaStatus = PersonaStatus.NORMAL,
    has_eligible_game_account: bool = True,
) -> Win5MemberPersona:
    return Win5MemberPersona(
        id="persona-1",
        status=status,
        has_eligible_game_account=has_eligible_game_account,
    )


def _game_account_row(
    *,
    id_: int,
    persona_id: str,
    uma_pid: str,
) -> dict[str, object]:
    return {
        "id": id_,
        "persona_id": persona_id,
        "game_region": "jp",
        "uma_pid": uma_pid,
        "nickname": f"Account {id_}",
        "affiliation": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _race_row(
    id_: int,
    round_id: int,
    name: str,
    *,
    scheduled_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "id": id_,
        "round_id": round_id,
        "name": name,
        "scheduled_at": scheduled_at,
        "created_at": NOW,
        "updated_at": NOW,
    }


class RecordingRepository:
    def __init__(
        self,
        result: tuple[Win5OpenRoundCard, ...],
        *,
        member: Win5MemberPersona | None = None,
        info: Win5ActiveSeasonInfo | None = None,
        season_choices: tuple[Win5SeasonChoice, ...] = (),
        standings_source: Win5StandingsSource | None = None,
        normal_round_choices: tuple[Win5NormalSubmissionRoundChoice, ...] = (),
        normal_editor_source: Win5NormalSubmissionEditorSource | None = None,
        special_round_choices: tuple[Win5SpecialSubmissionRoundChoice, ...] = (),
        special_editor_source: Win5SpecialSubmissionEditorSource | None = None,
        cancellable_submissions: tuple[Win5CancellableSubmission, ...] = (),
        cancellable_submission: Win5CancellableSubmission | None = None,
        active_open_overflow: bool = False,
    ) -> None:
        self.result = result
        self.member = member
        self.info = info
        self.season_choices = season_choices
        self.standings_source = standings_source
        self.normal_round_choices = normal_round_choices
        self.normal_editor_source = normal_editor_source
        self.special_round_choices = special_round_choices
        self.special_editor_source = special_editor_source
        self.cancellable_submissions = cancellable_submissions
        self.cancellable_submission = cancellable_submission
        self.active_open_overflow = active_open_overflow
        self.active_open_overflow_query_count = 0
        self.limits: list[int] = []
        self.member_discord_user_ids: list[str] = []
        self.info_persona_ids: list[str] = []
        self.season_searches: list[tuple[str, int]] = []
        self.standings_queries: list[tuple[int, str, int]] = []
        self.normal_round_searches: list[tuple[str, int]] = []
        self.normal_editor_queries: list[tuple[int, str]] = []
        self.special_round_searches: list[tuple[str, int]] = []
        self.special_editor_queries: list[tuple[int, str]] = []
        self.cancellable_searches: list[tuple[str, str, int]] = []
        self.cancellable_queries: list[tuple[int, str]] = []

    def find_member_persona(self, *, discord_user_id: str) -> Win5MemberPersona | None:
        self.member_discord_user_ids.append(discord_user_id)
        return self.member

    def has_active_open_round_overflow(self) -> bool:
        self.active_open_overflow_query_count += 1
        return self.active_open_overflow

    def get_active_season_info(self, *, persona_id: str) -> Win5ActiveSeasonInfo | None:
        self.info_persona_ids.append(persona_id)
        return self.info

    def list_open_rounds(self, *, limit: int) -> tuple[Win5OpenRoundCard, ...]:
        self.limits.append(limit)
        return self.result

    def search_normal_submission_rounds(
        self,
        *,
        query: str,
        limit: int,
    ) -> tuple[Win5NormalSubmissionRoundChoice, ...]:
        self.normal_round_searches.append((query, limit))
        return self.normal_round_choices

    def get_normal_submission_editor_source(
        self,
        *,
        round_id: int,
        persona_id: str,
    ) -> Win5NormalSubmissionEditorSource | None:
        self.normal_editor_queries.append((round_id, persona_id))
        return self.normal_editor_source

    def search_special_submission_rounds(
        self,
        *,
        query: str,
        limit: int,
    ) -> tuple[Win5SpecialSubmissionRoundChoice, ...]:
        self.special_round_searches.append((query, limit))
        return self.special_round_choices

    def get_special_submission_editor_source(
        self,
        *,
        round_id: int,
        persona_id: str,
    ) -> Win5SpecialSubmissionEditorSource | None:
        self.special_editor_queries.append((round_id, persona_id))
        return self.special_editor_source

    def search_cancellable_submissions(
        self,
        *,
        persona_id: str,
        query: str,
        limit: int,
    ) -> tuple[Win5CancellableSubmission, ...]:
        self.cancellable_searches.append((persona_id, query, limit))
        return self.cancellable_submissions

    def get_cancellable_submission(
        self,
        *,
        submission_id: int,
        persona_id: str,
    ) -> Win5CancellableSubmission | None:
        self.cancellable_queries.append((submission_id, persona_id))
        return self.cancellable_submission

    def search_standings_seasons(self, *, query: str, limit: int) -> tuple[Win5SeasonChoice, ...]:
        self.season_searches.append((query, limit))
        return self.season_choices

    def get_standings(
        self,
        *,
        season_id: int,
        ranking: str,
        limit: int,
    ) -> Win5StandingsSource | None:
        self.standings_queries.append((season_id, ranking, limit))
        return self.standings_source


@dataclass
class RecordingUnitOfWork:
    win5_member_queries: RecordingRepository
    entered: bool = False
    exited: bool = False
    commit_count: int = 0
    rollback_count: int = 0

    def __enter__(self) -> RecordingUnitOfWork:
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.exited = True
        return False

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class RecordingFactory:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(self.repository)
        self.created.append(unit_of_work)
        return unit_of_work


def _active_season_info(*, open_round_count: int = 2) -> Win5ActiveSeasonInfo:
    return Win5ActiveSeasonInfo(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        starts_at=datetime(2026, 8, 25, 0, 0, tzinfo=UTC),
        ends_at=None,
        total_round_count=4,
        open_round_count=open_round_count,
        season_score=21,
        top1_score=8,
    )


def _cancellable_submission() -> Win5CancellableSubmission:
    return Win5CancellableSubmission(
        season_id=7,
        season_name="2026 하반기",
        round_id=11,
        round_name="Round 11",
        round_type=Win5RoundType.NORMAL,
        submission_id=501,
        tier=Win5SubmissionTier.TOP3,
        version=4,
        pick_count=2,
    )


def _normal_editor_source(
    *,
    submission: Win5NormalSubmissionSource | None = None,
    round_status: Win5RoundStatus = Win5RoundStatus.OPEN,
    races: tuple[Win5RaceCard, ...] | None = None,
) -> Win5NormalSubmissionEditorSource:
    if races is None:
        races = (
            Win5RaceCard(
                id=1101,
                name="Tokyo 11R",
                scheduled_at=datetime(2026, 9, 2, 8, 30, tzinfo=UTC),
                entries=tuple(
                    Win5RaceEntryOption(
                        id=2000 + gate_number,
                        gate_number=gate_number,
                        name=f"Horse {gate_number}",
                    )
                    for gate_number in range(1, 9)
                ),
            ),
        )
    return Win5NormalSubmissionEditorSource(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        round_id=11,
        round_name="Round 11",
        round_type=Win5RoundType.NORMAL,
        round_status=round_status,
        races=races,
        submission=submission,
    )


def _special_editor_source(
    *,
    submission: Win5SpecialSubmissionSource | None = None,
    round_status: Win5RoundStatus = Win5RoundStatus.OPEN,
    races: tuple[Win5RaceCard, ...] | None = None,
) -> Win5SpecialSubmissionEditorSource:
    if races is None:
        races = (
            Win5RaceCard(
                id=1201,
                name="Tokyo 11R",
                scheduled_at=None,
                entries=(Win5RaceEntryOption(id=3008, gate_number=8, name="Do Deuce"),),
            ),
            Win5RaceCard(id=1202, name="Kyoto 10R", scheduled_at=None),
            Win5RaceCard(id=1203, name="Hanshin 12R", scheduled_at=None),
        )
    return Win5SpecialSubmissionEditorSource(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        round_id=12,
        round_name="Special Round 12",
        round_type=Win5RoundType.SPECIAL,
        round_status=round_status,
        races=races,
        submission=submission,
    )


__all__ = [
    "Base",
    "DatabaseRuntime",
    "DiscordAccountORM",
    "GameAccountORM",
    "NOW",
    "PersonaORM",
    "PersonaStatus",
    "QueryRunner",
    "RACE_SCHEDULED_AT",
    "ROUND_CLOSES_AT",
    "ROUND_OPENS_AT",
    "RecordingFactory",
    "RecordingRepository",
    "RecordingUnitOfWork",
    "UTC",
    "Win5AcceptedNormalSubmission",
    "Win5AcceptedSpecialSubmission",
    "Win5ActiveSeasonUnavailableError",
    "Win5CancellableSubmission",
    "Win5CancellableSubmissionUnavailableError",
    "Win5MemberPersona",
    "Win5MemberQueries",
    "Win5MemberQueryApprovalPendingError",
    "Win5MemberQueryIdentityError",
    "Win5MemberQueryInvalidSourceError",
    "Win5NormalSubmissionInvalidSourceError",
    "Win5NormalSubmissionPick",
    "Win5NormalSubmissionRoundChoice",
    "Win5NormalSubmissionRoundUnavailableError",
    "Win5NormalSubmissionSource",
    "Win5OpenRoundCard",
    "Win5RaceCard",
    "Win5RaceEntryORM",
    "Win5RaceEntryOption",
    "Win5RaceORM",
    "Win5RoundORM",
    "Win5RoundType",
    "Win5ScoreORM",
    "Win5SeasonChoice",
    "Win5SeasonORM",
    "Win5SeasonStatus",
    "Win5SpecialSubmissionInvalidSourceError",
    "Win5SpecialSubmissionPick",
    "Win5SpecialSubmissionRoundChoice",
    "Win5SpecialSubmissionRoundUnavailableError",
    "Win5SpecialSubmissionSource",
    "Win5StandingEntry",
    "Win5StandingScore",
    "Win5Standings",
    "Win5StandingsSeasonUnavailableError",
    "Win5StandingsSource",
    "Win5SubmissionORM",
    "Win5SubmissionPick",
    "Win5SubmissionPickORM",
    "Win5SubmissionStatus",
    "Win5SubmissionTier",
    "_active_season_info",
    "_cancellable_submission",
    "_game_account_row",
    "_member",
    "_normal_editor_source",
    "_race_row",
    "_special_editor_source",
    "compose_win5_member_queries",
    "create_engine",
    "datetime",
    "pytest",
]
