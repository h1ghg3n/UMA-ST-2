"""Shared fakes and fixtures for WIN5 member Discord adapter tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from threading import get_ident
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands
from sqlalchemy import create_engine

from uma_st2.adapters.discord import (
    Win5MemberCommandGroup,
    Win5MemberDiscordAdapter,
    Win5MemberInteractionContext,
    Win5NormalSubmissionCancelConfirmView,
    Win5NormalSubmissionDraft,
    Win5NormalSubmissionEditorView,
    Win5SpecialSubmissionCancelConfirmView,
    Win5SpecialSubmissionDraft,
    Win5SpecialSubmissionEditorView,
    Win5SpecialSubmissionModal,
    Win5SubmissionCancelConfirmView,
    Win5SubmissionsView,
    format_active_season_info,
    format_open_rounds,
    format_standings,
    format_submission_history_round,
)
from uma_st2.application.win5 import (
    CancelledWin5Submission,
    SavedWin5Submission,
    Win5AcceptedNormalSubmission,
    Win5AcceptedSpecialSubmission,
    Win5ActiveSeasonInfo,
    Win5ActiveSeasonUnavailableError,
    Win5CancellableSubmission,
    Win5CancellableSubmissionUnavailableError,
    Win5MemberApprovalPendingError,
    Win5MemberQueryApprovalPendingError,
    Win5MemberQueryIdentityError,
    Win5MemberResult,
    Win5MemberSubmission,
    Win5MemberSubmissionPick,
    Win5MemberSubmissionRound,
    Win5MemberSubmissionRoundPage,
    Win5MemberSubmissionRoundSummary,
    Win5MemberSubmissionsDashboard,
    Win5NormalSubmissionEditor,
    Win5NormalSubmissionJudgement,
    Win5NormalSubmissionPick,
    Win5NormalSubmissionRoundChoice,
    Win5OpenRoundCard,
    Win5RaceCard,
    Win5RaceEntryOption,
    Win5SeasonChoice,
    Win5SpecialSubmissionEditor,
    Win5SpecialSubmissionJudgement,
    Win5SpecialSubmissionPick,
    Win5SpecialSubmissionRoundChoice,
    Win5StandingEntry,
    Win5Standings,
    Win5StandingsSeasonUnavailableError,
    Win5SubmissionPickAuditSnapshot,
    Win5SubmissionsInvalidSourceError,
    Win5SubmissionsUnavailableError,
    Win5SubmissionVersionConflictError,
)
from uma_st2.compose import compose_win5_member_command_group
from uma_st2.domain.win5 import (
    Win5JudgementOutcome,
    Win5NormalJudgementItem,
    Win5NormalSubmissionScore,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SpecialJudgementItem,
    Win5SubmissionStatus,
    Win5SubmissionTier,
)
from uma_st2.infrastructure.database import DatabaseRuntime


def _round(
    *,
    id_: int,
    round_type: Win5RoundType,
    name: str,
    race_names: tuple[str, ...],
) -> Win5OpenRoundCard:
    return Win5OpenRoundCard(
        id=id_,
        season_id=7,
        season_name="2026 하반기",
        round_type=round_type,
        name=name,
        opens_at=None,
        closes_at=None,
        races=tuple(
            Win5RaceCard(
                id=(id_ * 100) + index,
                name=race_name,
                scheduled_at=None,
            )
            for index, race_name in enumerate(race_names, start=1)
        ),
    )


def _info() -> Win5ActiveSeasonInfo:
    return Win5ActiveSeasonInfo(
        season_id=7,
        season_name="2026 @everyone",
        season_status=Win5SeasonStatus.ACTIVE,
        starts_at=datetime(2026, 8, 25, 0, 0, tzinfo=UTC),
        ends_at=datetime(2026, 12, 31, 15, 0, tzinfo=UTC),
        total_round_count=4,
        open_round_count=2,
        season_score=21,
        top1_score=8,
    )


def _standings() -> Win5Standings:
    return Win5Standings(
        season_id=7,
        season_name="2026 @everyone 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        ranking="season",
        entries=(
            Win5StandingEntry(
                rank=1,
                persona_id="persona-1",
                display_name="우승자 @everyone",
                score=30,
            ),
            Win5StandingEntry(
                rank=2,
                persona_id="persona-2",
                display_name="공동 2위 A",
                score=20,
            ),
            Win5StandingEntry(
                rank=2,
                persona_id="persona-3",
                display_name="공동 2위 B",
                score=20,
            ),
        ),
    )


def _cancellable_submission(
    *,
    submission_id: int = 501,
    round_id: int = 11,
    round_name: str = "Round 11 @everyone",
    version: int = 4,
) -> Win5CancellableSubmission:
    return Win5CancellableSubmission(
        season_id=7,
        season_name="2026 @everyone 하반기",
        round_id=round_id,
        round_name=round_name,
        round_type=Win5RoundType.NORMAL,
        submission_id=submission_id,
        tier=Win5SubmissionTier.TOP3,
        version=version,
        pick_count=2,
    )


def _submissions_fixture() -> tuple[
    Win5MemberSubmissionsDashboard,
    tuple[Win5MemberSubmissionRoundPage, ...],
]:
    created_at = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
    open_entries = tuple(
        Win5RaceEntryOption(
            id=2000 + gate_number,
            gate_number=gate_number,
            name=f"Open Horse {gate_number}",
        )
        for gate_number in range(1, 7)
    )
    open_round = Win5MemberSubmissionRound(
        id=11,
        season_id=7,
        round_type=Win5RoundType.NORMAL,
        status=Win5RoundStatus.OPEN,
        name="Open Round @everyone",
        opens_at=None,
        closes_at=None,
        races=(
            Win5RaceCard(
                id=1101,
                name="Tokyo 11R",
                scheduled_at=None,
                entries=open_entries,
            ),
        ),
    )

    scored_entries = tuple(
        Win5RaceEntryOption(
            id=3000 + gate_number,
            gate_number=gate_number,
            name=f"Scored Horse {gate_number} @everyone",
        )
        for gate_number in range(1, 7)
    )
    results = tuple(
        Win5MemberResult(
            id=4100 + position,
            race_id=1201,
            position=position,
            race_entry_id=3000 + position,
            gate_number=None,
            reference_entry=scored_entries[position - 1],
        )
        for position in range(1, 6)
    )
    cancelled_pick = Win5MemberSubmissionPick(
        id=6001,
        race_id=1201,
        position=1,
        race_entry_id=3002,
        gate_number=None,
        reference_entry=scored_entries[1],
    )
    exact_pick = Win5MemberSubmissionPick(
        id=7001,
        race_id=1201,
        position=1,
        race_entry_id=3001,
        gate_number=None,
        reference_entry=scored_entries[0],
    )
    off_board_pick = Win5MemberSubmissionPick(
        id=7002,
        race_id=1201,
        position=2,
        race_entry_id=3006,
        gate_number=None,
        reference_entry=scored_entries[5],
    )
    score = Win5NormalSubmissionScore(
        tier=Win5SubmissionTier.TOP3,
        items=(
            Win5NormalJudgementItem(
                position=1,
                submission_pick_id=exact_pick.id,
                matched_result_id=results[0].id,
                outcome=Win5JudgementOutcome.EXACT,
                season_score_delta=3,
            ),
            Win5NormalJudgementItem(
                position=2,
                submission_pick_id=off_board_pick.id,
                matched_result_id=None,
                outcome=Win5JudgementOutcome.OFF_BOARD,
                season_score_delta=0,
            ),
            Win5NormalJudgementItem(
                position=3,
                submission_pick_id=None,
                matched_result_id=None,
                outcome=Win5JudgementOutcome.MISSING,
                season_score_delta=0,
            ),
        ),
        exact_count=1,
        wrong_position_count=0,
        off_board_count=1,
        missing_count=1,
        season_score_delta=3,
        top1_score_delta=0,
        circle_point_reward=10,
    )
    scored_round = Win5MemberSubmissionRound(
        id=12,
        season_id=7,
        round_type=Win5RoundType.NORMAL,
        status=Win5RoundStatus.SCORED,
        name="Scored Round @everyone",
        opens_at=None,
        closes_at=None,
        races=(
            Win5RaceCard(
                id=1201,
                name="Nakayama 11R",
                scheduled_at=None,
                entries=scored_entries,
            ),
        ),
        results=results,
        submissions=(
            Win5MemberSubmission(
                id=500,
                round_id=12,
                persona_id="persona-1",
                tier=Win5SubmissionTier.TOP1,
                status=Win5SubmissionStatus.CANCELLED,
                active_marker=None,
                version=2,
                created_at=created_at,
                updated_at=created_at,
                picks=(cancelled_pick,),
            ),
            Win5MemberSubmission(
                id=501,
                round_id=12,
                persona_id="persona-1",
                tier=Win5SubmissionTier.TOP3,
                status=Win5SubmissionStatus.ACCEPTED,
                active_marker=True,
                version=4,
                created_at=created_at,
                updated_at=created_at,
                picks=(exact_pick, off_board_pick),
                judgement=Win5NormalSubmissionJudgement(
                    event_id=9001,
                    season_id=7,
                    round_id=12,
                    race_id=1201,
                    submission_id=501,
                    submission_version=4,
                    persona_id="persona-1",
                    tier=Win5SubmissionTier.TOP3,
                    result_fingerprint="f" * 64,
                    scoring_policy_version="normal-3-1-0-v1",
                    reward_policy_version="normal-exact-reward-v1",
                    score=score,
                ),
            ),
        ),
    )

    special_reference = Win5RaceEntryOption(id=4008, gate_number=8, name="Do Deuce @everyone")
    special_result = Win5MemberResult(
        id=4201,
        race_id=1301,
        position=1,
        race_entry_id=None,
        gate_number=8,
        reference_entry=special_reference,
    )
    special_round = Win5MemberSubmissionRound(
        id=13,
        season_id=7,
        round_type=Win5RoundType.SPECIAL,
        status=Win5RoundStatus.SCORED,
        name="Special Scored Round",
        opens_at=None,
        closes_at=None,
        races=(
            Win5RaceCard(
                id=1301,
                name="Kyoto 10R @everyone",
                scheduled_at=None,
                entries=(special_reference,),
            ),
        ),
        results=(special_result,),
        submissions=(
            Win5MemberSubmission(
                id=502,
                round_id=13,
                persona_id="persona-1",
                tier=Win5SubmissionTier.SPECIAL_WINNER,
                status=Win5SubmissionStatus.ACCEPTED,
                active_marker=True,
                version=1,
                created_at=created_at,
                updated_at=created_at,
                picks=(
                    Win5MemberSubmissionPick(
                        id=8001,
                        race_id=1301,
                        position=1,
                        race_entry_id=None,
                        gate_number=8,
                        reference_entry=special_reference,
                    ),
                ),
                judgement=Win5SpecialSubmissionJudgement(
                    event_id=9002,
                    season_id=7,
                    round_id=13,
                    submission_id=502,
                    submission_version=1,
                    persona_id="persona-1",
                    tier=Win5SubmissionTier.SPECIAL_WINNER,
                    result_fingerprint="a" * 64,
                    scoring_policy_version="special-winner-plus-one-v1",
                    reward_policy_version="special-no-circle-point-v1",
                    race_count=1,
                    exact_count=1,
                    off_board_count=0,
                    missing_count=0,
                    season_score_delta=1,
                    top1_score_delta=1,
                    circle_point_reward=0,
                    items=(
                        Win5SpecialJudgementItem(
                            race_id=1301,
                            submission_pick_id=8001,
                            matched_result_id=special_result.id,
                            outcome=Win5JudgementOutcome.EXACT,
                            season_score_delta=1,
                        ),
                    ),
                ),
            ),
        ),
    )
    dashboard = Win5MemberSubmissionsDashboard(
        season_id=7,
        season_name="2026 @everyone 하반기",
        open_rounds=(
            Win5MemberSubmissionRoundSummary(
                id=open_round.id,
                season_id=open_round.season_id,
                round_type=open_round.round_type,
                status=open_round.status,
                name=open_round.name,
                submission_count=0,
            ),
        ),
        scored_rounds=tuple(
            Win5MemberSubmissionRoundSummary(
                id=round_.id,
                season_id=round_.season_id,
                round_type=round_.round_type,
                status=round_.status,
                name=round_.name,
                submission_count=len(round_.submissions),
            )
            for round_ in (scored_round, special_round)
        ),
    )
    pages = tuple(
        Win5MemberSubmissionRoundPage(
            season_id=7,
            season_name="2026 @everyone 하반기",
            round=round_,
            total_submission_count=len(round_.submissions),
            submission_offset=0,
            submission_limit=5,
            total_race_count=len(round_.races),
            race_offset=0,
            race_limit=5,
        )
        for round_ in (open_round, scored_round, special_round)
    )
    return dashboard, pages


def _void_submission_history_fixture() -> tuple[
    Win5MemberSubmissionsDashboard,
    Win5MemberSubmissionRoundPage,
    Win5MemberSubmissionRoundPage,
]:
    dashboard, pages = _submissions_fixture()
    source_page = pages[2]
    source_round = source_page.round
    source_submission = source_round.submissions[0]
    source_judgement = source_submission.judgement
    assert isinstance(source_judgement, Win5SpecialSubmissionJudgement)
    void_race = Win5RaceCard(
        id=1302,
        name="Tokyo 취소 Race @everyone",
        scheduled_at=None,
        void_reason="강풍으로 공식 취소 @everyone",
        voided_at=datetime(2026, 8, 25, 1, 0, tzinfo=UTC),
    )
    void_pick = Win5MemberSubmissionPick(
        id=8002,
        race_id=void_race.id,
        position=1,
        race_entry_id=None,
        gate_number=9,
    )
    mixed_submission = replace(
        source_submission,
        picks=(*source_submission.picks, void_pick),
        judgement=replace(
            source_judgement,
            scoring_policy_version="special-winner-plus-one-v2",
            race_count=2,
            void_count=1,
            items=(
                *source_judgement.items,
                Win5SpecialJudgementItem(
                    race_id=void_race.id,
                    submission_pick_id=void_pick.id,
                    matched_result_id=None,
                    outcome=Win5JudgementOutcome.VOID,
                    season_score_delta=0,
                ),
            ),
        ),
    )
    mixed_round = replace(
        source_round,
        id=14,
        name="Mixed Void Round",
        races=(*source_round.races, void_race),
        submissions=(replace(mixed_submission, round_id=14),),
    )
    mixed_page = replace(
        source_page,
        round=mixed_round,
        total_race_count=2,
        total_void_race_count=1,
    )

    first_void_race = replace(
        source_round.races[0],
        void_reason="폭우로 공식 취소",
        voided_at=datetime(2026, 8, 25, 1, 5, tzinfo=UTC),
    )
    cancelled_round = replace(
        mixed_round,
        id=15,
        status=Win5RoundStatus.CANCELLED,
        name="All Void Round",
        races=(first_void_race, void_race),
        results=(),
        submissions=(
            replace(
                mixed_submission,
                round_id=15,
                judgement=None,
            ),
        ),
    )
    cancelled_page = replace(
        mixed_page,
        round=cancelled_round,
        total_void_race_count=2,
    )
    mixed_summary = Win5MemberSubmissionRoundSummary(
        id=14,
        season_id=7,
        round_type=Win5RoundType.SPECIAL,
        status=Win5RoundStatus.SCORED,
        name=mixed_round.name,
        submission_count=1,
    )
    cancelled_summary = replace(
        mixed_summary,
        id=15,
        status=Win5RoundStatus.CANCELLED,
        name=cancelled_round.name,
    )
    return (
        replace(
            dashboard,
            scored_rounds=(*dashboard.scored_rounds, mixed_summary),
            cancelled_rounds=(cancelled_summary,),
        ),
        mixed_page,
        cancelled_page,
    )


def _normal_editor(
    *,
    entry_count: int = 8,
    submission: Win5AcceptedNormalSubmission | None = None,
) -> Win5NormalSubmissionEditor:
    return Win5NormalSubmissionEditor(
        season_id=7,
        season_name="2026 @everyone 하반기",
        round_id=11,
        round_name="Round 11 @everyone",
        race_id=1101,
        race_name="Tokyo 11R",
        entries=tuple(
            Win5RaceEntryOption(
                id=2000 + gate_number,
                gate_number=gate_number,
                name=f"Horse {gate_number} @everyone",
            )
            for gate_number in range(1, entry_count + 1)
        ),
        submission=submission,
    )


def _special_editor(
    *,
    race_count: int = 7,
    submission: Win5AcceptedSpecialSubmission | None = None,
) -> Win5SpecialSubmissionEditor:
    return Win5SpecialSubmissionEditor(
        season_id=7,
        season_name="2026 @everyone 하반기",
        round_id=12,
        round_name="Special Round 12 @everyone",
        races=tuple(
            Win5RaceCard(
                id=1200 + index,
                name=f"Race {index} @everyone",
                scheduled_at=None,
                entries=(
                    Win5RaceEntryOption(
                        id=3000 + index,
                        gate_number=8,
                        name=f"Reference {index} @everyone",
                    ),
                )
                if index == 1
                else (),
            )
            for index in range(1, race_count + 1)
        ),
        submission=submission,
    )


class RecordingQueries:
    def __init__(
        self,
        result: tuple[Win5OpenRoundCard, ...] = (),
        *,
        error: Exception | None = None,
        dashboard: Win5MemberSubmissionsDashboard | None = None,
        dashboard_error: Exception | None = None,
        submission_history_pages: tuple[Win5MemberSubmissionRoundPage, ...] = (),
        submission_history_error: Exception | None = None,
        info: Win5ActiveSeasonInfo | None = None,
        info_error: Exception | None = None,
        season_choices: tuple[Win5SeasonChoice, ...] = (),
        standings: Win5Standings | None = None,
        standings_error: Exception | None = None,
        normal_round_choices: tuple[Win5NormalSubmissionRoundChoice, ...] = (),
        normal_editor: Win5NormalSubmissionEditor | None = None,
        normal_editor_error: Exception | None = None,
        special_round_choices: tuple[Win5SpecialSubmissionRoundChoice, ...] = (),
        special_editor: Win5SpecialSubmissionEditor | None = None,
        special_editor_error: Exception | None = None,
        cancellable_submissions: tuple[Win5CancellableSubmission, ...] = (),
        cancellable_submission: Win5CancellableSubmission | None = None,
        cancellable_error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.dashboard = dashboard
        self.dashboard_error = dashboard_error
        self.submission_history_pages = {
            (page.round.id, page.submission_offset, page.race_offset): page for page in submission_history_pages
        }
        self.submission_history_error = submission_history_error
        self.info = info
        self.info_error = info_error
        self.season_choices = season_choices
        self.standings = standings
        self.standings_error = standings_error
        self.normal_round_choices = normal_round_choices
        self.normal_editor = normal_editor
        self.normal_editor_error = normal_editor_error
        self.special_round_choices = special_round_choices
        self.special_editor = special_editor
        self.special_editor_error = special_editor_error
        self.cancellable_submissions = cancellable_submissions
        self.cancellable_submission = cancellable_submission
        self.cancellable_error = cancellable_error
        self.calls: list[tuple[int, int]] = []
        self.dashboard_calls: list[tuple[str, int]] = []
        self.submission_history_calls: list[tuple[str, int, int, int, int, int, int]] = []
        self.info_calls: list[tuple[str, int]] = []
        self.season_search_calls: list[tuple[str, int, int]] = []
        self.standings_calls: list[tuple[int, str, int, int]] = []
        self.normal_round_search_calls: list[tuple[str, str, int, int]] = []
        self.normal_editor_calls: list[tuple[str, int, int]] = []
        self.special_round_search_calls: list[tuple[str, str, int, int]] = []
        self.special_editor_calls: list[tuple[str, int, int]] = []
        self.cancellable_search_calls: list[tuple[str, str, int, int]] = []
        self.cancellable_calls: list[tuple[str, int, int]] = []

    def get_active_season_info(self, *, discord_user_id: str) -> Win5ActiveSeasonInfo:
        self.info_calls.append((discord_user_id, get_ident()))
        if self.info_error is not None:
            raise self.info_error
        if self.info is None:
            raise AssertionError("No active Season info was configured for this test.")
        return self.info

    def list_open_rounds(self, *, limit: int = 25) -> tuple[Win5OpenRoundCard, ...]:
        self.calls.append((limit, get_ident()))
        if self.error is not None:
            raise self.error
        return self.result

    def get_submissions_dashboard(
        self,
        *,
        discord_user_id: str,
    ) -> Win5MemberSubmissionsDashboard:
        self.dashboard_calls.append((discord_user_id, get_ident()))
        if self.dashboard_error is not None:
            raise self.dashboard_error
        if self.dashboard is None:
            raise AssertionError("No Submission dashboard was configured for this test.")
        return self.dashboard

    def get_submission_history_round(
        self,
        *,
        discord_user_id: str,
        round_id: int,
        submission_offset: int,
        submission_limit: int = 5,
        race_offset: int = 0,
        race_limit: int = 5,
    ) -> Win5MemberSubmissionRoundPage:
        self.submission_history_calls.append(
            (
                discord_user_id,
                round_id,
                submission_offset,
                submission_limit,
                race_offset,
                race_limit,
                get_ident(),
            )
        )
        if self.submission_history_error is not None:
            raise self.submission_history_error
        try:
            return self.submission_history_pages[(round_id, submission_offset, race_offset)]
        except KeyError as exc:
            raise AssertionError("No Submission history page was configured for this test.") from exc

    def search_standings_seasons(
        self,
        *,
        query: str = "",
        limit: int = 25,
    ) -> tuple[Win5SeasonChoice, ...]:
        self.season_search_calls.append((query, limit, get_ident()))
        if self.standings_error is not None:
            raise self.standings_error
        return self.season_choices

    def get_standings(
        self,
        *,
        season_id: int,
        ranking: str = "season",
        limit: int = 100,
    ) -> Win5Standings:
        self.standings_calls.append((season_id, ranking, limit, get_ident()))
        if self.standings_error is not None:
            raise self.standings_error
        if self.standings is None:
            raise AssertionError("No standings were configured for this test.")
        return self.standings

    def search_normal_submission_rounds(
        self,
        *,
        discord_user_id: str,
        query: str = "",
        limit: int = 25,
    ) -> tuple[Win5NormalSubmissionRoundChoice, ...]:
        self.normal_round_search_calls.append((discord_user_id, query, limit, get_ident()))
        if self.normal_editor_error is not None:
            raise self.normal_editor_error
        return self.normal_round_choices

    def get_normal_submission_editor(
        self,
        *,
        discord_user_id: str,
        round_id: int,
    ) -> Win5NormalSubmissionEditor:
        self.normal_editor_calls.append((discord_user_id, round_id, get_ident()))
        if self.normal_editor_error is not None:
            raise self.normal_editor_error
        if self.normal_editor is None:
            raise AssertionError("No Normal editor was configured for this test.")
        return self.normal_editor

    def search_special_submission_rounds(
        self,
        *,
        discord_user_id: str,
        query: str = "",
        limit: int = 25,
    ) -> tuple[Win5SpecialSubmissionRoundChoice, ...]:
        self.special_round_search_calls.append((discord_user_id, query, limit, get_ident()))
        if self.special_editor_error is not None:
            raise self.special_editor_error
        return self.special_round_choices

    def get_special_submission_editor(
        self,
        *,
        discord_user_id: str,
        round_id: int,
    ) -> Win5SpecialSubmissionEditor:
        self.special_editor_calls.append((discord_user_id, round_id, get_ident()))
        if self.special_editor_error is not None:
            raise self.special_editor_error
        if self.special_editor is None:
            raise AssertionError("No Special editor was configured for this test.")
        return self.special_editor

    def search_cancellable_submissions(
        self,
        *,
        discord_user_id: str,
        query: str = "",
        limit: int = 25,
    ) -> tuple[Win5CancellableSubmission, ...]:
        self.cancellable_search_calls.append((discord_user_id, query, limit, get_ident()))
        if self.cancellable_error is not None:
            raise self.cancellable_error
        return self.cancellable_submissions

    def get_cancellable_submission(
        self,
        *,
        discord_user_id: str,
        submission_id: int,
    ) -> Win5CancellableSubmission:
        self.cancellable_calls.append((discord_user_id, submission_id, get_ident()))
        if self.cancellable_error is not None:
            raise self.cancellable_error
        if self.cancellable_submission is None:
            raise AssertionError("No cancellable Submission was configured for this test.")
        return self.cancellable_submission


class RecordingPreparation:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, str, bool]] = []

    async def __call__(
        self,
        interaction: object,
        command_name: str,
        *,
        ephemeral: bool,
    ) -> bool:
        self.calls.append((interaction, command_name, ephemeral))
        return self.allowed


class RecordingAutocompleteAuthorization:
    def __init__(self, *, allowed: bool = True, error: Exception | None = None) -> None:
        self.allowed = allowed
        self.error = error
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        if self.error is not None:
            raise self.error
        return self.allowed


class RecordingInteractionAuthorization:
    def __init__(self, *, allowed: bool = True, error: Exception | None = None) -> None:
        self.allowed = allowed
        self.error = error
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        if self.error is not None:
            raise self.error
        return self.allowed


class RecordingCommands:
    def __init__(
        self,
        *,
        save_result: SavedWin5Submission | None = None,
        cancel_result: CancelledWin5Submission | None = None,
        error: Exception | None = None,
    ) -> None:
        self.save_result = save_result
        self.cancel_result = cancel_result
        self.error = error
        self.save_calls: list[tuple[object, int]] = []
        self.cancel_calls: list[tuple[object, int]] = []

    def save_submission(self, command: object) -> SavedWin5Submission:
        self.save_calls.append((command, get_ident()))
        if self.error is not None:
            raise self.error
        if self.save_result is None:
            raise AssertionError("No save result was configured for this test.")
        return self.save_result

    def cancel_submission(self, command: object) -> CancelledWin5Submission:
        self.cancel_calls.append((command, get_ident()))
        if self.error is not None:
            raise self.error
        if self.cancel_result is None:
            raise AssertionError("No cancellation result was configured for this test.")
        return self.cancel_result


@dataclass
class RecordingFollowup:
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    fail: bool = False

    async def send(self, content: str, **kwargs: object) -> None:
        if self.fail:
            raise RuntimeError("Discord follow-up failed")
        self.messages.append((content, kwargs))


@dataclass
class RecordingResponse:
    edits: list[dict[str, object]] = field(default_factory=list)
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    modals: list[discord.ui.Modal] = field(default_factory=list)
    defers: list[dict[str, object]] = field(default_factory=list)
    done: bool = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **kwargs: object) -> None:
        self.defers.append(kwargs)
        self.done = True

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)
        self.done = True

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))
        self.done = True

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self.modals.append(modal)
        self.done = True


class RecordingInteraction:
    def __init__(
        self,
        *,
        edit_fails: bool = False,
        interaction_id: int = 555,
        user_id: int = 123,
        guild_id: int = 987,
        channel_id: int = 654,
    ) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.followup = RecordingFollowup()
        self.response = RecordingResponse()
        self.edit_fails = edit_fails
        self.edits: list[dict[str, object]] = []
        self.delete_count = 0

    async def edit_original_response(self, **kwargs: object) -> None:
        if self.edit_fails:
            raise RuntimeError("Discord edit failed")
        self.edits.append(kwargs)

    async def delete_original_response(self) -> None:
        self.delete_count += 1


async def _run_in_test_worker[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    """Exercise the injected runner with deterministic executor ownership."""

    with ThreadPoolExecutor(max_workers=1) as executor:
        return await asyncio.get_running_loop().run_in_executor(executor, operation)


def _adapter(
    queries: RecordingQueries,
    preparation: RecordingPreparation,
    authorization: RecordingAutocompleteAuthorization | None = None,
    *,
    commands: RecordingCommands | None = None,
    interaction_authorization: RecordingInteractionAuthorization | None = None,
) -> Win5MemberDiscordAdapter:
    return Win5MemberDiscordAdapter(
        queries=queries,  # type: ignore[arg-type]
        submission_history_queries=queries,  # type: ignore[arg-type]
        commands=commands or RecordingCommands(),  # type: ignore[arg-type]
        prepare_command=preparation,  # type: ignore[arg-type]
        authorize_autocomplete=authorization or RecordingAutocompleteAuthorization(),  # type: ignore[arg-type]
        authorize_interaction=interaction_authorization or RecordingInteractionAuthorization(),  # type: ignore[arg-type]
        blocking_runner=_run_in_test_worker,
    )


def _layout_items(view: discord.ui.LayoutView, item_type: type[object]) -> list[object]:
    return [item for item in view.walk_children() if isinstance(item, item_type)]


def _layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _accepted_submission(*, version: int = 4) -> Win5AcceptedNormalSubmission:
    return Win5AcceptedNormalSubmission(
        id=501,
        tier=Win5SubmissionTier.TOP3,
        version=version,
        picks=(
            Win5NormalSubmissionPick(position=1, race_entry_id=2001),
            Win5NormalSubmissionPick(position=3, race_entry_id=2003),
        ),
    )


def _accepted_special_submission(*, version: int = 6) -> Win5AcceptedSpecialSubmission:
    return Win5AcceptedSpecialSubmission(
        id=502,
        version=version,
        picks=(
            Win5SpecialSubmissionPick(race_id=1201, gate_number=8),
            Win5SpecialSubmissionPick(race_id=1203, gate_number=99),
        ),
    )


__all__ = (
    "CancelledWin5Submission",
    "DatabaseRuntime",
    "RecordingAutocompleteAuthorization",
    "RecordingCommands",
    "RecordingInteraction",
    "RecordingInteractionAuthorization",
    "RecordingPreparation",
    "RecordingQueries",
    "SavedWin5Submission",
    "UTC",
    "Win5AcceptedNormalSubmission",
    "Win5ActiveSeasonInfo",
    "Win5ActiveSeasonUnavailableError",
    "Win5CancellableSubmissionUnavailableError",
    "Win5JudgementOutcome",
    "Win5MemberCommandGroup",
    "Win5MemberApprovalPendingError",
    "Win5MemberInteractionContext",
    "Win5MemberQueryApprovalPendingError",
    "Win5MemberQueryIdentityError",
    "Win5MemberResult",
    "Win5MemberSubmission",
    "Win5MemberSubmissionPick",
    "Win5NormalJudgementItem",
    "Win5NormalSubmissionCancelConfirmView",
    "Win5NormalSubmissionDraft",
    "Win5NormalSubmissionEditorView",
    "Win5NormalSubmissionPick",
    "Win5NormalSubmissionRoundChoice",
    "Win5RaceCard",
    "Win5RaceEntryOption",
    "Win5RoundStatus",
    "Win5RoundType",
    "Win5SeasonChoice",
    "Win5SeasonStatus",
    "Win5SpecialJudgementItem",
    "Win5SpecialSubmissionCancelConfirmView",
    "Win5SpecialSubmissionDraft",
    "Win5SpecialSubmissionEditorView",
    "Win5SpecialSubmissionJudgement",
    "Win5SpecialSubmissionModal",
    "Win5SpecialSubmissionPick",
    "Win5SpecialSubmissionRoundChoice",
    "Win5Standings",
    "Win5StandingsSeasonUnavailableError",
    "Win5SubmissionCancelConfirmView",
    "Win5SubmissionPickAuditSnapshot",
    "Win5SubmissionStatus",
    "Win5SubmissionTier",
    "Win5SubmissionVersionConflictError",
    "Win5SubmissionsInvalidSourceError",
    "Win5SubmissionsUnavailableError",
    "Win5SubmissionsView",
    "_accepted_special_submission",
    "_accepted_submission",
    "_adapter",
    "_cancellable_submission",
    "_info",
    "_layout_items",
    "_layout_text",
    "_normal_editor",
    "_round",
    "_special_editor",
    "_standings",
    "_submissions_fixture",
    "_void_submission_history_fixture",
    "app_commands",
    "asyncio",
    "compose_win5_member_command_group",
    "create_engine",
    "datetime",
    "discord",
    "format_active_season_info",
    "format_open_rounds",
    "format_standings",
    "format_submission_history_round",
    "get_ident",
    "logging",
    "pytest",
    "replace",
)
